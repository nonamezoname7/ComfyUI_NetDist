# ComfyUI Sub-Graph Execution Model Reference

This document provides a reference for understanding ComfyUI's sub-graph execution model, which will be used for planning remote sub-graph execution in NetDist.

## Overview

ComfyUI supports dynamic sub-graph execution through "Node Expansion". Nodes can dynamically generate new graph structures at runtime that get merged into the execution flow. This enables flow control (loops, conditionals) and can be leveraged for remote sub-graph execution.

---

## 1. Key Data Structures

### DynamicPrompt (`comfy_execution/graph.py`)

Manages the original prompt and dynamically created "ephemeral" nodes:

```python
class DynamicPrompt:
    def __init__(self, original_prompt):
        self.original_prompt = original_prompt      # User's original workflow
        self.ephemeral_prompt = {}                  # Dynamically created nodes
        self.ephemeral_parents = {}                 # Parent tracking for ephemeral nodes
        self.ephemeral_display = {}                 # UI display mapping
```

**Key methods:**
- `get_node(node_id)` - Returns node from ephemeral_prompt first, then original_prompt
- `add_ephemeral_node(node_id, node_info, parent_id, display_id)` - Adds dynamically created node
- `get_real_node_id(node_id)` - Traverses parent chain to find original node
- `all_node_ids()` - Returns union of original and ephemeral node IDs

### ExecutionBlocker (`comfy_execution/graph_utils.py`)

Used to conditionally block execution paths:

```python
class ExecutionBlocker:
    """Return this from a node to block downstream execution."""
    def __init__(self, message):
        self.message = message  # None = silent block
```

### GraphBuilder (`comfy_execution/graph_utils.py`)

Utility for programmatically constructing graph structures:

```python
class GraphBuilder:
    def __init__(self, prefix=None):
        self.prefix = prefix or GraphBuilder.alloc_prefix()
        self.nodes = {}
        self.id_gen = 1

    def node(self, class_type, id=None, **kwargs):
        # Creates Node with unique prefixed ID

    def lookup_node(self, id):
        # Find existing node by ID

    def finalize(self):
        # Returns JSON-serializable graph dictionary
```

**Node class:**
```python
class Node:
    def __init__(self, id, class_type, inputs):
        self.id = id
        self.class_type = class_type
        self.inputs = inputs
        self.override_display_id = None

    def out(self, index):
        # Returns link reference [node_id, output_index]
        return [self.id, index]

    def set_input(self, key, value):
        self.inputs[key] = value
```

### ExecutionList (`comfy_execution/graph.py`)

Manages execution order with caching:

```python
class ExecutionList(TopologicalSort):
    def add_node(node_id, include_lazy=False, subgraph_nodes=None)
    def add_strong_link(from_node_id, from_socket, to_node_id)
    def stage_node_execution()      # Select next node
    def complete_node_execution()   # Mark done
    def unstage_node_execution()    # Return to pending
    def add_external_block(node_id) # Block for async
```

---

## 2. Execution Result States

```python
class ExecutionResult(Enum):
    SUCCESS = 0   # Node completed, move to next
    FAILURE = 1   # Node failed, abort execution
    PENDING = 2   # Node waiting (subgraph/async), return to queue
```

---

## 3. Sub-Graph Expansion Flow

### Step 1: Node Returns Expansion

A node triggers sub-graph expansion by returning a dict with `expand` key:

```python
def my_function(self, **kwargs):
    g = GraphBuilder()

    # Build the sub-graph
    node1 = g.node("SomeNodeType", input1=value1)
    node2 = g.node("AnotherNode", input1=node1.out(0))

    return {
        "result": (node2.out(0), other_output),  # Links to expanded outputs
        "expand": g.finalize(),                   # Graph definition
    }
```

### Step 2: Processing in execute() (`execution.py`)

The `get_output_from_returns()` function detects expansion:

```python
def get_output_from_returns(return_values, obj):
    for r in return_values:
        if isinstance(r, dict) and 'expand' in r:
            has_subgraph = True
            new_graph = r['expand']
            result = r.get("result", None)
            subgraph_results.append((new_graph, result))
```

### Step 3: Adding Ephemeral Nodes

In `execute()` (lines 538-572):

```python
if has_subgraph:
    for new_graph, node_outputs in output_data:
        for node_id, node_info in new_graph.items():
            display_id = node_info.get("override_display_id", unique_id)
            dynprompt.add_ephemeral_node(node_id, node_info, unique_id, display_id)

            # Check if output node
            class_def = nodes.NODE_CLASS_MAPPINGS[node_info["class_type"]]
            if hasattr(class_def, 'OUTPUT_NODE') and class_def.OUTPUT_NODE:
                new_output_ids.append(node_id)

    # Setup subcaches for expanded nodes
    for cache in caches.all:
        subcache = await cache.ensure_subcache_for(unique_id, new_node_ids)

    # Add to execution list
    for node_id in new_output_ids:
        execution_list.add_node(node_id)

    pending_subgraph_results[unique_id] = cached_outputs
    return (ExecutionResult.PENDING, None, None)
```

