"""Semantic CPG construction from a compiled Slither unit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._graph import (
    _attach_source_manifest,
    _builtin_role,
    _call_edge_type,
    _call_name,
    _call_target,
    _canonical_path,
    _canonicalize_graph,
    _contract_functions,
    _contract_key,
    _declaration_attributes,
    _declared,
    _function_attributes,
    _function_key,
    _Graph,
    _has_span,
    _literal_category,
    _operation_label,
    _operator_attributes,
    _variable_key,
    _variable_name,
)


def _add_dataflow(graph: _Graph, blocks: list[str], predecessors: dict[str, list[str]], operations: dict[str, list[dict[str, Any]]], parameters: dict[int, set[str]]) -> None:
    """Emit CFG-fixpoint reaching definitions, preserving IR order inside each block."""
    incoming: dict[str, dict[int, set[str]]] = {block: {} for block in blocks}
    outgoing: dict[str, dict[int, set[str]]] = {block: {} for block in blocks}
    changed = True
    while changed:
        changed = False
        for block in blocks:
            merged: dict[int, set[str]] = {}
            for predecessor in predecessors[block]:
                for variable, definitions in outgoing[predecessor].items():
                    merged.setdefault(variable, set()).update(definitions)
            if not predecessors[block]:
                for variable, definitions in parameters.items():
                    merged.setdefault(variable, set()).update(definitions)
            current = {variable: set(definitions) for variable, definitions in merged.items()}
            for operation in operations[block]:
                if operation["define"] is not None:
                    current[id(operation["define"])] = {operation["id"]}
            if merged != incoming[block] or current != outgoing[block]:
                incoming[block], outgoing[block], changed = merged, current, True
    for block in blocks:
        current = {variable: set(definitions) for variable, definitions in incoming[block].items()}
        for operation in operations[block]:
            for variable in operation["read"]:
                for definition in sorted(current.get(id(variable), set())):
                    graph.edge(definition, operation["id"], "REACHING_DEF", variable=_variable_name(variable))
            if operation["define"] is not None:
                current[id(operation["define"])] = {operation["id"]}


def _add_control_dependence(graph: _Graph, entry: str, exit_: str, blocks: list[str], successors: dict[str, list[str]]) -> None:
    """Emit immediate dominators, post-dominators, and the resulting CDG."""
    vertices = [entry, *blocks, exit_]
    predecessors = {vertex: [] for vertex in vertices}
    for source, targets in successors.items():
        for target in targets:
            predecessors[target].append(source)
    dominators = {vertex: ({entry} if vertex == entry else set(vertices)) for vertex in vertices}
    changed = True
    while changed:
        changed = False
        for vertex in vertices:
            if vertex == entry:
                continue
            parents = predecessors[vertex]
            value = {vertex} | (set.intersection(*(dominators[parent] for parent in parents)) if parents else set())
            if value != dominators[vertex]:
                dominators[vertex], changed = value, True
    postdominators = {vertex: ({exit_} if vertex == exit_ else set(vertices)) for vertex in vertices}
    changed = True
    while changed:
        changed = False
        for vertex in reversed(vertices):
            if vertex == exit_:
                continue
            children = successors[vertex]
            value = {vertex} | (set.intersection(*(postdominators[child] for child in children)) if children else set())
            if value != postdominators[vertex]:
                postdominators[vertex], changed = value, True
    immediate_postdominator: dict[str, str] = {}
    for vertex in vertices:
        strict = postdominators[vertex] - {vertex}
        if strict:
            immediate_postdominator[vertex] = max(strict, key=lambda candidate: len(postdominators[candidate]))
        strict = dominators[vertex] - {vertex}
        if strict:
            graph.edge(max(strict, key=lambda candidate: len(dominators[candidate])), vertex, "DOMINATE")
        if vertex in immediate_postdominator:
            graph.edge(immediate_postdominator[vertex], vertex, "POST_DOMINATE")
    for branch in blocks:
        if len(successors[branch]) < 2:
            continue
        stop = immediate_postdominator.get(branch)
        for child in successors[branch]:
            runner, seen = child, set()
            while runner != stop and runner not in seen:
                seen.add(runner)
                if runner in blocks:
                    graph.edge(branch, runner, "CDG")
                runner = immediate_postdominator.get(runner, stop)


def _add_return_checks(graph: _Graph) -> None:
    """Link a low-level call to a guard only when SlithIR data/control flow proves it."""
    low_level_labels = {"LOW_LEVEL_CALL", "DELEGATECALL", "STATICCALL", "CALLCODE"}
    low_level_calls = {edge["source"] for edge in graph.links if edge["label"] in low_level_labels}
    if not low_level_calls:
        return
    definitions: dict[str, set[str]] = {}
    for edge in graph.links:
        if edge["label"] == "REACHING_DEF":
            definitions.setdefault(edge["source"], set()).add(edge["target"])
    guards = {edge["target"] for edge in graph.links if edge["label"] == "GUARD"}
    reverts = {edge["target"] for edge in graph.links if edge["label"] == "GUARD" and edge.get("guard") == "revert"}
    condition_blocks = {edge["target"]: edge["source"] for edge in graph.links if edge["label"] == "CONDITION"}
    branch_edges = [edge for edge in graph.links if edge["label"] in {"TRUE_BRANCH", "FALSE_BRANCH"}]
    ast_children: dict[str, set[str]] = {}
    for edge in graph.links:
        if edge["label"] == "AST":
            ast_children.setdefault(edge["source"], set()).add(edge["target"])
    for call in low_level_calls:
        reachable, pending = set(), [call]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(definitions.get(current, set()))
        for guard in guards & reachable:
            graph.edge(call, guard, "CHECKS_RETURN", via="guard")
        for condition, block in condition_blocks.items():
            if condition not in reachable:
                continue
            for branch in branch_edges:
                if branch["source"] != block:
                    continue
                for revert in reverts & ast_children.get(branch["target"], set()):
                    graph.edge(call, revert, "CHECKS_RETURN", via="branch", branch=branch["label"])


def build_graph(
    slither: Any,
    path: Path,
    sources: dict[str, bytes],
    source_files: list[Path],
    selected_solc: str,
    selected_solc_args: str,
    project_input: bool,
) -> dict[str, Any]:
    graph = _Graph(path)
    contract_ids: dict[str, str] = {}
    function_ids: dict[str, str] = {}
    state_ids: dict[str, str] = {}
    variable_ids: dict[int, str] = {}
    value_producers: dict[tuple[str, int], str] = {}
    operation_node_ids: set[str] = set()
    cfg_ids: dict[int, str] = {}
    call_ids: dict[tuple[str, str], str] = {}
    flows: dict[str, dict[str, Any]] = {}
    parameter_ids: dict[str, list[tuple[Any, str]]] = {}
    return_parameter_ids: dict[str, list[tuple[Any, str]]] = {}
    call_sites: list[dict[str, Any]] = []

    def declaration_id(variable: Any) -> str | None:
        declaration, seen = variable, set()
        while id(declaration) not in seen and getattr(declaration, "points_to", None) is not None:
            seen.add(id(declaration))
            declaration = declaration.points_to
        return variable_ids.get(id(declaration)) or state_ids.get(_variable_key(declaration))

    for contract in slither.contracts:
        contract_key = _contract_key(contract)
        contract_label = (getattr(contract, "contract_kind", None) or "contract").upper()
        contract_ids[contract_key] = graph.node(
            contract_label,
            contract.name,
            contract,
            sources,
            contract_name=contract.name,
            declaration_role="contract",
            has_inline_assembly=False,
        )
        for state in _declared(contract, "state_variables"):
            state_id = graph.node(
                "STATE_VARIABLE",
                state.name,
                state,
                sources,
                contract_name=contract.name,
                solidity_type=str(getattr(state, "type", "")),
                visibility=getattr(state, "visibility", None),
                **_declaration_attributes(state, "state"),
            )
            state_ids[_variable_key(state)] = state_id
            variable_ids[id(state)] = state_id
            graph.edge(contract_ids[contract_key], state_id, "AST")
        for function, label in _contract_functions(contract):
            function_key = _function_key(function)
            function_id = graph.node(
                label,
                function.full_name,
                function,
                sources,
                contract_name=contract.name,
                function_name=function.name,
                visibility=getattr(function, "visibility", None),
                payable=bool(getattr(function, "payable", False)),
                view=bool(getattr(function, "view", False)),
                pure=bool(getattr(function, "pure", False)),
                has_inline_assembly=False,
                **_function_attributes(function),
            )
            function_ids[function_key] = function_id
            graph.edge(contract_ids[contract_key], function_id, "CONTAINS")
            entry = graph.node("FUNCTION_ENTRY", function.full_name, contract_name=contract.name, function_name=function.name)
            exit_ = graph.node("FUNCTION_EXIT", function.full_name, contract_name=contract.name, function_name=function.name)
            graph.edge(function_id, entry, "CONTAINS")
            graph.edge(function_id, exit_, "CONTAINS")
            modifiers = [_function_key(modifier) for modifier in getattr(function, "modifiers", [])]
            body_entry, body_exit = entry, exit_
            if modifiers:
                body_entry = graph.node("FUNCTION_BODY_ENTRY", function.full_name, contract_name=contract.name, function_name=function.name)
                body_exit = graph.node("FUNCTION_BODY_EXIT", function.full_name, contract_name=contract.name, function_name=function.name)
                graph.edge(function_id, body_entry, "CONTAINS")
                graph.edge(function_id, body_exit, "CONTAINS")
            parameter_ids[function_key] = []
            return_parameter_ids[function_key] = []
            for parameter_index, parameter in enumerate(getattr(function, "parameters", [])):
                parameter_id = graph.node(
                    "PARAMETER",
                    parameter.name,
                    parameter,
                    sources,
                    contract_name=contract.name,
                    function_name=function.name,
                    parameter_index=parameter_index,
                    solidity_type=str(getattr(parameter, "type", "")),
                    **_declaration_attributes(parameter, "parameter"),
                )
                graph.edge(function_id, parameter_id, "AST")
                parameter_ids[function_key].append((parameter, parameter_id))
                variable_ids[id(parameter)] = parameter_id
            for parameter_index, parameter in enumerate(getattr(function, "returns", [])):
                parameter_id = graph.node(
                    "RETURN_PARAMETER",
                    parameter.name,
                    parameter,
                    sources,
                    contract_name=contract.name,
                    function_name=function.name,
                    parameter_index=parameter_index,
                    solidity_type=str(getattr(parameter, "type", "")),
                    **_declaration_attributes(parameter, "return"),
                )
                graph.edge(function_id, parameter_id, "AST")
                return_parameter_ids[function_key].append((parameter, parameter_id))
                variable_ids[id(parameter)] = parameter_id
            for local in getattr(function, "local_variables", []):
                local_id = graph.node(
                    "LOCAL_VARIABLE",
                    local.name,
                    local,
                    sources,
                    contract_name=contract.name,
                    function_name=function.name,
                    solidity_type=str(getattr(local, "type", "")),
                    **_declaration_attributes(local, "local"),
                )
                graph.edge(function_id, local_id, "AST")
                variable_ids[id(local)] = local_id
            flows[function_key] = {
                "entry": entry,
                "exit": exit_,
                "body_entry": body_entry,
                "body_exit": body_exit,
                "blocks": [],
                "successors": {body_entry: [], body_exit: []},
                "implemented": bool(getattr(function, "is_implemented", False)),
                "modifiers": modifiers,
                "placeholders": [],
                "state_reads": set(),
                "state_writes": set(),
            }

    state_node_ids = set(state_ids.values())
    for contract in slither.contracts:
        contract_key = _contract_key(contract)
        for parent in getattr(contract, "inheritance", []):
            parent_key = _contract_key(parent)
            if parent_key in contract_ids:
                graph.edge(contract_ids[contract_key], contract_ids[parent_key], "INHERITS")
        for function, _ in _contract_functions(contract):
            function_key = _function_key(function)
            function_id = function_ids[function_key]
            flow = flows[function_key]
            local_nodes = list(function.nodes)
            for modifier_index, modifier_key in enumerate(flow["modifiers"]):
                target = function_ids.get(modifier_key)
                if target is None:
                    raise ValueError(f"Cannot resolve Solidity modifier {modifier_key}")
                graph.edge(function_id, target, "APPLIES_MODIFIER", modifier_index=modifier_index)
            for node in local_nodes:
                node_type = getattr(getattr(node, "type", None), "name", "NODE")
                if node_type == "PLACEHOLDER":
                    label = "MODIFIER_PLACEHOLDER"
                elif node_type in {"IF", "IFLOOP", "STARTLOOP", "ENDLOOP"}:
                    label = "CONTROL_STRUCTURE"
                elif node_type in {"TRY", "CATCH"}:
                    label = node_type
                else:
                    label = "BASIC_BLOCK"
                cfg_id = graph.node(label, str(node), node, sources, contract_name=contract.name, function_name=function.name, solidity_cfg_type=node_type)
                if node_type == "ASSEMBLY":
                    graph.update_node(function_id, has_inline_assembly=True)
                    graph.update_node(contract_ids[contract_key], has_inline_assembly=True)
                cfg_ids[id(node)] = cfg_id
                flow["blocks"].append(cfg_id)
                flow["successors"][cfg_id] = []
                if node_type == "PLACEHOLDER":
                    flow["placeholders"].append(cfg_id)
                graph.edge(function_id, cfg_id, "CONTAINS")
            entry_point = getattr(function, "entry_point", None)
            if entry_point is not None and id(entry_point) in cfg_ids:
                flow["successors"][flow["body_entry"]].append(cfg_ids[id(entry_point)])
                graph.edge(flow["body_entry"], cfg_ids[id(entry_point)], "CFG")
            else:
                flow["successors"][flow["body_entry"]].append(flow["body_exit"])
                graph.edge(flow["body_entry"], flow["body_exit"], "CFG")
            operations_by_block: dict[str, list[dict[str, Any]]] = {cfg_ids[id(node)]: [] for node in local_nodes}
            predecessors: dict[str, list[str]] = {cfg_ids[id(node)]: [] for node in local_nodes}
            for node in local_nodes:
                cfg_id = cfg_ids[id(node)]
                node_type = getattr(getattr(node, "type", None), "name", "NODE")
                for child in getattr(node, "sons", []):
                    if id(child) in cfg_ids:
                        child_id = cfg_ids[id(child)]
                        flow["successors"][cfg_id].append(child_id)
                        predecessors[child_id].append(cfg_id)
                        graph.edge(cfg_id, child_id, "CFG")
                if node_type in {"IF", "IFLOOP"}:
                    for label, child in (("TRUE_BRANCH", getattr(node, "son_true", None)), ("FALSE_BRANCH", getattr(node, "son_false", None))):
                        if child is not None and id(child) in cfg_ids:
                            graph.edge(cfg_id, cfg_ids[id(child)], label)
                if not flow["successors"][cfg_id]:
                    flow["successors"][cfg_id].append(flow["body_exit"])
                    graph.edge(cfg_id, flow["body_exit"], "CFG")

                operations = list(getattr(node, "irs", []))
                operation_ids: dict[int, str] = {}
                ordered_operations: list[str] = []
                for evaluation_index, operation in enumerate(operations):
                    operation_name = type(operation).__name__
                    operation_label = _operation_label(operation)
                    operation_has_span = _has_span(operation)
                    op_id = graph.node(
                        operation_label,
                        str(operation),
                        operation if operation_has_span else node,
                        sources,
                        contract_name=contract.name,
                        function_name=function.name,
                        operation_type=operation_name,
                        evaluation_index=evaluation_index,
                        anchor_origin="exact" if operation_has_span else "cfg_fallback",
                        **_operator_attributes(operation, node),
                    )
                    operation_ids[id(operation)] = op_id
                    operation_node_ids.add(op_id)
                    ordered_operations.append(op_id)
                    graph.edge(cfg_id, op_id, "AST")
                    reads = list(getattr(operation, "read", []))
                    lvalue = getattr(operation, "lvalue", None)
                    read_targets: dict[int, str] = {}
                    for operand_index, operand in enumerate(reads):
                        target = declaration_id(operand)
                        if target:
                            graph.edge(op_id, target, "READS")
                            if target in state_node_ids:
                                graph.edge(op_id, target, "STATE_READ")
                                flow["state_reads"].add(target)
                            read_targets[id(operand)] = value_producers.get((function_key, id(operand)), target)
                        elif type(operand).__name__.startswith("SolidityVariable"):
                            target = graph.node(
                                "BUILTIN_VARIABLE",
                                str(operand),
                                operation if operation_has_span else node,
                                sources,
                                builtin_role=_builtin_role(operand),
                                anchor_origin="exact" if operation_has_span else "cfg_fallback",
                            )
                            graph.edge(op_id, target, "AST")
                            graph.edge(op_id, target, "READS")
                            read_targets[id(operand)] = target
                        elif type(operand).__name__ == "Constant" and operation_name != "Member":
                            target = graph.node(
                                "LITERAL",
                                str(operand),
                                operation if operation_has_span else node,
                                sources,
                                value=str(operand),
                                literal_category=_literal_category(operand, getattr(operand, "type", None)),
                                anchor_origin="exact" if operation_has_span else "cfg_fallback",
                            )
                            graph.edge(op_id, target, "AST")
                            graph.edge(op_id, target, "OPERAND", operand_index=operand_index)
                            read_targets[id(operand)] = target
                        elif (function_key, id(operand)) in value_producers:
                            read_targets[id(operand)] = value_producers[(function_key, id(operand))]

                    def access_target(value: Any, role: str) -> str:
                        producer = value_producers.get((function_key, id(value)))
                        if producer:
                            return producer
                        target = read_targets.get(id(value)) or declaration_id(value)
                        if target:
                            return target
                        value_type = type(value).__name__
                        label = "LITERAL" if value_type == "Constant" else "BUILTIN_VARIABLE" if value_type.startswith("SolidityVariable") else "IDENTIFIER"
                        attributes = {
                            "solidity_role": role,
                            "anchor_origin": "exact" if operation_has_span else "cfg_fallback",
                        }
                        if label == "LITERAL":
                            attributes.update({"value": str(value), "literal_category": _literal_category(value, getattr(value, "type", None))})
                        elif label == "BUILTIN_VARIABLE":
                            attributes["builtin_role"] = _builtin_role(value)
                        target = graph.node(label, str(value), operation if operation_has_span else node, sources, **attributes)
                        graph.edge(op_id, target, "AST")
                        return target

                    if operation_label == "INDEX_ACCESS":
                        graph.edge(op_id, access_target(getattr(operation, "variable_left", None), "INDEX_BASE"), "INDEX_BASE")
                        graph.edge(op_id, access_target(getattr(operation, "variable_right", None), "INDEX_KEY"), "INDEX_KEY")
                    elif operation_label == "MEMBER_ACCESS":
                        graph.edge(op_id, access_target(getattr(operation, "variable_left", None), "MEMBER_BASE"), "MEMBER_BASE")
                        member = getattr(operation, "variable_right", None)
                        member_id = graph.node(
                            "MEMBER_NAME",
                            str(member),
                            operation if operation_has_span else node,
                            sources,
                            solidity_role="MEMBER_FIELD",
                            anchor_origin="exact" if operation_has_span else "cfg_fallback",
                        )
                        graph.edge(op_id, member_id, "AST")
                        graph.edge(op_id, member_id, "MEMBER_FIELD")
                    if lvalue is not None and operation_name not in {"Index", "Length", "Member"}:
                        target = declaration_id(lvalue)
                        if target:
                            graph.edge(op_id, target, "WRITES")
                            if target in state_node_ids:
                                graph.edge(op_id, target, "STATE_WRITE")
                                flow["state_writes"].add(target)
                    if lvalue is not None and type(lvalue).__module__.startswith("slither.slithir.variables"):
                        value_producers[(function_key, id(lvalue))] = op_id
                    operations_by_block[cfg_id].append({"id": op_id, "read": reads, "define": lvalue})
                    if operation_name == "Condition":
                        graph.edge(cfg_id, op_id, "CONDITION")
                    if operation_name == "Return":
                        for return_index, (_, return_parameter_id) in enumerate(return_parameter_ids[function_key]):
                            graph.edge(op_id, return_parameter_id, "RETURN_VALUE", return_index=return_index)
                    if operation_name == "SolidityCall":
                        guard_name = str(getattr(operation, "function", "")).lower()
                        guard = next((name for name in ("require", "assert", "revert") if guard_name.startswith(name)), None)
                        if guard:
                            graph.edge(cfg_id, op_id, "GUARD", guard=guard)

                if node_type == "TRY":
                    for successor in flow["successors"][cfg_id]:
                        successor_type = next((getattr(getattr(candidate, "type", None), "name", "NODE") for candidate in local_nodes if cfg_ids[id(candidate)] == successor), "NODE")
                        graph.edge(cfg_id, successor, "TRY_FAILURE" if successor_type == "CATCH" else "TRY_SUCCESS")

                evaluation_successors: dict[str, list[str]] = {}
                if ordered_operations:
                    graph.edge(cfg_id, ordered_operations[0], "EVAL_ORDER")
                    for current, following in zip(ordered_operations, ordered_operations[1:]):
                        graph.edge(current, following, "EVAL_ORDER")
                        evaluation_successors[current] = [following]
                    evaluation_successors[ordered_operations[-1]] = list(flow["successors"][cfg_id])
                    for successor in flow["successors"][cfg_id]:
                        graph.edge(ordered_operations[-1], successor, "EVAL_ORDER")
                else:
                    for successor in flow["successors"][cfg_id]:
                        graph.edge(cfg_id, successor, "EVAL_ORDER")

                seen_calls: set[int] = set()
                for node_calls in (getattr(node, "internal_calls", []), getattr(node, "high_level_calls", []), getattr(node, "low_level_calls", [])):
                    for call in node_calls:
                        raw_call = call[-1] if isinstance(call, tuple) else call
                        caller = operation_ids.get(id(raw_call))
                        if caller is None or id(raw_call) in seen_calls:
                            continue
                        seen_calls.add(id(raw_call))
                        call_sites.append({"raw": raw_call, "candidate": _call_target(call), "caller": caller, "caller_function": function_key, "caller_exit": flow["exit"], "continuations": evaluation_successors.get(caller, list(flow["successors"][cfg_id])), "node": node, "call": call})
                for operation in operations:
                    if type(operation).__name__ not in {"Send", "Transfer"} or id(operation) in seen_calls:
                        continue
                    caller = operation_ids[id(operation)]
                    call_sites.append({"raw": operation, "candidate": None, "caller": caller, "caller_function": function_key, "caller_exit": flow["exit"], "continuations": evaluation_successors.get(caller, list(flow["successors"][cfg_id])), "node": node, "call": operation})
            _add_dataflow(graph, flow["blocks"], predecessors, operations_by_block, {id(parameter): {parameter_id} for parameter, parameter_id in parameter_ids[function_key]})
            _add_control_dependence(graph, flow["body_entry"], flow["body_exit"], flow["blocks"], flow["successors"])

    for contract in slither.contracts:
        for function, _ in _contract_functions(contract):
            function_key = _function_key(function)
            flow = flows[function_key]
            modifiers = flow["modifiers"]
            if not modifiers:
                continue
            modifier_flows = [flows.get(modifier) for modifier in modifiers]
            if any(modifier_flow is None for modifier_flow in modifier_flows):
                raise ValueError(f"Cannot resolve modifier flow for {function_key}")
            function_id = function_ids[function_key]
            graph.edge(flow["entry"], modifier_flows[0]["entry"], "MODIFIER_ENTER", callsite=function_id)
            return_targets = {flow["exit"]}
            body_reached = False
            for index, modifier_flow in enumerate(modifier_flows):
                for return_target in return_targets:
                    graph.edge(modifier_flow["exit"], return_target, "MODIFIER_RETURN", callsite=function_id)
                if not modifier_flow["placeholders"]:
                    break
                next_entry = modifier_flows[index + 1]["entry"] if index + 1 < len(modifier_flows) else flow["body_entry"]
                for placeholder in modifier_flow["placeholders"]:
                    graph.edge(placeholder, next_entry, "MODIFIER_BODY", callsite=function_id)
                return_targets = {
                    successor
                    for placeholder in modifier_flow["placeholders"]
                    for successor in modifier_flow["successors"][placeholder]
                }
                body_reached = index + 1 == len(modifier_flows)
            if body_reached:
                for return_target in return_targets:
                    graph.edge(flow["body_exit"], return_target, "MODIFIER_EXIT", callsite=function_id)

    def call_value_node(value: Any, call: dict[str, Any], role: str) -> str:
        producer = value_producers.get((call["caller_function"], id(value)))
        if producer:
            return producer
        target = declaration_id(value)
        if target:
            return target
        label = "LITERAL" if type(value).__name__ == "Constant" else "BUILTIN_VARIABLE" if type(value).__name__.startswith("SolidityVariable") else "IDENTIFIER"
        raw_call = call["raw"]
        has_span = _has_span(raw_call)
        attributes = {"solidity_role": role, "anchor_origin": "exact" if has_span else "cfg_fallback"}
        if label == "LITERAL":
            attributes.update({"value": str(value), "literal_category": _literal_category(value, getattr(value, "type", None))})
        elif label == "BUILTIN_VARIABLE":
            attributes["builtin_role"] = _builtin_role(value)
        return graph.node(label, str(value), raw_call if has_span else call["node"], sources, **attributes)

    for call in call_sites:
        call["candidate_key"] = _function_key(call["candidate"]) if call["candidate"] is not None else None
        call["edge_type"] = _call_edge_type(call["raw"])

    dependencies = {function: set(flow["modifiers"]) for function, flow in flows.items()}
    for call in call_sites:
        if call["edge_type"] != "MODIFIER_CALL" and call["candidate_key"] in flows:
            dependencies[call["caller_function"]].add(call["candidate_key"])
    effects = {
        function: {
            "reads": set(flow["state_reads"]),
            "writes": set(flow["state_writes"]),
        }
        for function, flow in flows.items()
    }
    changed = True
    while changed:
        changed = False
        for function, callees in dependencies.items():
            for relation in ("reads", "writes"):
                combined = set(effects[function][relation])
                for callee in callees:
                    if callee in effects:
                        combined.update(effects[callee][relation])
                if combined != effects[function][relation]:
                    effects[function][relation] = combined
                    changed = True

    reaching_operation_sources: dict[tuple[str, str], set[str]] = {}
    for edge in graph.links:
        if edge["label"] == "REACHING_DEF" and edge["source"] in operation_node_ids:
            key = edge["target"], edge.get("variable", "")
            reaching_operation_sources.setdefault(key, set()).add(edge["source"])

    for call in call_sites:
        candidate_key = call["candidate_key"]
        target = function_ids.get(candidate_key) if candidate_key else None
        edge_type = call["edge_type"]
        dynamic_delegatecall = edge_type == "DELEGATECALL" and target is None
        if type(call["raw"]).__name__ in {"Send", "Transfer"}:
            name = f"address.{type(call['raw']).__name__.lower()}"
        elif type(call["raw"]).__name__ == "LowLevelCall":
            name = f"address.{getattr(call['raw'], 'function_name', 'call')}"
        else:
            name = _call_name(call["call"])
        if target is None:
            target = call_ids.get((edge_type, name))
            if target is None:
                target = graph.node("CALL_TARGET", name, None, sources)
                call_ids[(edge_type, name)] = target
        graph.edge(call["caller"], target, edge_type)
        if dynamic_delegatecall:
            graph.edge(call["caller"], target, "DYNAMIC_DELEGATECALL")
        for index, argument in enumerate(getattr(call["raw"], "arguments", [])):
            label = "LITERAL" if type(argument).__name__ == "Constant" else "IDENTIFIER"
            argument_has_span = _has_span(call["raw"])
            argument_attrs = {
                "argument_index": index,
                "anchor_origin": "exact" if argument_has_span else "cfg_fallback",
            }
            if label == "LITERAL":
                argument_attrs.update({"value": str(argument), "literal_category": _literal_category(argument, getattr(argument, "type", None))})
            argument_id = graph.node(label, str(argument), call["raw"] if argument_has_span else call["node"], sources, **argument_attrs)
            graph.edge(call["caller"], argument_id, "AST")
            graph.edge(call["caller"], argument_id, "ARGUMENT", argument_index=index)
            producer = value_producers.get((call["caller_function"], id(argument)))
            reference = declaration_id(argument) if producer is None else None
            if producer:
                graph.edge(producer, argument_id, "VALUE_TO_ARGUMENT", argument_index=index)
            else:
                if reference:
                    graph.edge(argument_id, reference, "REF")
                variable_name = _variable_name(argument)
                for reaching_source in sorted(reaching_operation_sources.get((call["caller"], variable_name), set())):
                    graph.edge(reaching_source, argument_id, "VALUE_TO_ARGUMENT", argument_index=index)
            if candidate_key in parameter_ids and index < len(parameter_ids[candidate_key]):
                graph.edge(argument_id, parameter_ids[candidate_key][index][1], "ARGUMENT_TO_PARAMETER", argument_index=index)
        if edge_type != "MODIFIER_CALL" and candidate_key in return_parameter_ids:
            for return_index, (_, return_parameter_id) in enumerate(return_parameter_ids[candidate_key]):
                graph.edge(return_parameter_id, call["caller"], "RETURN_TO_CALLER", return_index=return_index)
        if edge_type != "MODIFIER_CALL" and candidate_key in effects:
            direct = flows[candidate_key]
            for relation, label in (("reads", "CALL_READS_STATE"), ("writes", "CALL_WRITES_STATE")):
                for state_id in sorted(effects[candidate_key][relation]):
                    graph.edge(call["caller"], state_id, label, transitive=state_id not in direct[f"state_{relation}"])
        receiver = getattr(call["raw"], "destination", None)
        if receiver is not None:
            graph.edge(call["caller"], call_value_node(receiver, call, "RECEIVER"), "RECEIVER")
        for label, value in (("CALL_VALUE", getattr(call["raw"], "call_value", None)), ("CALL_GAS", getattr(call["raw"], "call_gas", None))):
            if value is not None:
                graph.edge(call["caller"], call_value_node(value, call, label), label)
        target_flow = flows.get(candidate_key) if candidate_key else None
        if edge_type != "MODIFIER_CALL" and target_flow and target_flow["implemented"]:
            graph.edge(call["caller"], target_flow["entry"], "XCFG_CALL", callsite=call["caller"])
            for continuation in call["continuations"] or [call["caller_exit"]]:
                graph.edge(target_flow["exit"], continuation, "XCFG_RETURN", callsite=call["caller"])
    _add_return_checks(graph)
    data = graph.data()
    data["graph"]["solc_version"] = selected_solc
    data["graph"]["solc_args"] = selected_solc_args
    data["graph"]["input_kind"] = "directory" if project_input else "file"
    data["graph"]["input_sources"] = [_canonical_path(source) for source in source_files]
    data["graph"]["scope"] = "project" if project_input or len({node["file"] for node in data["nodes"] if node["file"]}) > 1 else "file"
    data["graph"]["has_inline_assembly"] = any(node.get("has_inline_assembly") for node in data["nodes"] if node["label"] in {"CONTRACT", "INTERFACE", "LIBRARY", "ABSTRACT"})
    data["graph"]["compiler_semantic_regime"] = "checked-arithmetic-default" if tuple(map(int, selected_solc.split("."))) >= (0, 8) else "unchecked-arithmetic-default"
    data["graph"]["unsupported_semantics"] = ["INLINE_ASSEMBLY_OPAQUE"] if data["graph"]["has_inline_assembly"] else []
    _attach_source_manifest(data, sources)
    _canonicalize_graph(data)
    data["graph"]["node_ordering"] = "spider-canonical-v1"
    return data
