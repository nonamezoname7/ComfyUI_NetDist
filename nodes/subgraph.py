"""
Subgraph execution nodes for ComfyUI_NetDist.

Nodes for extracting and executing portions of a workflow on remote
ComfyUI instances. Uses rawLink to prevent local execution of upstream nodes.
"""

from comfy_execution.graph_utils import is_link

from ..core.utils import clean_url, get_new_job_id
from ..core.subgraph import (
    extract_subgraph_from_dynprompt,
    prepare_subgraph_prompt,
    dispatch_subgraph,
    fetch_subgraph_image,
)


class RemoteSubgraphQueue:
    """
    Extract a subgraph from the workflow and dispatch to remote.

    Uses lazy=True to prevent upstream nodes from being scheduled initially,
    combined with rawLink=True to receive the trigger link [node_id, slot]
    without triggering local execution. check_lazy_status returns [] to
    ensure upstream is never requested.
    """

    TITLE = "Queue Subgraph on Remote"
    CATEGORY = "remote/subgraph"
    RETURN_TYPES = ("REMINFO",)
    RETURN_NAMES = ("remote_info",)
    FUNCTION = "queue"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "trigger": ("IMAGE", {"lazy": True, "rawLink": True}),
                "remote_url": ("STRING", {
                    "default": "http://127.0.0.1:8288",
                    "multiline": False,
                }),
                "mode": (["remote", "local"], {"default": "remote"}),
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    def check_lazy_status(self, trigger, remote_url, mode, dynprompt, unique_id):
        """
        Never request the trigger input - we only need the link reference.

        Returns:
            list: Empty list - no inputs need evaluation.
        """
        # We never need the actual IMAGE data, only the link [node_id, slot]
        # which we get via rawLink. Return empty list to prevent upstream execution.
        return []

    def queue(self, trigger, remote_url, mode, dynprompt, unique_id):
        """
        Extract subgraph and dispatch to remote.

        Args:
            trigger: Raw link [node_id, output_index] to the subgraph output.
            remote_url (str): Remote ComfyUI instance URL.
            mode (str): "remote" to execute on remote, "local" to skip.
            dynprompt: DynamicPrompt object for accessing graph structure.
            unique_id (str): This node's ID.

        Returns:
            tuple: (remote_info dict,)
        """
        # trigger is a raw link [node_id, output_index]
        if not is_link(trigger):
            raise ValueError(f"NetDist: trigger must be a link, got {type(trigger)}")

        if mode == "local":
            print(f"NetDist: Subgraph mode=local, skipping remote dispatch")
            return ({
                "mode": "local",
                "trigger_link": trigger,
            },)

        remote_url = clean_url(remote_url)
        trigger_node_id = trigger[0]
        print(f"NetDist: Extracting subgraph from trigger node {trigger_node_id}...")

        # Extract subgraph by tracing upstream from trigger node
        subgraph_nodes = extract_subgraph_from_dynprompt(dynprompt, trigger_node_id)

        if not subgraph_nodes:
            raise ValueError("NetDist: No subgraph found - nothing to execute")

        print(f"NetDist: Found {len(subgraph_nodes)} nodes in subgraph")

        # Build the prompt dict from dynprompt
        prompt = {}
        for node_id in subgraph_nodes:
            prompt[node_id] = dynprompt.get_node(node_id)

        # Prepare and dispatch
        print(f"NetDist: Preparing subgraph for {remote_url}...")
        subgraph_prompt = prepare_subgraph_prompt(
            prompt, subgraph_nodes, trigger, remote_url
        )
        job_id = get_new_job_id()
        print(f"NetDist: Dispatching subgraph (job_id={job_id})...")
        dispatch_subgraph(remote_url, subgraph_prompt, job_id)
        print(f"NetDist: Subgraph dispatched, waiting for fetch...")

        return ({
            "mode": "remote",
            "remote_url": remote_url,
            "job_id": job_id,
            "trigger_link": trigger,
        },)


class SubgraphFetch_IMAGE:
    """
    Fetch IMAGE output from a remote subgraph execution.

    Polls the remote instance for job completion and retrieves the output image.
    """

    TITLE = "Fetch Subgraph Output (Image)"
    CATEGORY = "remote/subgraph"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "fetch"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "remote_info": ("REMINFO",),
            },
            "optional": {
                "local_image": ("IMAGE",),
            },
        }

    def fetch(self, remote_info, local_image=None):
        """
        Fetch IMAGE from remote or return local value.

        Args:
            remote_info (dict): Info from RemoteSubgraphQueue.
            local_image: IMAGE to use in local mode.

        Returns:
            tuple: (image tensor,)
        """
        mode = remote_info.get("mode", "remote")

        if mode == "local":
            print("NetDist: Fetch mode=local, using local_image")
            if local_image is not None:
                return (local_image,)
            raise ValueError("NetDist: Local mode requires local_image input")

        # Fetch from remote
        remote_url = remote_info.get("remote_url")
        job_id = remote_info.get("job_id")

        if not remote_url or not job_id:
            raise ValueError("NetDist: Missing remote_url or job_id in remote_info")

        print(f"NetDist: Fetching IMAGE from {remote_url} (job_id={job_id})...")
        result = fetch_subgraph_image(remote_url, job_id)

        if result is None:
            raise ValueError("NetDist: Failed to fetch image from remote")

        print(f"NetDist: Fetch complete, image shape={result.shape}")
        return (result,)


NODE_CLASS_MAPPINGS = {
    "RemoteSubgraphQueue": RemoteSubgraphQueue,
    "SubgraphFetch_IMAGE": SubgraphFetch_IMAGE,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RemoteSubgraphQueue": "Queue Subgraph on Remote",
    "SubgraphFetch_IMAGE": "Fetch Subgraph Output (Image)",
}
