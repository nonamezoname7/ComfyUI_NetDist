# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ComfyUI_NetDist is a ComfyUI custom node pack that enables distributed workflow execution across multiple GPUs and networked machines. It provides nodes for queuing jobs on remote ComfyUI instances and fetching results back.

## Architecture

### Core Module (`core/`)
- **dispatch.py**: Handles job dispatching to remote ComfyUI instances via HTTP API. Key functions:
  - `dispatch_to_remote()`: Modifies workflow prompts and sends to remote `/prompt` endpoint
  - `clear_remote_queue()`: Cancels pending jobs from the same client
  - Handles OS path separator conversion between Linux/Windows machines
- **fetch.py**: Polls remote `/history` endpoint for completed jobs and retrieves output images
- **utils.py**: Session ID generation (`GID`), job ID creation, URL normalization

### Node Categories (`nodes/`)
- **simple.py**: Basic dual-GPU setup (`RemoteQueueSimple`, `FetchRemote`)
- **advanced.py**: Multi-machine chaining (`RemoteChainStart`, `RemoteQueueWorker`, `RemoteChainEnd`)
- **images.py**: URL-based image I/O (`LoadImageUrl`, `SaveImageUrl`, `CombineImageBatch`)
- **latents.py**: Latent tensor I/O in multiple formats (numpy, safetensors, kohya npz)
- **workflows.py**: Workflow JSON save/load utilities

### Custom Types
- `REMINFO`: Dict with `remote_url` and `job_id` for tracking remote jobs
- `REMCHAIN`: Dict for chaining multiple remote workers (includes seed, batch, prompt, offsets)
- `JSON`: Workflow prompt data structure

## Key Patterns

### Node Class Structure
All nodes follow ComfyUI's node pattern:
```python
class NodeName:
    TITLE = "Display Name"       # Node display name
    CATEGORY = "remote"          # Menu category
    RETURN_TYPES = ("TYPE",)     # Output types tuple
    FUNCTION = "method_name"     # Method to call

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {...}, "hidden": {...}}

    def method_name(self, ...):
        return (result,)
```

### Remote Communication
All HTTP communication uses the `requests` library with 4-second timeouts for control endpoints and 16-second timeouts for data transfer. Jobs are tracked by client ID (session-unique) and job ID (timestamp-based).

### Input Resource Upload Pattern
When dispatching workflows to remote instances, local file references must be uploaded first. This is handled in `dispatch.py` via `upload_input_images()`.

**Pattern for extending to other node types:**
1. In `upload_input_images()` (or a new similar function), detect nodes by `class_type`
2. Extract the file reference from the node's `inputs` dict
3. Resolve the local path using `folder_paths.get_annotated_filepath()`
4. Upload to remote via ComfyUI's `/upload/image` API (POST multipart form)
5. Update the prompt with the new filename if it changed

**Currently supported:**
- `LoadImage` → uploads via `/upload/image`

**Candidates for future support:**
- `LoadImageMask` → `/upload/image` (same pattern as LoadImage)
- `LoadVideo` (if exists) → would need video upload handling
- Custom nodes that load local files

**ComfyUI Upload API reference:**
```python
# POST /upload/image - multipart form data
files = {"image": (filename, file_handle)}
data = {"type": "input", "overwrite": "true", "subfolder": "optional"}
response = requests.post(f"{remote_url}/upload/image", files=files, data=data)
# Returns: {"name": "actual_filename.png", "subfolder": "", "type": "input"}
```

**File path annotation format:**
- `"filename.png"` → resolves to input directory (default)
- `"filename.png[output]"` → resolves to output directory
- `"filename.png[temp]"` → resolves to temp directory
- `"subfolder/filename.png"` → resolves to subfolder within input directory

## Dependencies

Single external dependency: `requests`

## Testing

No test framework is configured. This is a ComfyUI custom node pack tested by loading into ComfyUI.

## Running

Install into ComfyUI's `custom_nodes/` directory. Requires at least two ComfyUI instances for distributed execution:
```bash
# Primary instance (default port)
python main.py

# Secondary instance on different GPU
python main.py --port 8288 --cuda-device 1

# For network access, add --listen to secondary instance
```
