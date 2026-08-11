import hashlib
import random
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from spider import extract
from spider.extract import _canonicalize_graph, to_dot
from spider.verify import validate


def test_extract() -> None:
    graph = extract(Path(__file__).parent / "fixtures" / "Bank.sol")
    labels = {node["label"] for node in graph["nodes"]}
    edge_labels = {edge["label"] for edge in graph["links"]}
    assert {"CONTRACT", "FUNCTION", "SOLIDITY_MODIFIER", "STATE_VARIABLE", "FUNCTION_EXIT", "BASIC_BLOCK", "INDEX_ACCESS", "BINARY_OPERATION"} <= labels
    assert {"AST", "CFG", "EVAL_ORDER", "REACHING_DEF", "STATE_READ", "STATE_WRITE", "LOW_LEVEL_CALL", "GUARD", "DOMINATE", "POST_DOMINATE"} <= edge_labels
    assert all(node["line_start"] is not None for node in graph["nodes"] if node["code"])
    assert graph["graph"]["format"] == "spider-cpg/1.0"
    assert graph["graph"]["tool"] == "Spider"
    assert graph["graph"]["attribute_schema"] == "spider-attributes/1"
    assert graph["graph"]["source_anchor_schema"] == "spider-source-anchor/1"
    assert graph["graph"]["source_anchor_unit"] == "utf-8-bytes-half-open"
    assert graph["graph"]["source_files"]
    for node in graph["nodes"]:
        assert "char_start" not in node and "char_end" not in node
        if node["file"] is None:
            assert node["file_id"] is None
        else:
            manifest = graph["graph"]["source_files"][node["file_id"]]
            assert manifest["sha256"] == hashlib.sha256(Path(node["file"]).read_bytes()).hexdigest()
            assert 0 <= node["byte_start"] <= node["byte_end"] <= manifest["byte_length"]
    assert not validate(graph)
    broken_anchor = deepcopy(graph)
    anchored = next(node for node in broken_anchor["nodes"] if node["file"] and node["byte_end"] is not None)
    anchored["byte_end"] = broken_anchor["graph"]["source_files"][anchored["file_id"]]["byte_length"] + 1
    assert "source byte span is out of bounds" in validate(broken_anchor)
    assert graph["graph"]["node_types"] == sorted(labels)
    assert graph["graph"]["edge_types"] == sorted(edge_labels)
    assert not validate(graph)

    # Canonical node ranks are the positional-encoding input.  Five raw-ID and
    # input-order permutations must serialize to the exact same canonical graph.
    canonical_graph = deepcopy(graph)
    for seed in range(177, 182):
        rng = random.Random(seed)
        permuted = deepcopy(graph)
        remap = {node["id"]: f"raw_{index}_{seed}" for index, node in enumerate(permuted["nodes"])}
        for node in permuted["nodes"]:
            node["id"] = remap[node["id"]]
        for edge in permuted["links"]:
            edge["source"] = remap[edge["source"]]
            edge["target"] = remap[edge["target"]]
            if edge.get("callsite") in remap:
                edge["callsite"] = remap[edge["callsite"]]
        rng.shuffle(permuted["nodes"])
        rng.shuffle(permuted["links"])
        _canonicalize_graph(permuted)
        assert permuted == canonical_graph

    project = extract(Path(__file__).parent / "fixtures" / "cross" / "Vault.sol")
    assert project["graph"]["scope"] == "project"
    project_nodes = {node["id"]: node for node in project["nodes"]}
    project_edges = project["links"]
    assert {"EXTERNAL_CALL", "XCFG_CALL", "XCFG_RETURN", "ARGUMENT_TO_PARAMETER", "RETURN_VALUE", "RETURN_TO_CALLER", "CALL_READS_STATE", "CALL_WRITES_STATE"} <= {edge["label"] for edge in project_edges}
    assert all(
        project_nodes[edge["source"]].get("contract_name") == "Vault"
        and project_nodes[edge["target"]].get("contract_name") == "Treasury"
        for edge in project_edges
        if edge["label"] in {"EXTERNAL_CALL", "CALL_READS_STATE", "CALL_WRITES_STATE"}
    )
    assert any(
        edge["label"] == "RETURN_TO_CALLER"
        and project_nodes[edge["source"]].get("contract_name") == "Treasury"
        and project_nodes[edge["target"]].get("contract_name") == "Vault"
        for edge in project_edges
    )
    assert len({node["file"] for node in project["nodes"] if node["file"]}) == 2
    assert not validate(project)
    assert len(project["graph"]["source_files"]) == 2
    broken_cross_return = deepcopy(project)
    broken_cross_return["links"] = [edge for edge in broken_cross_return["links"] if edge["label"] != "RETURN_TO_CALLER"]
    assert "RETURN_TO_CALLER does not match source-resolved call" in validate(broken_cross_return)
    broken_cross_effect = deepcopy(project)
    effect = next(edge for edge in broken_cross_effect["links"] if edge["label"] == "CALL_WRITES_STATE")
    effect["target"] = next(node["id"] for node in broken_cross_effect["nodes"] if node["label"] == "STATE_VARIABLE" and node["contract_name"] == "Vault")
    assert "CALL_WRITES_STATE does not match source-resolved effects" in validate(broken_cross_effect)

    windows_path = extract(Path(__file__).parent / "fixtures" / "WindowsPath.sol")
    assert windows_path["graph"]["solc_version"] == "0.8.11"
    assert not validate(windows_path)

    delegate = extract(Path(__file__).parent / "fixtures" / "Delegate.sol")
    assert "DELEGATECALL" in {edge["label"] for edge in delegate["links"]}
    assert "DYNAMIC_DELEGATECALL" in {edge["label"] for edge in delegate["links"]}

    callback = extract(Path(__file__).parent / "fixtures" / "Callback.sol")
    callback_nodes = {node["id"]: node["label"] for node in callback["nodes"]}
    callback_edges = callback["links"]
    assert not validate(callback)
    assert sum(edge["label"] == "XCFG_CALL" for edge in callback_edges) == 2
    assert all(callback_nodes[edge["source"]] == "CALL" and callback_nodes[edge["target"]] == "FUNCTION_ENTRY" and edge.get("callsite") == edge["source"] for edge in callback_edges if edge["label"] == "XCFG_CALL")
    assert all(callback_nodes[edge["source"]] == "FUNCTION_EXIT" and edge.get("callsite") for edge in callback_edges if edge["label"] == "XCFG_RETURN")
    assert any(edge["label"] == "CALL_WRITES_STATE" and edge.get("transitive") is True for edge in callback_edges)
    broken = deepcopy(callback)
    broken["links"] = [edge for edge in broken["links"] if edge["label"] != "XCFG_RETURN"]
    assert "invalid XCFG_RETURN continuation" in validate(broken)

    bank_modifier_edges = [edge for edge in graph["links"] if edge["label"] in {"MODIFIER_ENTER", "MODIFIER_BODY", "MODIFIER_EXIT", "MODIFIER_RETURN"}]
    assert {edge["label"] for edge in bank_modifier_edges} == {"MODIFIER_ENTER", "MODIFIER_BODY", "MODIFIER_EXIT", "MODIFIER_RETURN"}
    missing_modifier = deepcopy(graph)
    missing_modifier["links"] = [edge for edge in missing_modifier["links"] if edge["label"] not in {"APPLIES_MODIFIER", "MODIFIER_ENTER", "MODIFIER_BODY", "MODIFIER_EXIT", "MODIFIER_RETURN"}]
    missing_modifier["graph"]["edge_types"] = sorted({edge["label"] for edge in missing_modifier["links"]})
    assert "APPLIES_MODIFIER does not match modifier invocations" in validate(missing_modifier)

    two_modifiers = extract(Path(__file__).parent / "fixtures" / "TwoModifiers.sol")
    assert not validate(two_modifiers)
    two_modifier_nodes = {node["id"]: node for node in two_modifiers["nodes"]}
    assert not any(edge["label"].startswith("XCFG_") and two_modifier_nodes[edge["source"]]["label"] == "MODIFIER_INVOCATION" for edge in two_modifiers["links"])

    constant_flow = extract(Path(__file__).parent / "fixtures" / "ConstantFlow.sol")
    assert not validate(constant_flow)
    assert sum(node["label"] == "ENUM_MEMBER" for node in constant_flow["nodes"]) == 2
    assert "CONSTANT_VALUE" not in {edge["label"] for edge in constant_flow["links"]}

    value_call = extract(Path(__file__).parent / "fixtures" / "ValueCall.sol")
    assert not validate(value_call)
    assert "CALL_VALUE" in {edge["label"] for edge in value_call["links"]}

    control = extract(Path(__file__).parent / "fixtures" / "Control.sol")
    control_edges = [edge for edge in control["links"] if edge["label"] == "REACHING_DEF"]
    assert "CDG" in {edge["label"] for edge in control["links"]}
    assert {"TRUE_BRANCH", "FALSE_BRANCH"} <= {edge["label"] for edge in control["links"]}
    assert control_edges and all(edge.get("variable") for edge in control_edges)
    assert "OPERATION" not in {node["label"] for node in control["nodes"]}
    empty_blocks = {
        node["id"]
        for node in control["nodes"]
        if node["label"] in {"BASIC_BLOCK", "CONTROL_STRUCTURE", "MODIFIER_PLACEHOLDER"}
        and not any(edge["label"] == "AST" and edge["source"] == node["id"] for edge in control["links"])
    }
    assert empty_blocks and all(any(edge["label"] == "EVAL_ORDER" and edge["source"] == block for edge in control["links"]) for block in empty_blocks)
    assert control == extract(Path(__file__).parent / "fixtures" / "Control.sol")

    semantics = extract(Path(__file__).parent / "fixtures" / "SoliditySemantics.sol")
    assert not validate(semantics)
    semantics_nodes = {node["id"]: node for node in semantics["nodes"]}
    semantics_edges = semantics["links"]
    semantics_edge_labels = {edge["label"] for edge in semantics_edges}
    assert {"APPLIES_MODIFIER", "MODIFIER_CALL", "ARGUMENT_TO_PARAMETER", "ETHER_SEND", "ETHER_TRANSFER", "STATE_WRITE", "MEMBER_ACCESS"} <= semantics_edge_labels | {node["label"] for node in semantics["nodes"]}
    assert sum(edge["label"] == "ARGUMENT_TO_PARAMETER" for edge in semantics_edges) == 2
    assert sum(edge["label"] in {"ETHER_SEND", "ETHER_TRANSFER"} for edge in semantics_edges) == 2
    assert sum(edge["label"] == "CALL_VALUE" for edge in semantics_edges) >= 2
    assert {"INDEX_BASE", "INDEX_KEY", "MEMBER_BASE", "MEMBER_FIELD", "VALUE_TO_ARGUMENT"} <= semantics_edge_labels
    member_access = next(node_id for node_id, node in semantics_nodes.items() if node["label"] == "MEMBER_ACCESS")
    member_base = next(edge["target"] for edge in semantics_edges if edge["label"] == "MEMBER_BASE" and edge["source"] == member_access)
    assert semantics_nodes[member_base]["label"] == "INDEX_ACCESS"
    assert all(
        semantics_nodes[edge["source"]].get("operation_type") is not None
        for edge in semantics_edges
        if edge["label"] == "VALUE_TO_ARGUMENT"
    )
    assert "INDEX_BASE" in to_dot(semantics, representation="ddg")
    assert "MEMBER_BASE" not in to_dot(semantics, representation="cfg")
    assert "XCFG_CALL" in to_dot(semantics, representation="calls")
    assert not any(edge["label"] == "CONSTANT_VALUE" for edge in semantics_edges)
    internal_call = next(edge["source"] for edge in semantics_edges if edge["label"] == "INTERNAL_CALL" and semantics_nodes[edge["target"]]["name"].startswith("positive("))
    callee = next(edge["target"] for edge in semantics_edges if edge["label"] == "INTERNAL_CALL" and edge["source"] == internal_call)
    callee_exit = next(edge["target"] for edge in semantics_edges if edge["label"] == "CONTAINS" and edge["source"] == callee and semantics_nodes[edge["target"]]["label"] == "FUNCTION_EXIT")
    continuation = {edge["target"] for edge in semantics_edges if edge["label"] == "EVAL_ORDER" and edge["source"] == internal_call}
    assert continuation == {edge["target"] for edge in semantics_edges if edge["label"] == "XCFG_RETURN" and edge["source"] == callee_exit and edge.get("callsite") == internal_call}
    broken_eval = deepcopy(semantics)
    broken_eval["links"] = [edge for edge in broken_eval["links"] if not (edge["label"] == "EVAL_ORDER" and edge["target"] == internal_call)]
    assert "EVAL_ORDER does not match ordered IR and CFG" in validate(broken_eval)

    broken_return = deepcopy(semantics)
    broken_return["links"] = [
        edge
        for edge in broken_return["links"]
        if not ((edge["label"] == "EVAL_ORDER" and edge["source"] == internal_call) or (edge["label"] == "XCFG_RETURN" and edge.get("callsite") == internal_call))
    ]
    assert "invalid XCFG_RETURN continuation" in validate(broken_return)

    broken_parameter = deepcopy(semantics)
    binding = next(edge for edge in broken_parameter["links"] if edge["label"] == "ARGUMENT_TO_PARAMETER")
    binding["target"] = next(node_id for node_id, node in semantics_nodes.items() if node["label"] == "PARAMETER" and node_id != binding["target"])
    assert "invalid ARGUMENT_TO_PARAMETER" in validate(broken_parameter)

    broken_argument = deepcopy(semantics)
    argument = next(edge for edge in broken_argument["links"] if edge["label"] == "ARGUMENT")
    next(node for node in broken_argument["nodes"] if node["id"] == argument["target"])["argument_index"] += 1
    assert "invalid ARGUMENT" in validate(broken_argument)

    broken_value_argument = deepcopy(semantics)
    value_binding = next(edge for edge in broken_value_argument["links"] if edge["label"] == "VALUE_TO_ARGUMENT")
    value_binding["source"] = internal_call
    assert "invalid VALUE_TO_ARGUMENT" in validate(broken_value_argument)

    broken_index = deepcopy(semantics)
    index_key = next(edge for edge in broken_index["links"] if edge["label"] == "INDEX_KEY")
    broken_index["links"].remove(index_key)
    assert "INDEX_KEY does not uniquely describe INDEX_ACCESS" in validate(broken_index)

    broken_member = deepcopy(semantics)
    member_field = next(edge for edge in broken_member["links"] if edge["label"] == "MEMBER_FIELD")
    member_field["target"] = next(edge["target"] for edge in broken_member["links"] if edge["label"] == "MEMBER_BASE" and edge["source"] == member_field["source"])
    assert "invalid MEMBER_FIELD" in validate(broken_member)

    broken_call_value = deepcopy(semantics)
    call_value = next(edge for edge in broken_call_value["links"] if edge["label"] == "CALL_VALUE")
    call_value["target"] = next(node_id for node_id, node in semantics_nodes.items() if node["label"] == "MEMBER_NAME")
    assert "invalid call value/gas edge" in validate(broken_call_value)

    broken_read = deepcopy(semantics)
    read = next(edge for edge in broken_read["links"] if edge["label"] == "READS" and semantics_nodes[edge["source"]].get("function_name") == "exercise")
    read["target"] = next(node_id for node_id, node in semantics_nodes.items() if node["label"] == "PARAMETER" and node.get("function_name") == "positive")
    assert "invalid READS" in validate(broken_read)

    broken_state = deepcopy(semantics)
    state_write = next(edge for edge in broken_state["links"] if edge["label"] == "STATE_WRITE")
    state_write["target"] = next(node_id for node_id, node in semantics_nodes.items() if node["label"] == "PARAMETER")
    assert "invalid STATE_WRITE" in validate(broken_state)

    multiple_placeholders = extract(Path(__file__).parent / "fixtures" / "UnusedMultiPlaceholder.sol")
    assert not validate(multiple_placeholders)
    multiple_nodes = {node["id"]: node for node in multiple_placeholders["nodes"]}
    touched = next(node_id for node_id, node in multiple_nodes.items() if node["label"] == "FUNCTION" and node["name"].startswith("touched("))
    multiple_overlay = [edge for edge in multiple_placeholders["links"] if edge.get("callsite") == touched]
    assert sum(edge["label"] == "MODIFIER_BODY" for edge in multiple_overlay) == 2
    assert sum(edge["label"] == "MODIFIER_EXIT" for edge in multiple_overlay) == 2

    checked = extract(Path(__file__).parent / "fixtures" / "CheckedLowLevel.sol")
    assert not validate(checked)
    checked_nodes = {node["id"]: node for node in checked["nodes"]}
    checks = [edge for edge in checked["links"] if edge["label"] == "CHECKS_RETURN"]
    assert {(edge.get("via"), edge.get("branch")) for edge in checks} == {("guard", None), ("branch", "TRUE_BRANCH")}
    assert all("unguarded" not in checked_nodes[edge["source"]].get("function_name", "") for edge in checks)
    broken_check = deepcopy(checked)
    unguarded_call = next(edge["source"] for edge in broken_check["links"] if edge["label"] == "LOW_LEVEL_CALL" and checked_nodes[edge["source"]].get("function_name") == "unguarded")
    next(edge for edge in broken_check["links"] if edge["label"] == "CHECKS_RETURN" and edge.get("via") == "guard")["source"] = unguarded_call
    assert "CHECKS_RETURN guard is not data-flow reachable" in validate(broken_check)

    try_assembly = extract(Path(__file__).parent / "fixtures" / "TryAssembly.sol")
    assert not validate(try_assembly)
    try_nodes = {node["id"]: node for node in try_assembly["nodes"]}
    assert {"TRY", "CATCH"} <= {node["label"] for node in try_nodes.values()}
    assert sum(edge["label"] == "TRY_FAILURE" for edge in try_assembly["links"]) == 2
    assert sum(edge["label"] == "TRY_SUCCESS" for edge in try_assembly["links"]) == 1
    assert try_assembly["graph"]["has_inline_assembly"]
    assert next(node for node in try_nodes.values() if node["label"] == "FUNCTION" and node.get("function_name") == "rawCall")["has_inline_assembly"]
    assert not next(node for node in try_nodes.values() if node["label"] == "FUNCTION" and node.get("function_name") == "checkedTry")["has_inline_assembly"]
    broken_try = deepcopy(try_assembly)
    broken_try["links"] = [edge for edge in broken_try["links"] if edge["label"] != "TRY_FAILURE"]
    broken_try["graph"]["edge_types"] = sorted({edge["label"] for edge in broken_try["links"]})
    assert "TRY success/failure edges do not match CFG" in validate(broken_try)
    broken_assembly = deepcopy(try_assembly)
    broken_assembly["graph"]["has_inline_assembly"] = False
    assert "invalid graph inline assembly coverage" in validate(broken_assembly)

    tuple_call = extract(Path(__file__).parent / "fixtures" / "TupleCall.sol")
    assert not validate(tuple_call)
    tuple_nodes = {node["id"]: node for node in tuple_call["nodes"]}
    assert any(edge["label"] == "LOW_LEVEL_CALL" for edge in tuple_call["links"])
    assert any(edge["label"] == "CHECKS_RETURN" for edge in tuple_call["links"])
    assert any(node["label"] == "ASSIGNMENT" and "success(bool)" in node["name"] for node in tuple_nodes.values())
    assert tuple_call["graph"]["slither_version"] == "0.11.5"
    assert tuple_call["graph"]["solc_select_version"] == "1.2.0"

    attributes = extract(Path(__file__).parent / "fixtures" / "Attributes.sol")
    assert not validate(attributes)
    attribute_nodes = attributes["nodes"]
    limit = next(node for node in attribute_nodes if node["label"] == "STATE_VARIABLE" and node["name"] == "LIMIT")
    owner = next(node for node in attribute_nodes if node["label"] == "STATE_VARIABLE" and node["name"] == "owner")
    queues = next(node for node in attribute_nodes if node["label"] == "STATE_VARIABLE" and node["name"] == "queues")
    assert (limit["type_family"], limit["type_signedness"], limit["type_bit_width"], limit["is_constant"]) == ("integer", "unsigned", 128, True)
    assert (owner["type_family"], owner["type_bit_width"], owner["is_immutable"]) == ("address", 160, True)
    assert (queues["type_family"], queues["data_location"], queues["container_depth"]) == ("mapping", "storage", 2)
    base_update = next(node for node in attribute_nodes if node["label"] == "FUNCTION" and node["contract_name"] == "AttributeBase")
    child_update = next(node for node in attribute_nodes if node["label"] == "FUNCTION" and node["contract_name"] == "Attributes" and node["function_name"] == "update")
    assert base_update["visibility"] == "external" and child_update["visibility"] == "external"
    assert "visibility" in attributes["graph"]["learnable_attribute_keys"]
    assert base_update["is_virtual"] and not base_update["is_override"]
    assert child_update["is_override"] and not child_update["is_virtual"]
    values = [node for node in attribute_nodes if node["label"] == "PARAMETER" and node["name"] == "values"]
    assert values and all(node["data_location"] == "calldata" for node in values)
    arithmetic = [node for node in attribute_nodes if node["label"] == "BINARY_OPERATION" and node.get("operator_symbol") == "+"]
    assert {node["arithmetic_regime"] for node in arithmetic} == {"checked", "unchecked"}
    broken_attribute = deepcopy(attributes)
    next(node for node in broken_attribute["nodes"] if node["label"] == "STATE_VARIABLE")["type_family"] = "LEAKED_LABEL"
    assert "invalid type_family attribute" in validate(broken_attribute)
    broken_visibility = deepcopy(attributes)
    next(node for node in broken_visibility["nodes"] if node["label"] == "FUNCTION")["visibility"] = "package"
    assert "invalid visibility attribute" in validate(broken_visibility)


if __name__ == "__main__":
    test_extract()
