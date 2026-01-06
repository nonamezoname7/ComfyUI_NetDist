"""
Subgraph extraction and remote execution for ComfyUI_NetDist.

This module handles extracting a portion of a workflow (subgraph) and
dispatching it to a remote ComfyUI instance.
"""

import os
import json
import time
import requests
import torch
import numpy as np
from copy import deepcopy
from PIL import Image

import folder_paths
from .utils import clean_url, get_client_id, get_new_job_id


def is_link(value):
    """
    Check if a value is a ComfyUI node link.

    Args:
        value: Input value to check.

    Returns:
        bool: True if value is a link [node_id, output_index].
    """
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def extract_subgraph(prompt, queue_node_id):
    """
    Trace upstream from queue node to find all dependent nodes.

    Args:
        prompt (dict): Full workflow prompt.
        queue_node_id (str): ID of the RemoteSubgraphQueue node.

    Returns:
        tuple: (subgraph_nodes set, trigger_link or None)
            - subgraph_nodes: Set of node IDs in the subgraph
            - trigger_link: [node_id, output_index] of the trigger input
    """
    queue_node = prompt.get(queue_node_id)
    if not queue_node:
        return set(), None

    # Find what the trigger input connects to
    trigger_link = queue_node.get("inputs", {}).get("trigger")
    if not is_link(trigger_link):
        # No upstream connection - nothing to extract
        return set(), None

    # Trace upstream from trigger
    subgraph_nodes = set()
    to_visit = [trigger_link[0]]

    while to_visit:
        node_id = to_visit.pop()
        if node_id in subgraph_nodes:
            continue
        if node_id not in prompt:
            continue

        subgraph_nodes.add(node_id)
        node = prompt[node_id]

        # Find all upstream links
        for input_value in node.get("inputs", {}).values():
            if is_link(input_value):
                upstream_id = input_value[0]
                if upstream_id not in subgraph_nodes:
                    to_visit.append(upstream_id)

    return subgraph_nodes, trigger_link


def extract_subgraph_from_dynprompt(dynprompt, start_node_id):
    """
    Trace upstream from a node using DynamicPrompt to find all dependent nodes.

    Args:
        dynprompt: DynamicPrompt object from ComfyUI execution.
        start_node_id (str): ID of the node to trace upstream from.

    Returns:
        set: Set of node IDs in the subgraph.
    """
    subgraph_nodes = set()
    to_visit = [start_node_id]

    while to_visit:
        node_id = to_visit.pop()
        if node_id in subgraph_nodes:
            continue

        node = dynprompt.get_node(node_id)
        if node is None:
            continue

        subgraph_nodes.add(node_id)

        # Find all upstream links
        for input_value in node.get("inputs", {}).values():
            if is_link(input_value):
                upstream_id = input_value[0]
                if upstream_id not in subgraph_nodes:
                    to_visit.append(upstream_id)

    return subgraph_nodes


def upload_subgraph_images(remote_url, prompt, subgraph_nodes):
    """
    Upload images from LoadImage nodes in the subgraph to remote.

    Args:
        remote_url (str): Remote ComfyUI instance URL.
        prompt (dict): Full workflow prompt.
        subgraph_nodes (set): Node IDs in the subgraph.

    Returns:
        dict: Mapping of node_id -> new_filename for any changed filenames.
    """
    filename_updates = {}

    for node_id in subgraph_nodes:
        node = prompt.get(node_id)
        if not node or node.get("class_type") != "LoadImage":
            continue

        image_ref = node.get("inputs", {}).get("image")
        if not image_ref:
            continue

        # Resolve local path
        local_path = folder_paths.get_annotated_filepath(image_ref)
        if not os.path.exists(local_path):
            print(f"NetDist: Warning - image not found: {local_path}")
            continue

        # Parse annotation if present
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

        new_filename = result.get("name", filename)
        if new_filename != filename:
            new_ref = f"{new_filename}[{upload_type}]" if upload_type != "input" else new_filename
            filename_updates[node_id] = new_ref
            print(f"NetDist: Uploaded {filename} -> {new_filename}")
        else:
            print(f"NetDist: Uploaded {filename}")

    return filename_updates


def get_remote_os(remote_url):
    """
    Get the operating system of the remote ComfyUI instance.

    Args:
        remote_url (str): Remote ComfyUI instance URL.

    Returns:
        str: OS identifier (e.g., "nt" for Windows, "posix" for Linux/Mac).
    """
    url = f"{remote_url}/system_stats"
    r = requests.get(url, timeout=4)
    r.raise_for_status()
    data = r.json()
    return data["system"]["os"]


