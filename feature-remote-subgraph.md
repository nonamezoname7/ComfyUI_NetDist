# Feature: Remote Sub-Graph Execution

## Overview

Execute sub-graphs on remote ComfyUI instances with typed boundary nodes. SubgraphInput/Output nodes live **inside** the subgraph to define its contract, making subgraphs self-documenting and testable locally.

---

## Node Architecture

### Typed Node Classes (one per whitelisted type)

| Input Nodes | Output Nodes | Fetch Nodes |
|-------------|--------------|-------------|
| `SubgraphInput_IMAGE` | `SubgraphOutput_IMAGE` | `SubgraphFetch_IMAGE` |
| `SubgraphInput_LATENT` | `SubgraphOutput_LATENT` | `SubgraphFetch_LATENT` |
| `SubgraphInput_MASK` | `SubgraphOutput_MASK` | `SubgraphFetch_MASK` |
| `SubgraphInput_INT` | `SubgraphOutput_INT` | `SubgraphFetch_INT` |
| `SubgraphInput_FLOAT` | `SubgraphOutput_FLOAT` | `SubgraphFetch_FLOAT` |
| `SubgraphInput_STRING` | `SubgraphOutput_STRING` | `SubgraphFetch_STRING` |

Plus: `RemoteSubgraphDispatch` - coordinator node

---

## Node Definitions

### SubgraphInput_* (lives in subgraph AND main workflow)

```
┌─────────────────────────────────┐
│   SubgraphInput_IMAGE           │
├─────────────────────────────────┤
│ input_name: "img1"              │
├─────────────────────────────────┤
│ Inputs:                         │
│  → image     (IMAGE)            │
├─────────────────────────────────┤
│ Outputs:                        │
│  → image     (IMAGE) passthrough│
│  → marker    (SUBGRAPH_MARKER)  │
└─────────────────────────────────┘
```

- **In subgraph**: Marks input boundary, value injected during remote execution
- **In main workflow**: Receives actual value, outputs marker for dispatch ordering
- Passthrough allows local testing of subgraph

### SubgraphOutput_* (lives inside subgraph)

```
┌─────────────────────────────────┐
│   SubgraphOutput_IMAGE          │
├─────────────────────────────────┤
│ output_name: "result"           │
├─────────────────────────────────┤
│ Inputs:                         │
│  → image     (IMAGE)            │
├─────────────────────────────────┤
│ Outputs:                        │
│  → image     (IMAGE) passthrough│
└─────────────────────────────────┘
```

- Marks output boundary in subgraph
- During remote dispatch: replaced with capture node (PreviewImage, SaveLatentNumpy, etc.)
- Passthrough allows local testing

### RemoteSubgraphDispatch (lives in main workflow)

```
┌─────────────────────────────────┐
│   RemoteSubgraphDispatch        │
├─────────────────────────────────┤
│ remote_url: [http://...]        │
│ mode: [local/remote/both]       │
├─────────────────────────────────┤
│ Inputs:                         │
│  → workflow  (JSON)             │
│  → markers   (SUBGRAPH_MARKER)  │  ← from all SubgraphInput_* nodes
├─────────────────────────────────┤
│ Outputs:                        │
│  → remote_info (REMINFO)        │
└─────────────────────────────────┘
```

- Receives markers from all SubgraphInput nodes (enforces execution order)
- Scans prompt to find SubgraphInput nodes and their values
- Uploads input resources to remote
- Dispatches modified workflow to remote
- Returns REMINFO for fetch nodes

### SubgraphFetch_* (lives in main workflow)

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

- Fetches specific output by `output_name` from remote
- In `local` mode: returns local_value passthrough
- In `remote` mode: fetches from remote
- In `both` mode: could return both or compare

---

## Workflow Examples

### Subgraph Definition (saved as workflow JSON)

```
┌──────────────────────┐     ┌──────────────┐     ┌───────────────────────┐
│ SubgraphInput_IMAGE  │────→│ ImageUpscale │────→│ SubgraphOutput_IMAGE  │
│ input_name: "img1"   │     │              │     │ output_name: "result" │
└──────────────────────┘     └──────────────┘     └───────────────────────┘
```

This subgraph:
- Takes one IMAGE input named "img1"
- Outputs one IMAGE output named "result"
- Can be tested locally (nodes passthrough)
- Can be executed remotely via dispatch

### Main Workflow (executes subgraph remotely)

