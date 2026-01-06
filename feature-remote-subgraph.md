# Feature: Remote Sub-Graph Execution

## Overview

Execute portions of a workflow on remote ComfyUI instances. The subgraph is automatically extracted from the main workflow by tracing upstream from a queue node.

**Pattern:** (consistent with existing NetDist nodes)
- `RemoteSubgraphQueue` → extracts subgraph, dispatches, returns REMINFO
- `SubgraphFetch_*` → receives REMINFO, fetches specific output

---

## Node Architecture

### 7 Total Nodes:

| Node | Purpose |
|------|---------|
| `RemoteSubgraphQueue` | Extract subgraph, upload inputs, dispatch, return REMINFO |
| `SubgraphFetch_IMAGE` | Fetch IMAGE output from remote |
| `SubgraphFetch_LATENT` | Fetch LATENT output from remote |
| `SubgraphFetch_MASK` | Fetch MASK output from remote |
| `SubgraphFetch_INT` | Fetch INT output from remote |
| `SubgraphFetch_FLOAT` | Fetch FLOAT output from remote |
| `SubgraphFetch_STRING` | Fetch STRING output from remote |

---

## Node Definitions

### RemoteSubgraphQueue (dispatch node)

```
┌─────────────────────────────────┐
│   RemoteSubgraphQueue           │
├─────────────────────────────────┤
│ remote_url: [http://...]        │
│ mode: [local/remote/both]       │
├─────────────────────────────────┤
│ Inputs:                         │
│  → trigger    (any type)        │  ← connects to end of subgraph
├─────────────────────────────────┤
│ Outputs:                        │
│  → remote_info (REMINFO)        │
└─────────────────────────────────┘
│ Hidden:                         │
│  → prompt    (PROMPT)           │
│  → unique_id (UNIQUE_ID)        │
└─────────────────────────────────┘
```

**Behavior:**
- Traces upstream from `trigger` input to identify subgraph
- Detects boundary inputs (whitelisted types)
- Uploads IMAGE/LATENT boundaries to remote
- Builds standalone prompt with injected loaders
- Dispatches to remote `/prompt`
- Returns REMINFO with job_id for fetch nodes

### SubgraphFetch_* (fetch nodes)

```
┌─────────────────────────────────┐
│   SubgraphFetch_IMAGE           │
├─────────────────────────────────┤
│ output_name: "result"           │
├─────────────────────────────────┤
│ Inputs:                         │
│  → remote_info (REMINFO)        │
│  → local_value (IMAGE) optional │  ← for local/both modes
├─────────────────────────────────┤
│ Outputs:                        │
│  → image     (IMAGE)            │
└─────────────────────────────────┘
```

**Behavior:**
- `local` mode (in REMINFO): returns local_value passthrough
- `remote` mode: polls /history, fetches output by output_name
- `both` mode: could return both or compare

---

## Workflow Example

### Main Workflow

```
┌───────────┐     ┌──────────────┐     ┌──────────────┐     ┌─────────────────────┐
│ LoadImage │────→│ VAEEncode    │────→│ KSampler     │────→│ VAEDecode           │
└───────────┘     └──────────────┘     └──────────────┘     └─────────────────────┘
                                                                      │
                        ┌─────────────────────────────────────────────┘
                        ↓
            ┌───────────────────────────┐
            │ RemoteSubgraphQueue       │
            │ remote_url: 192.168.1.68  │
            │ mode: remote              │
            └───────────────────────────┘
                        │
                        ↓ REMINFO
            ┌───────────────────────────┐
            │ SubgraphFetch_IMAGE       │────→ [SaveImage]
            │ output_name: "image_out"  │
            └───────────────────────────┘
```

### What Gets Sent to Remote

The queue node traces upstream and extracts:

```python
{
  "1": {"class_type": "LoadCheckpoint", "inputs": {"ckpt_name": "model.safetensors"}},
  "2": {"class_type": "LoadImage", "inputs": {"image": "uploaded_input.png"}},  # uploaded
  "3": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
  "4": {"class_type": "KSampler", "inputs": {...}},
  "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
  "6": {"class_type": "PreviewImage", "inputs": {"images": ["5", 0]}}  # capture node
}
```

