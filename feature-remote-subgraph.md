# Feature: Remote Sub-Graph Execution

## Overview

Execute portions of a workflow on remote ComfyUI instances. Only `SubgraphOutput_*` marker nodes are needed - the subgraph is automatically extracted from the main workflow by tracing upstream from the output markers.

---

## Simplified Architecture

**Key insight:** The subgraph IS part of the main workflow. We extract it dynamically by:
1. Tracing upstream from `SubgraphOutput_*` nodes
2. Detecting boundary inputs (whitelisted types entering the subgraph)
3. Converting to a standalone prompt with injected loader nodes

**Only output marker nodes needed:**
- `SubgraphOutput_IMAGE`
- `SubgraphOutput_LATENT`
- `SubgraphOutput_MASK`
- `SubgraphOutput_INT`
- `SubgraphOutput_FLOAT`
- `SubgraphOutput_STRING`

---

## Node Definition

### SubgraphOutput_* (one per whitelisted type)

```
┌─────────────────────────────────┐
│   SubgraphOutput_IMAGE          │
├─────────────────────────────────┤
│ remote_url: [http://...]        │
│ mode: [local/remote/both]       │
│ output_name: "result"           │
├─────────────────────────────────┤
│ Inputs:                         │
│  → image     (IMAGE)            │
├─────────────────────────────────┤
│ Outputs:                        │
│  → image     (IMAGE)            │
└─────────────────────────────────┘
│ Hidden:                         │
│  → prompt    (PROMPT)           │
└─────────────────────────────────┘
```

**Behavior:**
- `local` mode: passthrough (returns input image directly)
- `remote` mode: extracts subgraph, dispatches, fetches result
- `both` mode: executes locally AND remotely (for comparison)

---

## Subgraph Extraction Process

### Example Workflow

```
Main workflow prompt:
{
  "1": {"class_type": "LoadCheckpoint", "inputs": {"ckpt_name": "model.safetensors"}},
  "2": {"class_type": "LoadImage", "inputs": {"image": "photo.png"}},
  "3": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
  "4": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "latent_image": ["3", 0], ...}},
  "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
  "6": {"class_type": "SubgraphOutput_IMAGE", "inputs": {"image": ["5", 0], "remote_url": "..."}}
}
```

### Step 1: Trace Upstream from Output

Starting from node "6" (SubgraphOutput_IMAGE), trace all upstream dependencies:
- Node 6 depends on: 5
- Node 5 depends on: 4, 1
- Node 4 depends on: 1, 3
- Node 3 depends on: 2, 1
- Nodes 1, 2 have no upstream dependencies

Subgraph nodes: {1, 2, 3, 4, 5, 6}

### Step 2: Identify Boundary Inputs

Find inputs to subgraph nodes that come from OUTSIDE or are whitelisted entry points:
- Node 2 (LoadImage): `image` = "photo.png" → **IMAGE boundary** (file reference)
- Node 1 (LoadCheckpoint): `ckpt_name` = "model.safetensors" → **NOT whitelisted** (remote loads its own)

Since MODEL/VAE/CLIP aren't whitelisted, they stay as-is (remote has same models).

The only boundary input to handle: the image from LoadImage.

### Step 3: Convert to Standalone Prompt

```python
# Extracted subgraph (sent to remote):
{
  "1": {"class_type": "LoadCheckpoint", "inputs": {"ckpt_name": "model.safetensors"}},
  "2": {"class_type": "LoadImage", "inputs": {"image": "uploaded_photo.png"}},  # uploaded file
  "3": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
  "4": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "latent_image": ["3", 0], ...}},
  "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
  "6": {"class_type": "PreviewImage", "inputs": {"images": ["5", 0]}}  # replaced output node
}
```

**Changes made:**
1. LoadImage now references uploaded file
2. SubgraphOutput_IMAGE replaced with PreviewImage (capture node)

---

## Boundary Input Handling

### For Each Whitelisted Type:

| Type | At Boundary | Injection | Capture |
|------|-------------|-----------|---------|
| `IMAGE` | Upload to remote | `LoadImage` with uploaded filename | `PreviewImage` |
| `LATENT` | Upload .npy file | `LoadLatentNumpy` with uploaded filename | `SaveLatentNumpy` |
| `MASK` | Upload to remote | `LoadImage` (single channel) | `PreviewImage` |
| `INT` | Direct injection | Replace link with value | Store in metadata |
| `FLOAT` | Direct injection | Replace link with value | Store in metadata |
| `STRING` | Direct injection | Replace link with value | Store in metadata |

