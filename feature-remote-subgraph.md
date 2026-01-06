# Feature: Remote Sub-Graph Execution

## Overview

Add a node that executes a sub-graph on a remote ComfyUI instance. The node lives **OUTSIDE** the subgraph and acts as a bridge - accepting inputs, dispatching the subgraph to remote, and returning outputs.

---

## UX Design

### Node: `RemoteSubgraphExecute`

```
┌─────────────────────────────────┐
│   Remote Subgraph Execute       │
├─────────────────────────────────┤
│ remote_url: [http://...]        │
│ workflow:   [JSON input]        │
├─────────────────────────────────┤
│ Inputs:                         │
│  → image_in     (IMAGE)         │
│  → latent_in    (LATENT)        │
│  → mask_in      (MASK)          │
│  → int_in       (INT)           │
│  → float_in     (FLOAT)         │
│  → string_in    (STRING)        │
├─────────────────────────────────┤
│ Outputs:                        │
│  → image_out    (IMAGE)         │
│  → latent_out   (LATENT)        │
│  → mask_out     (MASK)          │
│  → int_out      (INT)           │
│  → float_out    (FLOAT)         │
│  → string_out   (STRING)        │
└─────────────────────────────────┘
```

### Node Placement

The `RemoteSubgraphExecute` node sits **outside** the subgraph as one of its inputs/outputs. This allows:
- Same subgraph to be reused locally or remotely
- Clean separation between workflow logic and remote execution
- Multiple remote instances can execute same subgraph

### Subgraph Convention

The subgraph must have specially-named marker nodes to define boundaries:
- **Input markers**: `SubgraphInput` nodes with typed outputs
- **Output markers**: `SubgraphOutput` nodes with typed inputs

---

## Whitelisted Types

### Supported (can serialize/transfer):

| Type | Serialization Method |
|------|---------------------|
| `IMAGE` | Upload via `/upload/image`, fetch via `/view` |
| `LATENT` | Numpy `.npy` file, upload/download |
| `MASK` | Same as IMAGE (single channel) |
| `INT` | JSON primitive |
| `FLOAT` | JSON primitive |
| `STRING` | JSON primitive |
| `BOOLEAN` | JSON primitive |

### NOT Supported (remote loads its own):

| Type | Reason |
|------|--------|
| `MODEL` | Too large to transfer; remote has its own checkpoints |
| `CLIP` | Same as MODEL |
| `VAE` | Same as MODEL |
| `CONDITIONING` | Complex nested structure, tied to specific CLIP |
| Custom types | Unless explicitly added to whitelist |

---

## Architecture

### Files to Create

1. **`nodes/subgraph.py`** (NEW)
   - `RemoteSubgraphExecute` - main execution node
   - `SubgraphInput` - marker node for subgraph inputs
   - `SubgraphOutput` - marker node for subgraph outputs

2. **`core/subgraph_dispatch.py`** (NEW)
   - `prepare_subgraph_for_remote()` - inject inputs, configure outputs
   - `dispatch_subgraph()` - upload resources + send prompt
   - `fetch_subgraph_results()` - retrieve outputs from remote

### Files to Modify

3. **`core/dispatch.py`**
   - Generalize `upload_input_images()` for reuse

4. **`__init__.py`**
   - Register new nodes

---

## Execution Flow

```
1. User connects inputs to RemoteSubgraphExecute node
2. Node receives workflow JSON + input values

3. prepare_subgraph_for_remote():
   - Find SubgraphInput nodes in workflow
   - Replace with actual input values (or upload and reference)
   - Find SubgraphOutput nodes
   - Replace with capture nodes (PreviewImage, SaveLatentNumpy, etc.)

4. Upload resources:
   - IMAGE inputs → POST /upload/image
   - LATENT inputs → POST /upload/image (as .npy)

5. dispatch_subgraph():
   - POST modified workflow to remote /prompt
   - Store job_id for tracking

6. Poll for completion:
   - GET /history until job appears with outputs

7. fetch_subgraph_results():
   - IMAGE outputs → GET /view?filename=...
   - LATENT outputs → GET /view?filename=...
   - Primitives → extract from job metadata

8. Return outputs to local graph
```

---

## Marker Node Design

### SubgraphInput

```python
class SubgraphInput:
    """
    Marks an input boundary in a subgraph.
    When executed remotely, this node is replaced with the actual input value.
    """
    TITLE = "Subgraph Input"
    CATEGORY = "remote/subgraph"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "input_name": ("STRING", {"default": "image_in"}),
                "input_type": (["IMAGE", "LATENT", "MASK", "INT", "FLOAT", "STRING"],),
            }
        }

    # Dynamic return type based on input_type selection
    RETURN_TYPES = ("*",)  # Wildcard, actual type determined at runtime
    FUNCTION = "passthrough"

    def passthrough(self, input_name, input_type):
        # Returns placeholder; replaced during remote prep
        return (None,)
```

### SubgraphOutput

```python
class SubgraphOutput:
    """
    Marks an output boundary in a subgraph.
    When executed remotely, this captures the output for return.
    """
    TITLE = "Subgraph Output"
    CATEGORY = "remote/subgraph"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "output_name": ("STRING", {"default": "image_out"}),
                "value": ("*",),  # Accepts any whitelisted type
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "capture"

    def capture(self, output_name, value):
        # Value is captured and returned to host
        return {}
```

---

## Implementation Phases

### Phase 1: Basic Infrastructure
- [ ] Create `SubgraphInput` and `SubgraphOutput` marker nodes
- [ ] Create `RemoteSubgraphExecute` with fixed typed slots
- [ ] Support IMAGE type only (simplest case)
- [ ] Basic dispatch and fetch

### Phase 2: Full Type Support
- [ ] Add LATENT support (numpy serialization)
- [ ] Add MASK support
- [ ] Add primitive types (INT, FLOAT, STRING, BOOLEAN)
- [ ] Handle multiple inputs/outputs of same type

### Phase 3: Polish
- [ ] Better error handling and logging
- [ ] Progress reporting during remote execution
- [ ] Caching of uploaded resources
- [ ] Timeout configuration

---

## Example Usage

### Main Workflow (runs locally)

```
[LoadImage] ─────────────────────┐
                                 ↓
                    ┌────────────────────────┐
                    │ RemoteSubgraphExecute  │
                    │  remote_url: 192.168.1.68:8199
                    │  workflow: [JSON]      │
                    └────────────────────────┘
                                 ↓
                          [SaveImage]
```

### Subgraph (saved as JSON, runs on remote)

```
[SubgraphInput] ──→ [ImageUpscale] ──→ [SubgraphOutput]
   input_name: "image_in"              output_name: "image_out"
```

---

## Key Considerations

1. **Resource Upload**: Reuse pattern from `upload_input_images()`
2. **Polling**: Use existing `wait_for_job()` from `fetch.py`
3. **Type Safety**: Validate connected types match marker declarations
4. **Error Propagation**: Surface remote errors to local UI
5. **Timeout**: Configurable for long-running subgraphs
6. **Idempotency**: Same inputs should produce same outputs (caching)

---

## Open Questions

1. Should multiple inputs/outputs of same type use numbered slots (`image_in_1`, `image_in_2`) or named slots?
2. How to handle subgraph validation before execution (check all marker nodes exist)?
3. Should there be a "dry run" mode that validates without executing?