---

## Subgraph Extraction Process

### Step 1: Trace Upstream

Starting from RemoteSubgraphQueue's trigger input, recursively find all upstream nodes:

```python
def extract_subgraph(prompt, start_node_id):
    subgraph_nodes = set()
    to_visit = [start_node_id]

    while to_visit:
        node_id = to_visit.pop()
        if node_id in subgraph_nodes:
            continue
        subgraph_nodes.add(node_id)

        node = prompt[node_id]
        for input_value in node["inputs"].values():
            if is_link(input_value):
                upstream_id = input_value[0]
                to_visit.append(upstream_id)

    return subgraph_nodes
```

### Step 2: Identify Boundary Inputs

Find nodes in subgraph that load external resources:

```python
LOADER_NODES = {
    "LoadImage": ("image", "IMAGE"),
    "LoadLatent": ("latent", "LATENT"),
    # etc.
}

def find_boundary_inputs(prompt, subgraph_nodes):
    boundaries = []
    for node_id in subgraph_nodes:
        node = prompt[node_id]
        if node["class_type"] in LOADER_NODES:
            boundaries.append((node_id, node))
    return boundaries
```

### Step 3: Upload and Inject

For each boundary input:
1. Read the local file
2. Upload to remote via `/upload/image`
3. Update the node's input to reference uploaded filename

### Step 4: Replace Output Node

Replace RemoteSubgraphQueue with a capture node:

```python
# Original: RemoteSubgraphQueue with trigger input from node "5"
# Replace with: PreviewImage capturing that output

prompt[queue_node_id] = {
    "class_type": "PreviewImage",
    "inputs": {"images": trigger_link}
}
```

---

## Boundary Input Handling

### Whitelisted Types:

| Type | Loader Node | Capture Node | Upload Method |
|------|-------------|--------------|---------------|
| `IMAGE` | `LoadImage` | `PreviewImage` | `/upload/image` |
| `LATENT` | `LoadLatentNumpy` | `SaveLatentNumpy` | `/upload/image` (.npy) |
| `MASK` | `LoadImage` | `PreviewImage` | `/upload/image` |
| `INT` | Direct injection | Metadata | In prompt JSON |
| `FLOAT` | Direct injection | Metadata | In prompt JSON |
| `STRING` | Direct injection | Metadata | In prompt JSON |

### Non-Whitelisted (stay as-is):

| Type | Handling |
|------|----------|
| `MODEL` | Remote loads same checkpoint by name |
| `CLIP` | Remote loads same checkpoint by name |
| `VAE` | Remote loads same checkpoint by name |
| `CONDITIONING` | Regenerated on remote |

---

## Execution Flow

```
1. RemoteSubgraphQueue executes
2. Read workflow via hidden "prompt" input
3. Trace upstream from trigger to find subgraph nodes
4. Find boundary inputs (loader nodes for whitelisted types)
5. For each IMAGE/LATENT boundary:
   - Read local file
   - Upload to remote via /upload/image
   - Update prompt with uploaded filename
6. Replace queue node with capture node in prompt
7. Dispatch prompt to remote /prompt
8. Return REMINFO with job_id, remote_url, mode

9. SubgraphFetch_* nodes execute (after receiving REMINFO)
10. If mode == "local": return local_value
11. If mode == "remote":
    - Poll /history for job completion
    - Fetch output by output_name:
      - IMAGE: GET /view?filename=...
      - LATENT: GET /view?filename=...
      - Primitives: Extract from metadata
12. Return fetched value
```

---

## Multiple Outputs

```
[Processing] ────→ [RemoteSubgraphQueue]
                           │
                           ↓ REMINFO
         ┌─────────────────┼─────────────────┐
         ↓                 ↓                 ↓
[SubgraphFetch_IMAGE] [SubgraphFetch_LATENT] [SubgraphFetch_INT]
 output_name: "img"    output_name: "lat"    output_name: "steps"
```

