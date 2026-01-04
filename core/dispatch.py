import os
import time
import json
import torch
import random
import requests
import numpy as np
from PIL import Image
from copy import deepcopy

import folder_paths
from .utils import clean_url, get_client_id


def upload_input_images(remote_url, prompt):
	"""
	Find LoadImage nodes in prompt, upload their images to remote,
	and update prompt with new filenames if needed.

	Args:
		remote_url (str): Remote ComfyUI instance URL.
		prompt (dict): Workflow prompt dictionary.

	Returns:
		dict: Modified prompt (or original if no changes needed).
	"""
	for node_id, node in prompt.items():
		if node.get("class_type") != "LoadImage":
			continue

		image_ref = node.get("inputs", {}).get("image")
		if not image_ref:
			continue

		# Resolve local path using ComfyUI's path resolution
		local_path = folder_paths.get_annotated_filepath(image_ref)
		if not os.path.exists(local_path):
			print(f"NetDist: Warning - image not found: {local_path}")
			continue

		# Parse annotation if present (e.g., "file.png[output]")
		if "[" in image_ref:
			filename = image_ref.split("[")[0]
			upload_type = image_ref.split("[")[1].rstrip("]")
		else:
			filename = image_ref
			upload_type = "input"

		# Upload to remote
		print(f"NetDist: Uploading {filename} to {remote_url}...")
		with open(local_path, "rb") as f:
			files = {"image": (filename, f)}
			data = {"type": upload_type, "overwrite": "true"}
			r = requests.post(
				f"{remote_url}/upload/image",
				files=files,
				data=data,
				timeout=30
			)
			r.raise_for_status()
			result = r.json()

		# Update prompt if filename changed (due to remote's duplicate handling)
		new_filename = result.get("name", filename)
		if new_filename != filename:
			new_ref = f"{new_filename}[{upload_type}]" if upload_type != "input" else new_filename
			prompt[node_id]["inputs"]["image"] = new_ref
			print(f"NetDist: Uploaded {filename} -> {new_filename}")
		else:
			print(f"NetDist: Uploaded {filename}")

	return prompt


def clear_remote_queue(remote_url):
	r = requests.get(f"{remote_url}/queue", timeout=4)
	r.raise_for_status()
	queue = r.json()

	to_cancel = []
	client_id = get_client_id()
	for k in queue.get("queue_pending", []):
		if k[3].get("client_id") == client_id:
			to_cancel.append(k[1]) # job UUID
	r = requests.post(
		f"{remote_url}/queue",
		json    = {"delete" : to_cancel},
		timeout = 4,
	)
	r.raise_for_status()

	for k in queue.get("queue_running", []):
		if k[3].get("client_id") == client_id:
			r = requests.post(
				f"{remote_url}/interrupt",
				json    = {},
				timeout = 4,
			)
			r.raise_for_status()
			break

def get_remote_os(remote_url):
	url = f"{remote_url}/system_stats"
	r = requests.get(url, timeout=4)
	r.raise_for_status()
	data = r.json()
	return data["system"]["os"]

def get_output_nodes(remote_url):
	# I'm 90% sure this could just use the
	# list from the host but better safe than sorry
	url = f"{remote_url}/object_info"
	r = requests.get(url, timeout=4)
	r.raise_for_status()
	data = r.json()
	out = [k for k, v in data.items() if v.get("output_node")]
	return out

def dispatch_to_remote(remote_url, prompt, job_id=f"{get_client_id()}-unknown", outputs="final_image"):
	### PROMPT LOGIC ###
	prompt = deepcopy(prompt)

	# Upload input images to remote before processing
	prompt = upload_input_images(remote_url, prompt)

	to_del = []
	def recursive_node_deletion(start_node):
		target_nodes = [start_node]
		if start_node not in to_del:
			to_del.append(start_node)
		while len(target_nodes) > 0:
			new_targets = []
			for target in target_nodes:
				for node in prompt.keys():
					inputs = prompt[node].get("inputs")
					if not inputs:
						continue
					for i in inputs.values():
						if type(i) == list:
							if len(i) > 0 and i[0] in to_del:
								if node not in to_del:
									to_del.append(node)
									new_targets.append(node)
			target_nodes += new_targets
			target_nodes.remove(target)

	# find current node and disable all others
	output_src = None
	for i in prompt.keys():
		if prompt[i]["class_type"].startswith("RemoteQueue"):
			if clean_url(prompt[i]["inputs"]["remote_url"]) == remote_url:
				prompt[i]["inputs"]["enabled"] = "remote"
				output_src = i
			else:
				prompt[i]["inputs"]["enabled"] = "false"

	banned = [] if outputs == "any" else ["PreviewImage", "SaveImage"] # get_output_nodes(remote_url)
	output = None
	for i in prompt.keys():
		# only leave current fetch but replace with PreviewImage
		if prompt[i]["class_type"] == "FetchRemote":
			if prompt[i]["inputs"]["remote_info"][0] == output_src:
				output = {
					"inputs": {"images": prompt[i]["inputs"]["final_image"]},
					"class_type": 'PreviewImage',
					"final_output": True, # might allow multiple outputs with an ID?
				}
			recursive_node_deletion(i)
		# do not save output on remote
		if prompt[i]["class_type"] in banned:
			recursive_node_deletion(i)
	if output:
		prompt[str(max([int(x) for x in prompt.keys()])+1)] = output
	for i in to_del: del prompt[i]

	### OS LOGIC ###
	sep_remote = "\\" if get_remote_os(remote_url) == "nt" else "/"
	sep_local  = "\\" if os.name == "nt" else "/"
	sem_input_map = { # class type : input to replace
		"CheckpointLoaderSimple" : "ckpt_name",
		"CheckpointLoader"       : "ckpt_name",
		"LoraLoader"             : "lora_name",
		"VAELoader"              : "vae_name",
	}
	if sep_remote != sep_local:
		for i in prompt.keys():
			if prompt[i]["class_type"] in sem_input_map.keys():
				key = sem_input_map[prompt[i]["class_type"]]
				prompt[i]["inputs"][key] = prompt[i]["inputs"][key].replace(sep_local, sep_remote)

	### SEND REQUEST ###
	data = {
		"prompt": prompt,
		"client_id": get_client_id(),
		"extra_data": {
			"job_id": job_id,
		}
	}
	ar = requests.post(
		f"{remote_url}/prompt",
		data    = json.dumps(data),
		headers = {"Content-Type": "application/json"},
		timeout = 4,
	)
	if not ar.ok:
		print(f"NetDist: Remote server rejected prompt with status {ar.status_code}")
		print(f"NetDist: Response: {ar.text}")
	ar.raise_for_status()
	return
