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