Each fetch node requests its specific output by name from the same job.

---

## Files to Create/Modify

### `nodes/subgraph.py` (NEW)

```python
class RemoteSubgraphQueue:
    TITLE = "Queue Subgraph on Remote"
    CATEGORY = "remote/subgraph"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "trigger": ("*",),  # accepts any type
                "remote_url": ("STRING", {"default": "http://127.0.0.1:8288"}),
                "mode": (["local", "remote", "both"],),
            },
            "hidden": {
                "prompt": "PROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("REMINFO",)
    FUNCTION = "queue"

    def queue(self, trigger, remote_url, mode, prompt, unique_id):
        if mode == "local":
            return ({"mode": "local"},)

        # Extract and dispatch subgraph
        subgraph = extract_subgraph(prompt, unique_id)
        prepared = prepare_for_remote(subgraph, remote_url)
        job_id = dispatch_subgraph(remote_url, prepared)

        return ({
            "mode": mode,
            "remote_url": remote_url,
            "job_id": job_id,
        },)


class SubgraphFetch_IMAGE:
    TITLE = "Fetch Subgraph Output (Image)"
    CATEGORY = "remote/subgraph"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "remote_info": ("REMINFO",),
                "output_name": ("STRING", {"default": "image_out"}),
            },
            "optional": {
                "local_value": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "fetch"

    def fetch(self, remote_info, output_name, local_value=None):
        if remote_info.get("mode") == "local":
            return (local_value,)

        result = fetch_output(
            remote_info["remote_url"],
            remote_info["job_id"],
            output_name,
            "IMAGE"
        )
        return (result,)

# Similar classes for LATENT, MASK, INT, FLOAT, STRING
```

### `core/subgraph.py` (NEW)

```python
def extract_subgraph(prompt, output_node_id):
    """Trace upstream from output node to find all subgraph nodes."""
    ...

def find_boundary_inputs(prompt, subgraph_nodes):
    """Find loader nodes that need upload handling."""
    ...

def prepare_for_remote(prompt, subgraph_nodes, boundaries, remote_url):
    """
    Build standalone prompt:
    - Upload boundary inputs
    - Replace loaders with uploaded filenames
    - Replace output node with capture node
    """
    ...

def dispatch_subgraph(remote_url, prompt):
    """POST prepared prompt to remote /prompt."""
    ...

def fetch_output(remote_url, job_id, output_name, output_type):
    """Poll /history and fetch specific output."""
    ...
```

### `__init__.py` (MODIFY)

```python
from .nodes.subgraph import NODE_CLASS_MAPPINGS as SubgraphNodes
NODE_CLASS_MAPPINGS.update(SubgraphNodes)
```

---

## Implementation Phases

### Phase 1: IMAGE only
- [ ] `RemoteSubgraphQueue` node
- [ ] `SubgraphFetch_IMAGE` node
- [ ] `extract_subgraph()` - trace upstream
- [ ] `prepare_for_remote()` - handle IMAGE boundaries
- [ ] Basic dispatch and fetch

### Phase 2: All types
- [ ] `SubgraphFetch_LATENT` (numpy serialization)
- [ ] `SubgraphFetch_MASK`
- [ ] `SubgraphFetch_INT/FLOAT/STRING` (direct injection)

### Phase 3: Polish
- [ ] Error handling and propagation
- [ ] Timeout configuration
- [ ] Progress reporting
- [ ] Mode "both" comparison logic
- [ ] Caching of uploaded files

---

## Key Design Decisions

1. **Follows existing pattern** - Queue returns REMINFO, Fetch receives it
2. **Auto-detect subgraph** - Trace upstream from trigger, no manual marking
3. **Single dispatch, multiple fetch** - One queue node, many fetch nodes
4. **Typed fetch nodes** - One class per whitelisted type for type safety
5. **Mode control on queue** - local/remote/both, passed via REMINFO
6. **Remote has same models** - MODEL/CLIP/VAE referenced by name