### Step 4: Resolving Pending Results

When the expanding node is re-executed after subgraph completes:

```python
elif unique_id in pending_subgraph_results:
    cached_results = pending_subgraph_results[unique_id]
    resolved_outputs = []

    for is_subgraph, result in cached_results:
        if is_subgraph:
            for r in result:
                if is_link(r):
                    source_node, source_output = r[0], r[1]
                    node_cached = execution_list.get_cache(source_node, unique_id)
                    resolved_output.append(node_cached.outputs[source_output])
```

---

## 4. Link Format

References to node outputs use the format:
```python
[node_id, output_index]  # e.g., ["node_123", 0]
```

Helper function:
```python
def is_link(val):
    return isinstance(val, list) and len(val) == 2 and isinstance(val[1], int)
```

---

## 5. Async Node Execution Pattern

For parallel/async operations:

```python
if has_pending_tasks:
    pending_async_nodes[unique_id] = output_data
    unblock = execution_list.add_external_block(unique_id)

    async def await_completion():
        await asyncio.gather(*tasks, return_exceptions=True)
        unblock()  # Release the block when done

    asyncio.create_task(await_completion())
    return (ExecutionResult.PENDING, None, None)
```

---

## 6. Caching for Sub-Graphs

### HierarchicalCache (`comfy_execution/caching.py`)

Supports nested caches for ephemeral nodes:

```python
class HierarchicalCache(BasicCache):
    def _get_cache_for(self, node_id):
        # Traverses parent chain to find correct subcache

    async def ensure_subcache_for(self, node_id, children_ids):
        # Creates subcache for expanded nodes
```

---

## 7. Example: While Loop Implementation

From `tests/execution/testing_nodes/testing-pack/flow_control.py`:

```python
class TestWhileLoopClose:
    def while_loop_close(self, condition, dynprompt=None, unique_id=None, **kwargs):
        if not condition:
            # Loop done - return values directly
            return tuple(values)

        # Continue looping - clone and expand graph
        graph = GraphBuilder()

        # Clone contained nodes
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.node(original_node["class_type"], id=node_id)
            node.set_override_display_id(node_id)

        # Reconnect inputs
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.lookup_node(node_id)
            for k, v in original_node["inputs"].items():
                if is_link(v) and v[0] in contained:
                    parent = graph.lookup_node(v[0])
                    node.set_input(k, parent.out(v[1]))
                else:
                    node.set_input(k, v)

        return {
            "result": tuple(my_clone.out(x) for x in range(NUM_SOCKETS)),
            "expand": graph.finalize(),
        }
```

---

## 8. Key Files

| File | Purpose |
|------|---------|
| `execution.py` | Main execution loop, node execution, subgraph handling |
| `comfy_execution/graph.py` | DynamicPrompt, ExecutionList, TopologicalSort |
| `comfy_execution/graph_utils.py` | GraphBuilder, Node, is_link(), ExecutionBlocker |
| `comfy_execution/caching.py` | HierarchicalCache, subcache management |

---

## 9. Implications for Remote Sub-Graph Execution

Key observations for implementing remote execution:

1. **Serialization Ready**: `GraphBuilder.finalize()` produces JSON-serializable graphs

2. **Node ID Prefixing**: GraphBuilder's prefix system ensures unique IDs across expansions

3. **Link References**: `[node_id, output_index]` format already used for cross-node references

4. **Pending Pattern**: Return `ExecutionResult.PENDING` + use `add_external_block()` for async waiting

5. **Result Storage**: Use pattern similar to `pending_subgraph_results` dict

6. **Display Mapping**: `override_display_id` maps expanded nodes to original for UI

7. **Cache Hierarchy**: `ensure_subcache_for()` creates isolated caches for expanded nodes

---

## 10. Potential Remote Execution Approach

```python
# Conceptual flow for remote sub-graph execution:

def remote_subgraph_node(self, subgraph_prompt, remote_url, **kwargs):
    # 1. Build graph with remote execution wrapper
    g = GraphBuilder()

    # 2. Add nodes that will execute remotely
    # (serialize subgraph_prompt and send to remote)

    # 3. Return pending with link to result node
    return {
        "result": (result_node.out(0),),
        "expand": g.finalize(),
    }

    # 4. Use add_external_block() pattern to wait for remote
    # 5. Fetch results and inject into cache when complete
```