def prepare_subgraph_prompt(prompt, subgraph_nodes, trigger_link, remote_url):
    """
    Build a standalone prompt containing only the subgraph nodes.

    Args:
        prompt (dict): Full workflow prompt.
        subgraph_nodes (set): Node IDs to include.
        trigger_link (list): [node_id, output_index] for the output to capture.
        remote_url (str): Remote ComfyUI instance URL.

    Returns:
        dict: Standalone prompt ready for remote execution.
    """
    # Deep copy only the subgraph nodes
    subgraph_prompt = {}
    for node_id in subgraph_nodes:
        subgraph_prompt[node_id] = deepcopy(prompt[node_id])

    # Upload images and update references
    filename_updates = upload_subgraph_images(remote_url, prompt, subgraph_nodes)
    for node_id, new_ref in filename_updates.items():
        subgraph_prompt[node_id]["inputs"]["image"] = new_ref

    # Handle OS path separator differences for model loaders
    sep_remote = "\\" if get_remote_os(remote_url) == "nt" else "/"
    sep_local = "\\" if os.name == "nt" else "/"
    path_input_map = {
        "CheckpointLoaderSimple": "ckpt_name",
        "CheckpointLoader": "ckpt_name",
        "LoraLoader": "lora_name",
        "VAELoader": "vae_name",
    }
    if sep_remote != sep_local:
        for node_id in subgraph_nodes:
            node = subgraph_prompt[node_id]
            if node["class_type"] in path_input_map:
                key = path_input_map[node["class_type"]]
                node["inputs"][key] = node["inputs"][key].replace(sep_local, sep_remote)

    # Add capture node (PreviewImage) to get output
    # Use a new node ID that won't conflict (handle "group:node" format IDs)
    capture_node_id = f"netdist_capture_{int(time.time() * 1000)}"
    subgraph_prompt[capture_node_id] = {
        "class_type": "PreviewImage",
        "inputs": {"images": trigger_link},
        "final_output": True,
    }

    return subgraph_prompt


def dispatch_subgraph(remote_url, subgraph_prompt, job_id):
    """
    Send the subgraph prompt to a remote ComfyUI instance.

    Args:
        remote_url (str): Remote ComfyUI instance URL.
        subgraph_prompt (dict): Prepared subgraph prompt.
        job_id (str): Unique job identifier.

    Returns:
        bool: True if dispatch succeeded.
    """
    data = {
        "prompt": subgraph_prompt,
        "client_id": get_client_id(),
        "extra_data": {
            "job_id": job_id,
        }
    }
    r = requests.post(
        f"{remote_url}/prompt",
        data=json.dumps(data),
        headers={"Content-Type": "application/json"},
        timeout=4,
    )
    if not r.ok:
        print(f"NetDist: Remote server rejected subgraph with status {r.status_code}")
        print(f"NetDist: Response: {r.text}")
    r.raise_for_status()
    return True


POLLING_INTERVAL = 0.5


def wait_for_subgraph_job(remote_url, job_id):
    """
    Poll remote /history until job completes.

    Args:
        remote_url (str): Remote ComfyUI instance URL.
        job_id (str): Job identifier to wait for.

    Returns:
        list: List of output image info dicts, or empty list.
    """
    fail_count = 0
    while fail_count <= 3:
        try:
            r = requests.get(f"{remote_url}/history", timeout=4)
            r.raise_for_status()
        except Exception as e:
            print(f"NetDist: Error polling history: {e}")
            fail_count += 1
            time.sleep(POLLING_INTERVAL)
            continue

        data = r.json()
        if not data:
            time.sleep(POLLING_INTERVAL)
            continue

        # Find our job by job_id in extra_data
        for prompt_id, job_data in data.items():
            extra_data = job_data.get("prompt", [None, None, None, {}])[3]
            if extra_data.get("job_id") == job_id:
                outputs = job_data.get("outputs", {})
                if outputs:
                    # Find node with final_output flag or use last
                    inputs_data = job_data.get("prompt", [None, None, {}])[2]
                    for node_id, node_inputs in inputs_data.items():
                        if node_inputs.get("final_output") and node_id in outputs:
                            return outputs[node_id].get("images", [])
                    # Fallback to last output
                    last_node = list(outputs.keys())[-1]
                    return outputs[last_node].get("images", [])
                return []

        time.sleep(POLLING_INTERVAL)

    raise OSError(f"NetDist: Failed to fetch subgraph output after {fail_count} failures")


def fetch_subgraph_image(remote_url, job_id):
    """
    Fetch IMAGE output from a completed subgraph job.

    Args:
        remote_url (str): Remote ComfyUI instance URL.
        job_id (str): Job identifier.

    Returns:
        torch.Tensor: Image tensor in ComfyUI format [B, H, W, C], or None.
    """
    images_info = wait_for_subgraph_job(remote_url, job_id)
    if not images_info:
        return None

    images = []
    for img_info in images_info:
        img_url = (
            f"{remote_url}/view?"
            f"filename={img_info['filename']}&"
            f"subfolder={img_info['subfolder']}&"
            f"type={img_info['type']}"
        )
        r = requests.get(img_url, stream=True, timeout=16)
        r.raise_for_status()

        img = Image.open(r.raw).convert("RGB")
        img_array = np.array(img).astype(np.float32) / 255.0
        img_tensor = torch.from_numpy(img_array)[None,]
        images.append(img_tensor)

    if not images:
        return None

    # Concatenate batch
    out = images[0]
    for img in images[1:]:
        out = torch.cat((out, img))
    return out
