from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from . import __version__
from .schema import GRAPH_FORMAT

_ANCHOR_SCHEMA = "spider-source-anchor/1"
_ATTRIBUTE_SCHEMA = "spider-attributes/1"
_SOURCE_STATUS = {"present", "missing", "synthetic"}
_ANCHOR_ORIGINS = {"exact", "cfg_fallback", "synthetic", "missing"}
_TYPE_FAMILIES = {"address", "boolean", "integer", "fixed_bytes", "dynamic_bytes", "string", "fixed_point", "array", "mapping", "contract", "struct", "enum", "function", "user_defined", "type_meta", "unknown", "UNK"}
_VISIBILITIES = {"public", "external", "internal", "private", "NA", "MISSING", "UNK"}
_DATA_LOCATIONS = {"storage", "memory", "calldata", "transient", "NA", "MISSING", "UNK"}
_MUTABILITIES = {"payable", "view", "pure", "nonpayable", "NA", "MISSING", "UNK"}
_OPERATOR_FAMILIES = {"assignment", "arithmetic", "comparison", "logical", "bitwise", "condition", "return", "NA", "UNK"}
_LITERAL_CATEGORIES = {"bool", "zero", "one", "numeric", "address", "string", "bytes", "other", "NA", "UNK"}


def _validate_extensions(nodes: dict[str, dict[str, Any]], metadata: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if metadata.get("attribute_schema") == _ATTRIBUTE_SCHEMA:
        keys = metadata.get("learnable_attribute_keys")
        if not isinstance(keys, list) or len(keys) != len(set(keys)) or not all(isinstance(key, str) for key in keys):
            errors.append("invalid learnable attribute key list")
        for node in nodes.values():
            if node.get("visibility") is not None and node.get("visibility") not in _VISIBILITIES:
                errors.append("invalid visibility attribute")
            if node.get("type_family") is not None and node.get("type_family") not in _TYPE_FAMILIES:
                errors.append("invalid type_family attribute")
            if node.get("data_location") is not None and node.get("data_location") not in _DATA_LOCATIONS:
                errors.append("invalid data_location attribute")
            if node.get("state_mutability") is not None and node.get("state_mutability") not in _MUTABILITIES:
                errors.append("invalid state_mutability attribute")
            if node.get("operator_family") is not None and node.get("operator_family") not in _OPERATOR_FAMILIES:
                errors.append("invalid operator_family attribute")
            if node.get("literal_category") is not None and node.get("literal_category") not in _LITERAL_CATEGORIES:
                errors.append("invalid literal_category attribute")
            if node.get("type_bit_width") is not None and (not isinstance(node["type_bit_width"], int) or node["type_bit_width"] <= 0):
                errors.append("invalid type_bit_width attribute")
            if node.get("container_depth") is not None and (not isinstance(node["container_depth"], int) or node["container_depth"] < 0):
                errors.append("invalid container_depth attribute")
            for key in ("is_constant", "is_immutable", "is_virtual", "is_override"):
                if node.get(key) is not None and not isinstance(node[key], bool):
                    errors.append(f"invalid {key} attribute")

    if metadata.get("source_anchor_schema") != _ANCHOR_SCHEMA:
        return errors
    manifest = metadata.get("source_files")
    if not isinstance(manifest, list):
        errors.append("source_files manifest is missing")
        return errors
    file_ids = [entry.get("file_id") for entry in manifest if isinstance(entry, dict)]
    if file_ids != list(range(len(manifest))):
        errors.append("source file IDs are not contiguous")
    by_id: dict[int, dict[str, Any]] = {}
    for entry in manifest:
        if not isinstance(entry, dict):
            errors.append("invalid source file manifest entry")
            continue
        file_id = entry.get("file_id")
        if isinstance(file_id, int):
            by_id[file_id] = entry
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            errors.append("invalid source file path")
        if not isinstance(entry.get("byte_length"), int) or entry.get("byte_length", -1) < 0:
            errors.append("invalid source file byte length")
        if not isinstance(entry.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")) is None:
            errors.append("invalid source file sha256")
        if entry.get("encoding") != "utf-8":
            errors.append("unsupported source file encoding")
    for node in nodes.values():
        filename = node.get("file")
        file_id = node.get("file_id")
        if filename is None:
            if file_id is not None:
                errors.append("spanless node has a file ID")
        elif not isinstance(file_id, int) or file_id not in by_id:
            errors.append("source node references an unknown file ID")
        start, end = node.get("byte_start"), node.get("byte_end")
        if (start is None) != (end is None):
            errors.append("incomplete byte_start/byte_end source anchor")
        if start is not None and (not isinstance(start, int) or not isinstance(end, int) or start < 0 or start > end):
            errors.append("invalid byte_start/byte_end source anchor")
        if isinstance(file_id, int) and file_id in by_id and node.get("byte_end") is not None and node["byte_end"] > by_id[file_id]["byte_length"]:
            errors.append("source byte span is out of bounds")
        if node.get("source_mapping_status") not in _SOURCE_STATUS:
            errors.append("invalid source mapping status")
        if node.get("anchor_origin") not in _ANCHOR_ORIGINS:
            errors.append("invalid source anchor origin")
    return errors


def _control_edges(vertices: set[str], entry: str, exit_: str, edges: list[dict[str, Any]]) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
    successors = {vertex: set() for vertex in vertices}
    predecessors = {vertex: set() for vertex in vertices}
    for edge in edges:
        if edge["label"] == "CFG" and edge["source"] in vertices and edge["target"] in vertices:
            successors[edge["source"]].add(edge["target"])
            predecessors[edge["target"]].add(edge["source"])
    dominators = {vertex: ({entry} if vertex == entry else set(vertices)) for vertex in vertices}
    changed = True
    while changed:
        changed = False
        for vertex in vertices - {entry}:
            value = {vertex} | (set.intersection(*(dominators[parent] for parent in predecessors[vertex])) if predecessors[vertex] else set())
            if value != dominators[vertex]:
                dominators[vertex], changed = value, True
    postdominators = {vertex: ({exit_} if vertex == exit_ else set(vertices)) for vertex in vertices}
    changed = True
    while changed:
        changed = False
        for vertex in vertices - {exit_}:
            value = {vertex} | (set.intersection(*(postdominators[child] for child in successors[vertex])) if successors[vertex] else set())
            if value != postdominators[vertex]:
                postdominators[vertex], changed = value, True
    dominate = {(max(dominators[vertex] - {vertex}, key=lambda parent: len(dominators[parent])), vertex) for vertex in vertices if dominators[vertex] - {vertex}}
    post_dominate = {(max(postdominators[vertex] - {vertex}, key=lambda child: len(postdominators[child])), vertex) for vertex in vertices if postdominators[vertex] - {vertex}}
    immediate_postdominator = {target: source for source, target in post_dominate}
    blocks = vertices - {entry, exit_}
    cdg: set[tuple[str, str]] = set()
    for branch in blocks:
        if len(successors[branch]) < 2:
            continue
        for child in successors[branch]:
            runner, seen = child, set()
            while runner != immediate_postdominator.get(branch) and runner not in seen:
                seen.add(runner)
                if runner in blocks:
                    cdg.add((branch, runner))
                runner = immediate_postdominator.get(runner, immediate_postdominator.get(branch))
    return dominate, post_dominate, cdg


def validate(graph: dict[str, Any]) -> list[str]:
    """Return structural CPG/XCFG violations; an empty list proves the checked invariants."""
    errors: list[str] = []
    nodes = {node.get("id"): node for node in graph.get("nodes", [])}
    edges = graph.get("links", [])
    metadata = graph.get("graph", {})
    if metadata.get("format") != GRAPH_FORMAT:
        errors.append("unexpected graph format")
    if "input_kind" in metadata or "input_sources" in metadata:
        input_kind = metadata.get("input_kind")
        input_sources = metadata.get("input_sources")
        if input_kind not in {"file", "directory"}:
            errors.append("invalid input kind")
        if not isinstance(input_sources, list) or not all(isinstance(source, str) and source for source in input_sources):
            errors.append("invalid input source list")
        elif input_sources != sorted(set(input_sources)):
            errors.append("invalid input source list")
        elif input_kind == "file" and (len(input_sources) != 1 or input_sources[0] != metadata.get("source")):
            errors.append("file input metadata does not match source")
        elif input_kind == "directory" and metadata.get("scope") != "project":
            errors.append("directory input must have project scope")
    if len(nodes) != len(graph.get("nodes", [])):
        errors.append("node IDs are not unique")
    errors.extend(_validate_extensions(nodes, metadata))
    if any(edge.get("source") not in nodes or edge.get("target") not in nodes for edge in edges):
        errors.append("edge endpoint is missing")
        return errors
    if len({json.dumps(edge, sort_keys=True) for edge in edges}) != len(edges):
        errors.append("duplicate edge")
    edges_by_label: dict[str, list[dict[str, Any]]] = {}
    outgoing: dict[tuple[str, str], list[dict[str, Any]]] = {}
    edge_triples: set[tuple[str, str, str]] = set()
    contextual_edges: set[tuple[str, str, str, str | None]] = set()
    for edge in edges:
        edges_by_label.setdefault(edge["label"], []).append(edge)
        outgoing.setdefault((edge["label"], edge["source"]), []).append(edge)
        edge_triples.add((edge["label"], edge["source"], edge["target"]))
        contextual_edges.add((edge["label"], edge["source"], edge["target"], edge.get("callsite")))
    orders = [node.get("order") for node in nodes.values()]
    if any(not isinstance(order, int) for order in orders) or sorted(orders) != list(range(len(nodes))):
        errors.append("node order is not unique and contiguous")
    if metadata.get("node_types") != sorted({node["label"] for node in nodes.values()}):
        errors.append("node type vocabulary does not match nodes")
    if metadata.get("edge_types") != sorted({edge["label"] for edge in edges}):
        errors.append("edge type vocabulary does not match edges")

    contains = [(edge["source"], edge["target"]) for edge in edges if edge["label"] == "CONTAINS"]
    ast_parent = {target: source for source, target in ((edge["source"], edge["target"]) for edge in edges if edge["label"] == "AST")}
    function_labels = {"FUNCTION", "CONSTRUCTOR", "FALLBACK", "RECEIVE", "STATE_INITIALIZER", "SOLIDITY_MODIFIER"}
    function_nodes = {node_id for node_id, node in nodes.items() if node["label"] in function_labels}
    owner = {child: function for function, child in contains if function in function_nodes}

    def owning_function(node_id: str | None) -> str | None:
        seen: set[str] = set()
        while node_id and node_id not in seen:
            seen.add(node_id)
            if node_id in function_nodes:
                return node_id
            if node_id in owner:
                return owner[node_id]
            node_id = ast_parent.get(node_id)
        return None

    flows: dict[str, tuple[str, str, str, str, set[str]]] = {}
    flow_labels = {"FUNCTION_ENTRY", "FUNCTION_EXIT", "FUNCTION_BODY_ENTRY", "FUNCTION_BODY_EXIT", "BASIC_BLOCK", "CONTROL_STRUCTURE", "MODIFIER_PLACEHOLDER", "TRY", "CATCH"}
    for function in function_nodes:
        children = {child for parent, child in contains if parent == function}
        entries = [child for child in children if nodes[child]["label"] == "FUNCTION_ENTRY"]
        exits = [child for child in children if nodes[child]["label"] == "FUNCTION_EXIT"]
        if len(entries) != 1 or len(exits) != 1:
            errors.append(f"{function}: expected one FUNCTION_ENTRY and FUNCTION_EXIT")
            continue
        entry, exit_ = entries[0], exits[0]
        body_entries = [child for child in children if nodes[child]["label"] == "FUNCTION_BODY_ENTRY"]
        body_exits = [child for child in children if nodes[child]["label"] == "FUNCTION_BODY_EXIT"]
        control_entry, control_exit = (body_entries[0], body_exits[0]) if len(body_entries) == len(body_exits) == 1 else (entry, exit_)
        vertices = {control_entry, control_exit} | {child for child in children if nodes[child]["label"] in flow_labels and nodes[child]["label"] not in {"FUNCTION_ENTRY", "FUNCTION_EXIT", "FUNCTION_BODY_ENTRY", "FUNCTION_BODY_EXIT"}}
        flows[function] = entry, exit_, control_entry, control_exit, vertices
        expected = _control_edges(vertices, control_entry, control_exit, edges)
        for label, expected_edges in zip(("DOMINATE", "POST_DOMINATE", "CDG"), expected):
            actual = {(edge["source"], edge["target"]) for edge in edges_by_label.get(label, []) if edge["source"] in vertices and edge["target"] in vertices}
            if actual != expected_edges:
                errors.append(f"{function}: {label} does not match CFG")

    for edge in edges:
        if edge["label"] != "REACHING_DEF":
            continue
        source_function, target_function = owning_function(edge["source"]), owning_function(edge["target"])
        if not edge.get("variable") or source_function is None or source_function != target_function:
            errors.append("invalid REACHING_DEF")
    for edge in (edge for edge in edges if edge["label"] in {"TRUE_BRANCH", "FALSE_BRANCH"}):
        if nodes[edge["source"]]["label"] != "CONTROL_STRUCTURE" or nodes[edge["source"]].get("solidity_cfg_type") not in {"IF", "IFLOOP"} or ("CFG", edge["source"], edge["target"]) not in edge_triples:
            errors.append("invalid branch edge")

    for try_node in (node_id for node_id, node in nodes.items() if node["label"] == "TRY"):
        cfg_targets = {edge["target"] for edge in outgoing.get(("CFG", try_node), [])}
        expected_failure = {target for target in cfg_targets if nodes[target]["label"] == "CATCH"}
        expected_success = cfg_targets - expected_failure
        actual_failure = {edge["target"] for edge in outgoing.get(("TRY_FAILURE", try_node), [])}
        actual_success = {edge["target"] for edge in outgoing.get(("TRY_SUCCESS", try_node), [])}
        if actual_failure != expected_failure or actual_success != expected_success:
            errors.append("TRY success/failure edges do not match CFG")
    for edge in (edge for edge in edges if edge["label"] in {"TRY_SUCCESS", "TRY_FAILURE"}):
        if nodes[edge["source"]]["label"] != "TRY" or ("CFG", edge["source"], edge["target"]) not in edge_triples or (edge["label"] == "TRY_FAILURE") != (nodes[edge["target"]]["label"] == "CATCH"):
            errors.append("invalid TRY success/failure edge")

    eval_successors: dict[str, set[str]] = {}
    for edge in (edge for edge in edges if edge["label"] == "EVAL_ORDER"):
        eval_successors.setdefault(edge["source"], set()).add(edge["target"])
    control_blocks = {node_id for node_id, node in nodes.items() if node["label"] in {"BASIC_BLOCK", "CONTROL_STRUCTURE", "MODIFIER_PLACEHOLDER", "TRY", "CATCH"}}
    cfg_successors = {block: {edge["target"] for edge in outgoing.get(("CFG", block), [])} for block in control_blocks}
    expected_evaluation: set[tuple[str, str]] = set()
    for block in control_blocks:
        operations = sorted(
            (child for child, parent in ast_parent.items() if parent == block),
            key=lambda operation: nodes[operation].get("evaluation_index", nodes[operation]["order"]),
        )
        if any(not isinstance(nodes[operation].get("evaluation_index"), int) for operation in operations):
            errors.append("operation is missing evaluation_index")
        if operations:
            expected_evaluation.add((block, operations[0]))
            expected_evaluation.update(zip(operations, operations[1:]))
            expected_evaluation.update((operations[-1], successor) for successor in cfg_successors[block])
        else:
            expected_evaluation.update((block, successor) for successor in cfg_successors[block])
    actual_evaluation = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "EVAL_ORDER"}
    if actual_evaluation != expected_evaluation:
        errors.append("EVAL_ORDER does not match ordered IR and CFG")
    for parent, child in ((edge["source"], edge["target"]) for edge in edges if edge["label"] == "AST" and edge["source"] in owner):
        reachable, pending = set(), [parent]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(eval_successors.get(current, set()))
        if child not in reachable:
            errors.append("operation is not reachable through EVAL_ORDER")

    call_labels = {"INTERNAL_CALL", "INTERNAL_DYNAMIC_CALL", "EXTERNAL_CALL", "LIBRARY_CALL", "BUILTIN_CALL", "LOW_LEVEL_CALL", "DELEGATECALL", "DYNAMIC_DELEGATECALL", "STATICCALL", "CALLCODE", "ETHER_SEND", "ETHER_TRANSFER", "SELFDESTRUCT", "MODIFIER_CALL"}
    all_semantic_targets = {(edge["source"], edge["target"]) for edge in edges if edge["label"] in call_labels and edge["target"] in function_nodes}
    semantic_targets = {(edge["source"], edge["target"]) for edge in edges if edge["label"] in call_labels - {"MODIFIER_CALL"} and edge["target"] in function_nodes}
    for edge in (edge for edge in edges if edge["label"] in call_labels):
        node = nodes[edge["source"]]
        expected_label = "MODIFIER_INVOCATION" if edge["label"] == "MODIFIER_CALL" else "CALL"
        if node["label"] != expected_label or not node.get("file") or node.get("line_start") is None or not node.get("code") or (edge["label"] == "MODIFIER_CALL" and nodes[edge["target"]]["label"] != "SOLIDITY_MODIFIER"):
            errors.append("invalid semantic CALL")
    xcfg_calls = [(edge["source"], edge["target"]) for edge in edges if edge["label"] == "XCFG_CALL"]
    contexts: dict[str, tuple[str, str, set[str]]] = {}
    for edge in (edge for edge in edges if edge["label"] == "XCFG_CALL"):
        call, entry = edge["source"], edge["target"]
        method = owner.get(entry)
        parent = ast_parent.get(call)
        caller_method = owning_function(call)
        if edge.get("callsite") != call or nodes[call]["label"] != "CALL" or method is None or parent is None or caller_method is None or (call, method) not in semantic_targets:
            errors.append("invalid XCFG_CALL")
            continue
        callee_entry, callee_exit, body_entry, body_exit, callee_vertices = flows[method]
        body = {edge["target"] for edge in outgoing.get(("CFG", body_entry), [])} - {body_exit}
        if not body:
            errors.append("XCFG_CALL expands a callee without a body")
            continue
        reachable, pending = set(), [parent]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(eval_successors.get(current, set()))
        if call not in reachable:
            errors.append("XCFG_CALL is unreachable from caller CFG")
        continuations = eval_successors.get(call, set())
        actual_returns = {return_edge["target"] for return_edge in outgoing.get(("XCFG_RETURN", callee_exit), []) if return_edge.get("callsite") == call}
        if not continuations or continuations != actual_returns:
            errors.append("invalid XCFG_RETURN continuation")
        contexts[call] = method, callee_exit, continuations
    for edge in edges:
        if edge["label"] == "XCFG_RETURN" and (edge.get("callsite") not in contexts or contexts[edge["callsite"]][1] != edge["source"] or edge["target"] not in contexts[edge["callsite"]][2]):
            errors.append("orphan XCFG_RETURN")
    for call, method in semantic_targets:
        entry, exit_, body_entry, body_exit, _ = flows[method]
        body = {edge["target"] for edge in outgoing.get(("CFG", body_entry), [])} - {body_exit}
        if body and (call, entry) not in xcfg_calls:
            errors.append("missing XCFG_CALL for source-resolved method")
    argument_edges = [edge for edge in edges if edge["label"] == "ARGUMENT"]
    arguments_by_call: dict[str, list[int]] = {}
    for edge in argument_edges:
        index = edge.get("argument_index")
        if not isinstance(index, int) or index < 0:
            errors.append("invalid ARGUMENT")
            continue
        arguments_by_call.setdefault(edge["source"], []).append(index)
        if nodes[edge["target"]].get("argument_index") != index or ast_parent.get(edge["target"]) != edge["source"] or nodes[edge["source"]]["label"] not in {"CALL", "MODIFIER_INVOCATION"} or nodes[edge["target"]]["label"] not in {"IDENTIFIER", "LITERAL"}:
            errors.append("invalid ARGUMENT")
    if any(sorted(indices) != list(range(len(indices))) for indices in arguments_by_call.values()):
        errors.append("ARGUMENT indices are not contiguous")
    argument_calls = {(edge["target"], edge.get("argument_index")): edge["source"] for edge in argument_edges}
    reaching_definitions = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "REACHING_DEF"}
    value_to_arguments = [edge for edge in edges if edge["label"] == "VALUE_TO_ARGUMENT"]
    for edge in value_to_arguments:
        index = edge.get("argument_index")
        call = argument_calls.get((edge["target"], index))
        if (
            call is None
            or nodes[edge["source"]].get("operation_type") is None
            or nodes[edge["target"]]["label"] not in {"IDENTIFIER", "LITERAL"}
            or (edge["source"], call) not in reaching_definitions
        ):
            errors.append("invalid VALUE_TO_ARGUMENT")
    parameter_owner = {parameter: function for parameter, function in ast_parent.items() if function in function_nodes and nodes[parameter]["label"] == "PARAMETER"}
    ordered_parameters = {
        function: sorted(
            (parameter for parameter, owner_function in parameter_owner.items() if owner_function == function),
            key=lambda parameter: nodes[parameter].get("parameter_index", nodes[parameter]["order"]),
        )
        for function in function_nodes
    }
    return_parameter_owner = {parameter: function for parameter, function in ast_parent.items() if function in function_nodes and nodes[parameter]["label"] == "RETURN_PARAMETER"}
    ordered_return_parameters = {
        function: sorted(
            (parameter for parameter, owner_function in return_parameter_owner.items() if owner_function == function),
            key=lambda parameter: nodes[parameter].get("parameter_index", nodes[parameter]["order"]),
        )
        for function in function_nodes
    }
    actual_bindings = {(edge["source"], edge["target"], edge.get("argument_index")) for edge in edges if edge["label"] == "ARGUMENT_TO_PARAMETER"}
    expected_bindings = {
        (argument, ordered_parameters[function][index], index)
        for (argument, index), call in argument_calls.items()
        for semantic_call, function in all_semantic_targets
        if semantic_call == call and isinstance(index, int) and 0 <= index < len(ordered_parameters[function])
    }
    if actual_bindings != expected_bindings:
        errors.append("ARGUMENT_TO_PARAMETER does not match source-resolved call")
    for edge in (edge for edge in edges if edge["label"] == "ARGUMENT_TO_PARAMETER"):
        key = edge["source"], edge.get("argument_index")
        call = argument_calls.get(key)
        function = parameter_owner.get(edge["target"])
        index = edge.get("argument_index")
        parameters = ordered_parameters.get(function, [])
        if call is None or function is None or not isinstance(index, int) or not 0 <= index < len(parameters) or parameters[index] != edge["target"] or (call, function) not in all_semantic_targets:
            errors.append("invalid ARGUMENT_TO_PARAMETER")
    return_operations = {node_id for node_id, node in nodes.items() if node["label"] == "RETURN"}
    actual_return_values = {(edge["source"], edge["target"], edge.get("return_index")) for edge in edges if edge["label"] == "RETURN_VALUE"}
    expected_return_values = {
        (operation, parameter, index)
        for operation in return_operations
        for function in [owning_function(operation)]
        if function is not None
        for index, parameter in enumerate(ordered_return_parameters[function])
    }
    if actual_return_values != expected_return_values:
        errors.append("RETURN_VALUE does not match function returns")
    actual_returns_to_caller = {(edge["source"], edge["target"], edge.get("return_index")) for edge in edges if edge["label"] == "RETURN_TO_CALLER"}
    expected_returns_to_caller = {
        (parameter, call, index)
        for call, function in all_semantic_targets
        for index, parameter in enumerate(ordered_return_parameters[function])
    }
    if actual_returns_to_caller != expected_returns_to_caller:
        errors.append("RETURN_TO_CALLER does not match source-resolved call")
    for edge in (edge for edge in edges if edge["label"] in {"RETURN_VALUE", "RETURN_TO_CALLER"}):
        if not isinstance(edge.get("return_index"), int) or edge["return_index"] < 0:
            errors.append("invalid return binding")
    for edge in (edge for edge in edges if edge["label"] in {"CALL_VALUE", "CALL_GAS", "RECEIVER"}):
        target = nodes[edge["target"]]
        valid_target = target["label"] in {"STATE_VARIABLE", "PARAMETER", "RETURN_PARAMETER", "LOCAL_VARIABLE", "BUILTIN_VARIABLE", "LITERAL", "IDENTIFIER"} or target.get("operation_type") is not None
        if nodes[edge["source"]]["label"] != "CALL" or not valid_target or (target.get("operation_type") is not None and (edge["target"], edge["source"]) not in reaching_definitions):
            errors.append("invalid call value/gas edge")

    value_target_labels = {"STATE_VARIABLE", "PARAMETER", "RETURN_PARAMETER", "LOCAL_VARIABLE", "BUILTIN_VARIABLE", "LITERAL", "IDENTIFIER"}

    def valid_access_target(node_id: str) -> bool:
        return nodes[node_id]["label"] in value_target_labels or nodes[node_id].get("operation_type") is not None

    for node_label, edge_labels in (("INDEX_ACCESS", ("INDEX_BASE", "INDEX_KEY")), ("MEMBER_ACCESS", ("MEMBER_BASE", "MEMBER_FIELD"))):
        for node_id, node in nodes.items():
            if node["label"] != node_label:
                continue
            for edge_label in edge_labels:
                matches = outgoing.get((edge_label, node_id), [])
                if len(matches) != 1:
                    errors.append(f"{edge_label} does not uniquely describe {node_label}")
                    continue
                target = nodes[matches[0]["target"]]
                if edge_label == "MEMBER_FIELD":
                    if target["label"] != "MEMBER_NAME" or ast_parent.get(matches[0]["target"]) != node_id:
                        errors.append("invalid MEMBER_FIELD")
                elif not valid_access_target(matches[0]["target"]):
                    errors.append(f"invalid {edge_label}")
    for edge in edges:
        if edge["label"] in {"INDEX_BASE", "INDEX_KEY"} and nodes[edge["source"]]["label"] != "INDEX_ACCESS":
            errors.append(f"invalid {edge['label']}")
        if edge["label"] in {"MEMBER_BASE", "MEMBER_FIELD"} and nodes[edge["source"]]["label"] != "MEMBER_ACCESS":
            errors.append(f"invalid {edge['label']}")

    low_level_calls = {edge["source"] for edge in edges if edge["label"] in {"LOW_LEVEL_CALL", "DELEGATECALL", "STATICCALL", "CALLCODE"}}
    guards = {edge["target"]: edge.get("guard") for edge in edges if edge["label"] == "GUARD"}
    reaching_successors: dict[str, set[str]] = {}
    for edge in edges:
        if edge["label"] == "REACHING_DEF":
            reaching_successors.setdefault(edge["source"], set()).add(edge["target"])
    condition_blocks = {edge["target"]: edge["source"] for edge in edges if edge["label"] == "CONDITION"}
    branch_edges = {(edge["source"], edge["target"], edge["label"]) for edge in edges if edge["label"] in {"TRUE_BRANCH", "FALSE_BRANCH"}}
    for edge in (edge for edge in edges if edge["label"] == "CHECKS_RETURN"):
        if edge["source"] not in low_level_calls or edge["target"] not in guards:
            errors.append("invalid CHECKS_RETURN")
            continue
        reachable, pending = set(), [edge["source"]]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(reaching_successors.get(current, set()))
        if edge.get("via") == "guard":
            if edge["target"] not in reachable:
                errors.append("CHECKS_RETURN guard is not data-flow reachable")
        elif edge.get("via") == "branch":
            branch = edge.get("branch")
            target_block = ast_parent.get(edge["target"])
            if guards[edge["target"]] != "revert" or not any((condition_blocks.get(condition), target_block, branch) in branch_edges for condition in reachable):
                errors.append("CHECKS_RETURN branch does not guard a revert")
        else:
            errors.append("invalid CHECKS_RETURN mode")

    declaration_labels = {"STATE_VARIABLE", "PARAMETER", "RETURN_PARAMETER", "LOCAL_VARIABLE"}
    reads = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "READS"}
    writes = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "WRITES"}
    state_reads = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "STATE_READ"}
    state_writes = {(edge["source"], edge["target"]) for edge in edges if edge["label"] == "STATE_WRITE"}
    for relation, pairs, target_labels in (("READS", reads, declaration_labels | {"BUILTIN_VARIABLE"}), ("WRITES", writes, declaration_labels)):
        if any(
            nodes[source].get("operation_type") is None
            or nodes[target]["label"] not in target_labels
            or (nodes[target]["label"] in {"PARAMETER", "RETURN_PARAMETER", "LOCAL_VARIABLE"} and owning_function(source) != owning_function(target))
            for source, target in pairs
        ):
            errors.append(f"invalid {relation}")
    if any(nodes[target]["label"] != "STATE_VARIABLE" or (source, target) not in reads for source, target in state_reads) or any(pair not in state_reads for pair in reads if nodes[pair[1]]["label"] == "STATE_VARIABLE"):
        errors.append("invalid STATE_READ")
    if any(nodes[target]["label"] != "STATE_VARIABLE" or (source, target) not in writes for source, target in state_writes) or any(pair not in state_writes for pair in writes if nodes[pair[1]]["label"] == "STATE_VARIABLE"):
        errors.append("invalid STATE_WRITE")

    direct_effects = {
        function: {
            "reads": {target for source, target in state_reads if owning_function(source) == function},
            "writes": {target for source, target in state_writes if owning_function(source) == function},
        }
        for function in function_nodes
    }
    effect_dependencies = {function: set() for function in function_nodes}
    for call, callee in semantic_targets:
        caller = owning_function(call)
        if caller is not None:
            effect_dependencies[caller].add(callee)
    for edge in (edge for edge in edges if edge["label"] == "APPLIES_MODIFIER"):
        if edge["source"] in function_nodes and edge["target"] in function_nodes:
            effect_dependencies[edge["source"]].add(edge["target"])
    effects = {
        function: {relation: set(values) for relation, values in relations.items()}
        for function, relations in direct_effects.items()
    }
    changed = True
    while changed:
        changed = False
        for function, callees in effect_dependencies.items():
            for relation in ("reads", "writes"):
                combined = set(effects[function][relation])
                for callee in callees:
                    combined.update(effects[callee][relation])
                if combined != effects[function][relation]:
                    effects[function][relation] = combined
                    changed = True
    for relation, label in (("reads", "CALL_READS_STATE"), ("writes", "CALL_WRITES_STATE")):
        actual = {(edge["source"], edge["target"], edge.get("transitive")) for edge in edges if edge["label"] == label}
        expected = {
            (call, state, state not in direct_effects[callee][relation])
            for call, callee in semantic_targets
            for state in effects[callee][relation]
        }
        if actual != expected:
            errors.append(f"{label} does not match source-resolved effects")
        if any(nodes[call]["label"] != "CALL" or nodes[state]["label"] != "STATE_VARIABLE" or not isinstance(transitive, bool) for call, state, transitive in actual):
            errors.append(f"invalid {label}")

    assembly_functions = {owning_function(node_id) for node_id, node in nodes.items() if node.get("solidity_cfg_type") == "ASSEMBLY"}
    if any(not isinstance(nodes[function].get("has_inline_assembly"), bool) or nodes[function]["has_inline_assembly"] != (function in assembly_functions) for function in function_nodes):
        errors.append("invalid function inline assembly coverage")
    contract_nodes = {node_id: node for node_id, node in nodes.items() if node["label"] in {"CONTRACT", "INTERFACE", "LIBRARY", "ABSTRACT"}}
    contract_functions = {
        contract_id: {edge["target"] for edge in outgoing.get(("CONTAINS", contract_id), []) if edge["target"] in function_nodes}
        for contract_id in contract_nodes
    }
    if any(not isinstance(contract.get("has_inline_assembly"), bool) or contract["has_inline_assembly"] != bool(contract_functions[contract_id] & assembly_functions) for contract_id, contract in contract_nodes.items()):
        errors.append("invalid contract inline assembly coverage")
    if metadata.get("has_inline_assembly") != any(assembly_functions):
        errors.append("invalid graph inline assembly coverage")

    modifier_edges = sorted((edge for edge in edges if edge["label"] == "APPLIES_MODIFIER"), key=lambda edge: (edge["source"], edge.get("modifier_index", -1)))
    modifier_calls: dict[str, list[dict[str, Any]]] = {}
    for edge in (edge for edge in edges if edge["label"] == "MODIFIER_CALL" and nodes[edge["target"]]["label"] == "SOLIDITY_MODIFIER"):
        function = owning_function(edge["source"])
        if function is not None:
            modifier_calls.setdefault(function, []).append(edge)
    semantic_applications: dict[str, list[str]] = {}
    for function, calls in modifier_calls.items():
        unique_calls: list[dict[str, Any]] = []
        seen_spans: set[tuple[str, str | None, int | str | None, int | None]] = set()
        for edge in sorted(calls, key=lambda item: (nodes[item["source"]].get("byte_start", nodes[item["source"]].get("char_start")) is None, nodes[item["source"]].get("byte_start", nodes[item["source"]].get("char_start", nodes[item["source"]]["order"])))):
            source = nodes[edge["source"]]
            start = source.get("byte_start", source.get("char_start"))
            span = (edge["target"], source.get("file"), start if start is not None else edge["source"], source.get("byte_end", source.get("char_end")))
            if span not in seen_spans:
                seen_spans.add(span)
                unique_calls.append(edge)
        semantic_applications[function] = [edge["target"] for edge in unique_calls]
    structural_applications: dict[str, list[str]] = {}
    for edge in modifier_edges:
        structural_applications.setdefault(edge["source"], []).append(edge["target"])
    if any(
        not all(target in iterator for target in semantic_targets)
        for function, semantic_targets in semantic_applications.items()
        for iterator in [iter(structural_applications.get(function, []))]
    ):
        errors.append("APPLIES_MODIFIER does not match modifier invocations")
    by_function: dict[str, list[dict[str, Any]]] = {}
    for edge in modifier_edges:
        by_function.setdefault(edge["source"], []).append(edge)
    expected_overlay: set[tuple[str, str, str, str]] = set()
    for function, modifier_list in by_function.items():
        if function not in flows or any(edge["target"] not in flows or nodes[edge["target"]]["label"] != "SOLIDITY_MODIFIER" for edge in modifier_list) or [edge.get("modifier_index") for edge in modifier_list] != list(range(len(modifier_list))):
            errors.append("invalid APPLIES_MODIFIER")
            continue
        function_entry, function_exit, body_entry, body_exit, _ = flows[function]
        modifier_flows = [flows[edge["target"]] for edge in modifier_list]
        placeholders = [[node_id for node_id in modifier_flow[4] if nodes[node_id]["label"] == "MODIFIER_PLACEHOLDER"] for modifier_flow in modifier_flows]
        expected_overlay.add(("MODIFIER_ENTER", function_entry, modifier_flows[0][0], function))
        if ("MODIFIER_ENTER", function_entry, modifier_flows[0][0], function) not in contextual_edges:
            errors.append("missing MODIFIER_ENTER")
        after: list[set[str]] = []
        body_reached = False
        for index, modifier_flow in enumerate(modifier_flows):
            return_target = after[index - 1] if index else {function_exit}
            expected_overlay.update(("MODIFIER_RETURN", modifier_flow[1], target, function) for target in return_target)
            if not all(("MODIFIER_RETURN", modifier_flow[1], target, function) in contextual_edges for target in return_target):
                errors.append("missing MODIFIER_RETURN")
            if not placeholders[index]:
                break
            after.append({edge["target"] for placeholder in placeholders[index] for edge in outgoing.get(("CFG", placeholder), [])})
            body_target = modifier_flows[index + 1][0] if index + 1 < len(modifier_flows) else body_entry
            expected_overlay.update(("MODIFIER_BODY", placeholder, body_target, function) for placeholder in placeholders[index])
            if not all(("MODIFIER_BODY", placeholder, body_target, function) in contextual_edges for placeholder in placeholders[index]):
                errors.append("missing MODIFIER_BODY")
            body_reached = index + 1 == len(modifier_flows)
        if body_reached and not all(("MODIFIER_EXIT", body_exit, target, function) in contextual_edges for target in after[-1]):
            errors.append("missing MODIFIER_EXIT")
        if body_reached:
            expected_overlay.update(("MODIFIER_EXIT", body_exit, target, function) for target in after[-1])
    actual_overlay = {
        (edge["label"], edge["source"], edge["target"], edge.get("callsite"))
        for edge in edges
        if edge["label"] in {"MODIFIER_ENTER", "MODIFIER_BODY", "MODIFIER_EXIT", "MODIFIER_RETURN"}
    }
    if actual_overlay != expected_overlay:
        errors.append("modifier overlay does not match applications")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(prog="spider-verify", description="Verify structural Solidity CPG and XCFG invariants.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("graph", type=Path, help="NetworkX node-link JSON produced by Spider")
    args = parser.parse_args()
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    errors = validate(graph)
    if errors:
        print("FAILED")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"OK: {len(graph['nodes'])} nodes, {len(graph['links'])} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