### Non-Whitelisted Types (stay as-is):

| Type | Handling |
|------|----------|
| `MODEL` | Remote loads same checkpoint by name |
| `CLIP` | Remote loads same checkpoint by name |
| `VAE` | Remote loads same checkpoint by name |
| `CONDITIONING` | Included in subgraph, regenerated on remote |

---

## Execution Flow

```
1. SubgraphOutput_* node executes
2. Read current workflow via hidden "prompt" input
3. Trace upstream to identify subgraph nodes
4. Identify boundary inputs (whitelisted types from outside)
5. For each IMAGE/LATENT boundary:
   - Upload to remote via /upload/image
   - Note the uploaded filename
6. Build standalone prompt:
   - Copy subgraph nodes
   - Replace boundary inputs with loader nodes (uploaded files)
   - Replace SubgraphOutput_* with capture node
7. Dispatch prompt to remote /prompt
8. Poll /history for completion
9. Fetch result:
   - IMAGE: GET /view?filename=...
   - LATENT: GET /view?filename=...
   - Primitives: Extract from job metadata
10. Return fetched value
```

---

## Multiple Outputs

For multiple outputs, use multiple SubgraphOutput_* nodes:

```
[Processing] → [SubgraphOutput_IMAGE "img_result"]
           ↘ → [SubgraphOutput_LATENT "lat_result"]
```

**Coordination:**
- First SubgraphOutput_* to execute dispatches the subgraph
- Others detect same job_id and just fetch their specific output
- Use shared REMINFO via node metadata or execution cache

---

## Files to Create/Modify

### `nodes/subgraph.py` (NEW)

```python
class SubgraphOutput_IMAGE:
    TITLE = "Subgraph Output (Image)"
    CATEGORY = "remote/subgraph"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "remote_url": ("STRING", {"default": "http://127.0.0.1:8288"}),
                "mode": (["local", "remote", "both"],),
                "output_name": ("STRING", {"default": "image_out"}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"

    def execute(self, image, remote_url, mode, output_name, prompt, unique_id):
        if mode == "local":
            return (image,)

        # Extract subgraph, dispatch, fetch
        subgraph = extract_subgraph(prompt, unique_id)
        prepared = prepare_for_remote(subgraph, remote_url)
        job_id = dispatch_subgraph(remote_url, prepared)
        result = fetch_output(remote_url, job_id, output_name, "IMAGE")

        if mode == "both":
            # Could return both or log comparison
            pass

        return (result,)

# Similar classes for LATENT, MASK, INT, FLOAT, STRING
```

### `core/subgraph.py` (NEW)

```python
def extract_subgraph(prompt, output_node_id):
    """
    Trace upstream from output node to extract subgraph.
    Returns: (subgraph_nodes, boundary_inputs)
    """
    ...

def prepare_for_remote(subgraph, boundary_inputs, remote_url):
    """
    Convert subgraph to standalone prompt:
    - Upload boundary inputs
    - Replace with loader nodes
    - Replace output markers with capture nodes
    """
    ...

def dispatch_subgraph(remote_url, prompt, job_id):
    """POST prepared prompt to remote."""
    ...

def fetch_output(remote_url, job_id, output_name, output_type):
    """Fetch specific output from completed job."""
    ...
```

---

## Implementation Phases

### Phase 1: IMAGE only
- [ ] `SubgraphOutput_IMAGE` node
- [ ] `extract_subgraph()` - trace upstream
- [ ] `prepare_for_remote()` - handle IMAGE boundaries
- [ ] Basic dispatch and fetch

### Phase 2: All types
- [ ] LATENT support (numpy serialization)
- [ ] MASK support
- [ ] Primitive types (direct injection)

### Phase 3: Multiple outputs
- [ ] Coordinate multiple SubgraphOutput_* nodes
- [ ] Shared dispatch, individual fetch

### Phase 4: Polish
- [ ] Error handling
- [ ] Timeout configuration
- [ ] Progress reporting
- [ ] Mode "both" comparison logic

---

## Key Design Decisions

1. **No SubgraphInput_* nodes** - boundaries detected automatically
2. **Subgraph extracted from main workflow** - no separate JSON needed
3. **Dynamic prompt reading** - uses hidden `prompt` input like current nodes
4. **Typed output nodes** - one class per whitelisted type
5. **Mode control** - local/remote/both for testing flexibility
6. **Remote has same models** - MODEL/CLIP/VAE referenced by name, not transferred