```
┌───────────┐     ┌──────────────────────┐
│ LoadImage │────→│ SubgraphInput_IMAGE  │──marker──┐
└───────────┘     │ input_name: "img1"   │          │
                  └──────────────────────┘          │
                                                    ↓
┌─────────────────────┐     ┌─────────────────────────────┐
│ LoadDiskWorkflowJSON│────→│ RemoteSubgraphDispatch      │
└─────────────────────┘     │ remote_url: 192.168.1.68    │
                            │ mode: remote                │
                            └─────────────────────────────┘
                                          │
                                          ↓ REMINFO
                            ┌─────────────────────────────┐
                            │ SubgraphFetch_IMAGE         │────→ [SaveImage]
                            │ output_name: "result"       │
                            └─────────────────────────────┘
```

---

## Whitelisted Types

### Supported (can serialize/transfer):

| Type | Upload Method | Fetch Method |
|------|---------------|--------------|
| `IMAGE` | `/upload/image` | `/view?filename=...` |
| `LATENT` | `/upload/image` (as .npy) | `/view?filename=...` |
| `MASK` | `/upload/image` | `/view?filename=...` |
| `INT` | JSON in prompt | Job metadata |
| `FLOAT` | JSON in prompt | Job metadata |
| `STRING` | JSON in prompt | Job metadata |

### NOT Supported:

| Type | Reason |
|------|--------|
| `MODEL` | Too large; remote loads its own |
| `CLIP` | Same as MODEL |
| `VAE` | Same as MODEL |
| `CONDITIONING` | Complex structure, tied to CLIP |

---

## Execution Flow

```
1. Main workflow executes SubgraphInput_* nodes
   - Values pass through
   - Markers output for dispatch ordering

2. RemoteSubgraphDispatch executes (blocked until all markers received)
   - Scans prompt for SubgraphInput_* nodes
   - Collects their input values from execution cache
   - Modifies subgraph workflow:
     a. Replace SubgraphInput_* with actual values / uploaded file refs
     b. Replace SubgraphOutput_* with capture nodes (PreviewImage, etc.)
   - Uploads IMAGE/LATENT inputs to remote
   - POSTs modified workflow to remote /prompt
   - Returns REMINFO with job_id

3. SubgraphFetch_* nodes execute (blocked until REMINFO received)
   - Poll remote /history for job completion
   - Fetch outputs by output_name:
     - IMAGE/LATENT: GET /view?filename=...
     - Primitives: extract from job response
   - Return fetched values
```

---

## Files to Create

### `nodes/subgraph.py`

```python
# Typed input nodes
class SubgraphInput_IMAGE: ...
class SubgraphInput_LATENT: ...
class SubgraphInput_MASK: ...
class SubgraphInput_INT: ...
class SubgraphInput_FLOAT: ...
class SubgraphInput_STRING: ...

# Typed output nodes (for inside subgraph)
class SubgraphOutput_IMAGE: ...
class SubgraphOutput_LATENT: ...
class SubgraphOutput_MASK: ...
class SubgraphOutput_INT: ...
class SubgraphOutput_FLOAT: ...
class SubgraphOutput_STRING: ...

# Dispatcher
class RemoteSubgraphDispatch: ...

# Typed fetch nodes
class SubgraphFetch_IMAGE: ...
class SubgraphFetch_LATENT: ...
class SubgraphFetch_MASK: ...
class SubgraphFetch_INT: ...
class SubgraphFetch_FLOAT: ...
class SubgraphFetch_STRING: ...
```

### `core/subgraph_dispatch.py`

```python
def prepare_subgraph_for_remote(workflow, inputs, outputs):
    """
    Modify subgraph workflow for remote execution.
    - Replace SubgraphInput_* with injected values
    - Replace SubgraphOutput_* with capture nodes
    """
    ...

def upload_subgraph_inputs(remote_url, inputs):
    """Upload IMAGE/LATENT inputs to remote."""
    ...

def fetch_subgraph_output(remote_url, job_id, output_name, output_type):
    """Fetch specific output from completed remote job."""
    ...
```

---

## Implementation Phases

### Phase 1: IMAGE only
- [ ] SubgraphInput_IMAGE, SubgraphOutput_IMAGE, SubgraphFetch_IMAGE
- [ ] RemoteSubgraphDispatch
- [ ] Basic upload and fetch for images

### Phase 2: All types
- [ ] LATENT, MASK support (numpy serialization)
- [ ] INT, FLOAT, STRING support (JSON injection)
- [ ] BOOLEAN support

### Phase 3: Polish
- [ ] Error handling and propagation
- [ ] Timeout configuration
- [ ] Progress reporting
- [ ] Caching of uploads

---

## Key Design Decisions

1. **Typed node classes** - One class per whitelisted type for type safety
2. **Subgraph contains Output nodes** - Self-documenting, testable locally
3. **Marker-based ordering** - SubgraphInput markers enforce dispatch waits for all inputs
4. **Separate Fetch nodes** - Allows multiple outputs, clear data flow
5. **Mode control on Dispatch** - local/remote/both for testing flexibility
