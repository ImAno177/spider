"""Graph storage, source metadata, canonicalization, and DOT rendering."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

_ATTRIBUTE_SCHEMA = "spider-attributes/1"
_ANCHOR_SCHEMA = "spider-source-anchor/1"
_DISTRIBUTION = "spider-solidity-cpg"
_ATTRIBUTE_KEYS = (
    "declaration_role",
    "visibility",
    "state_mutability",
    "data_location",
    "type_family",
    "type_signedness",
    "type_bit_width",
    "type_dynamicity",
    "type_shape",
    "container_depth",
    "is_constant",
    "is_immutable",
    "is_virtual",
    "is_override",
    "operator_family",
    "operator_symbol",
    "arithmetic_regime",
    "literal_category",
    "builtin_role",
)

def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def _canonical_path(value: str | Path) -> str:
    """Return a deterministic, slash-normalized source-unit identity."""
    return Path(value).resolve().as_posix()


def _span(obj: Any, sources: dict[str, bytes]) -> dict[str, Any]:
    mapping = getattr(obj, "source_mapping", None)
    if not mapping:
        return {
            "file": None,
            "file_id": None,
            "line_start": None,
            "line_end": None,
            "column_start": None,
            "column_end": None,
            "byte_start": None,
            "byte_end": None,
            "code": "",
            "source_mapping_status": "synthetic" if obj is None else "missing",
            "anchor_origin": "synthetic" if obj is None else "missing",
        }
    filename = str(getattr(getattr(mapping, "filename", None), "absolute", "") or "")
    start = getattr(mapping, "start", None)
    length = getattr(mapping, "length", None)
    end = start + length if isinstance(start, int) and isinstance(length, int) else None
    lines = list(getattr(mapping, "lines", []) or [])
    if filename not in sources:
        try:
            sources[filename] = Path(filename).read_bytes()
        except OSError:
            sources[filename] = b""
    source = sources[filename]
    valid_span = isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(source)
    code = source[start:end].decode("utf-8", errors="replace").strip() if valid_span else ""
    status = "present" if filename and valid_span else "missing"
    column_start = getattr(mapping, "starting_column", None)
    column_end = getattr(mapping, "ending_column", None)
    return {
        "file": filename or None,
        "file_id": None,
        "line_start": min(lines) if lines else None,
        "line_end": max(lines) if lines else None,
        "column_start": column_start if isinstance(column_start, int) else None,
        "column_end": column_end if isinstance(column_end, int) else None,
        "byte_start": start if valid_span else None,
        "byte_end": end if valid_span else None,
        "code": code,
        "source_mapping_status": status,
        "anchor_origin": "exact" if status == "present" else "missing",
    }


def _has_span(obj: Any) -> bool:
    mapping = getattr(obj, "source_mapping", None)
    filename = getattr(getattr(mapping, "filename", None), "absolute", None)
    return bool(filename and isinstance(getattr(mapping, "start", None), int))


def _type_attributes(solidity_type: Any) -> dict[str, Any]:
    """Convert Slither's structured type object into finite SSAE features."""
    if solidity_type is None:
        return {
            "type_family": "UNK",
            "type_signedness": "UNK",
            "type_bit_width": None,
            "type_dynamicity": "UNK",
            "type_shape": "unknown",
            "container_depth": 0,
        }

    type_name = type(solidity_type).__name__
    text = str(getattr(solidity_type, "type", "") or solidity_type).lower()
    family = "unknown"
    signedness = "NA"
    width: int | None = None
    dynamicity = "NA"
    shape = "scalar"
    depth = 0

    if type_name == "ElementaryType":
        if text == "address" or text.startswith("address "):
            family, width = "address", 160
        elif text == "bool":
            family, width = "boolean", 8
        elif text.startswith("uint"):
            family, signedness = "integer", "unsigned"
            suffix = text[4:]
            width = int(suffix) if suffix.isdigit() else 256
        elif text.startswith("int"):
            family, signedness = "integer", "signed"
            suffix = text[3:]
            width = int(suffix) if suffix.isdigit() else 256
        elif text.startswith("bytes") and text[5:].isdigit():
            family, width = "fixed_bytes", int(text[5:]) * 8
        elif text == "bytes":
            family, dynamicity, shape = "dynamic_bytes", "dynamic", "dynamic"
        elif text == "string":
            family, dynamicity, shape = "string", "dynamic", "dynamic"
        elif text.startswith("fixed") or text.startswith("ufixed"):
            family = "fixed_point"
        else:
            family = text.split(" ", 1)[0] or "unknown"
    elif type_name == "ArrayType":
        family, shape = "array", "array"
        depth = 1
        element = getattr(solidity_type, "type", None)
        nested = _type_attributes(element) if element is not None and element is not solidity_type else None
        if nested:
            depth += int(nested["container_depth"])
        dynamicity = "dynamic" if bool(getattr(solidity_type, "is_dynamic", False)) else "fixed"
    elif type_name == "MappingType":
        family, shape, dynamicity, depth = "mapping", "mapping", "dynamic", 1
        children = [getattr(solidity_type, "type_from", None), getattr(solidity_type, "type_to", None)]
        child_depths = [_type_attributes(child)["container_depth"] for child in children if child is not None]
        if child_depths:
            depth += max(child_depths)
    elif type_name == "FunctionType":
        family, shape = "function", "function"
    elif type_name == "UserDefinedType":
        underlying = getattr(solidity_type, "type", None)
        underlying_name = type(underlying).__name__
        family = {
            "Contract": "contract",
            "Structure": "struct",
            "Enum": "enum",
        }.get(underlying_name, "user_defined")
    elif type_name in {"TypeInformationType", "TypeType"}:
        family, shape = "type_meta", "type_meta"

    if family not in {"integer", "address", "boolean", "fixed_bytes"} and dynamicity == "NA":
        dynamic = getattr(solidity_type, "is_dynamic", None)
        if isinstance(dynamic, bool):
            dynamicity = "dynamic" if dynamic else "fixed"
    if family in {"address", "boolean", "fixed_bytes"}:
        dynamicity = "fixed"
    return {
        "type_family": family,
        "type_signedness": signedness,
        "type_bit_width": width,
        "type_dynamicity": dynamicity,
        "type_shape": shape,
        "container_depth": depth,
    }


def _is_reference_type(solidity_type: Any) -> bool:
    return _type_attributes(solidity_type)["type_family"] in {"array", "mapping", "dynamic_bytes", "string", "struct"}


def _data_location(variable: Any, declaration_role: str, solidity_type: Any) -> str:
    if declaration_role == "state":
        raw = str(getattr(variable, "location", "") or "").lower()
        return "transient" if raw == "transient" else "storage"
    if not _is_reference_type(solidity_type):
        return "NA"
    raw = str(getattr(variable, "location", "") or "").lower()
    return raw if raw in {"storage", "memory", "calldata", "transient"} else "MISSING"


def _declaration_attributes(variable: Any, declaration_role: str) -> dict[str, Any]:
    solidity_type = getattr(variable, "type", None)
    attrs = _type_attributes(solidity_type)
    attrs.update(
        {
            "declaration_role": declaration_role,
            "data_location": _data_location(variable, declaration_role, solidity_type),
            "state_mutability": "NA",
            "is_constant": bool(getattr(variable, "is_constant", False)) if declaration_role == "state" else None,
            "is_immutable": bool(getattr(variable, "is_immutable", False)) if declaration_role == "state" else None,
            "is_virtual": None,
            "is_override": None,
        }
    )
    return attrs


def _function_attributes(function: Any) -> dict[str, Any]:
    if bool(getattr(function, "payable", False)):
        mutability = "payable"
    elif bool(getattr(function, "view", False)):
        mutability = "view"
    elif bool(getattr(function, "pure", False)):
        mutability = "pure"
    else:
        mutability = "nonpayable"
    attrs = {
        "declaration_role": "modifier" if getattr(getattr(function, "function_type", None), "name", "") == "MODIFIER" else "function",
        "state_mutability": mutability,
        "data_location": "NA",
        "type_family": "function",
        "type_signedness": "NA",
        "type_bit_width": None,
        "type_dynamicity": "NA",
        "type_shape": "function",
        "container_depth": 0,
        "is_constant": None,
        "is_immutable": None,
        "is_virtual": bool(getattr(function, "is_virtual", False)),
        "is_override": bool(getattr(function, "is_override", False)),
    }
    return attrs


def _operator_attributes(operation: Any, node: Any) -> dict[str, Any]:
    operation_name = type(operation).__name__
    operator = getattr(getattr(operation, "type", None), "value", None)
    operator = str(operator) if operator is not None else None
    arithmetic = {"+", "-", "*", "**", "/", "%", "++", "--"}
    comparison = {"<", "<=", ">", ">=", "==", "!="}
    logical = {"&&", "||", "!"}
    bitwise = {"&", "|", "^", "~", "<<", ">>"}
    if operation_name == "Assignment":
        family = "assignment"
    elif operator in arithmetic:
        family = "arithmetic"
    elif operator in comparison:
        family = "comparison"
    elif operator in logical:
        family = "logical"
    elif operator in bitwise:
        family = "bitwise"
    elif operation_name in {"Condition", "Return"}:
        family = operation_name.lower()
    else:
        family = "NA"
    checked = getattr(getattr(node, "scope", None), "is_checked", None)
    return {
        "operator_family": family,
        "operator_symbol": operator,
        "arithmetic_regime": ("checked" if checked else "unchecked") if family == "arithmetic" and isinstance(checked, bool) else "NA",
    }


def _literal_category(value: Any, solidity_type: Any = None) -> str:
    family = _type_attributes(solidity_type).get("type_family") if solidity_type is not None else None
    text = str(getattr(value, "value", value)).strip()
    lowered = text.lower()
    if family == "boolean" or lowered in {"true", "false"}:
        return "bool"
    if family == "address":
        return "address"
    if family in {"string"} or (len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]):
        return "string"
    if family in {"dynamic_bytes", "fixed_bytes"} or lowered.startswith("hex"):
        return "bytes"
    try:
        numeric = int(text, 0)
        if numeric == 0:
            return "zero"
        if numeric == 1:
            return "one"
        return "numeric"
    except (TypeError, ValueError):
        return "other"


_BUILTIN_ROLES = {
    "msg.sender": "MSG_SENDER",
    "msg.value": "MSG_VALUE",
    "msg.data": "MSG_DATA",
    "tx.origin": "TX_ORIGIN",
    "tx.gasprice": "TX_GASPRICE",
    "block.timestamp": "BLOCK_TIMESTAMP",
    "block.number": "BLOCK_NUMBER",
    "block.prevrandao": "BLOCK_PREVRANDAO",
    "block.difficulty": "BLOCK_DIFFICULTY",
    "block.coinbase": "BLOCK_COINBASE",
    "blockhash": "BLOCKHASH",
    "now": "BLOCK_TIMESTAMP",
}


def _builtin_role(value: Any) -> str:
    text = str(value).strip().lower()
    return _BUILTIN_ROLES.get(text, "OTHER")


class _Graph:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.nodes: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self._edge_set: set[tuple[Any, ...]] = set()
        self._ids: Counter[str] = Counter()
        self._node_data: dict[str, dict[str, Any]] = {}

    def node(self, label: str, name: str, obj: Any = None, sources: dict[str, bytes] | None = None, **extra: Any) -> str:
        prefix = label.lower()
        self._ids[prefix] += 1
        node_id = f"{prefix}_{self._ids[prefix]}"
        data = {"id": node_id, "label": label, "name": name, "order": len(self.nodes), **_span(obj, sources or {}), **extra}
        self.nodes.append(data)
        self._node_data[node_id] = data
        return node_id

    def update_node(self, node_id: str, **extra: Any) -> None:
        self._node_data[node_id].update(extra)

    def edge(self, source: str, target: str, label: str, **extra: Any) -> None:
        key = (source, target, label, *sorted(extra.items()))
        if key not in self._edge_set:
            self._edge_set.add(key)
            self.links.append({"source": source, "target": target, "label": label, **extra})

    def data(self) -> dict[str, Any]:
        return {
            "directed": True,
            "multigraph": True,
            "graph": {
                "format": "spider-cpg/1.0",
                "tool": "Spider",
                "source": _canonical_path(self.source),
                "scope": "file",
                "node_type_key": "label",
                "edge_type_key": "label",
                "position_key": "order",
                "schema": "Spider Solidity/SlithIR typed CPG",
                "attribute_schema": _ATTRIBUTE_SCHEMA,
                "source_anchor_schema": _ANCHOR_SCHEMA,
                "source_anchor_unit": "utf-8-bytes-half-open",
                "source_hash": "sha256(raw-source-bytes)",
                "learnable_attribute_keys": list(_ATTRIBUTE_KEYS),
                "extractor_version": _package_version(_DISTRIBUTION),
                "slither_version": _package_version("slither-analyzer"),
                "solc_select_version": _package_version("solc-select"),
                "node_types": sorted({node["label"] for node in self.nodes}),
                "edge_types": sorted({edge["label"] for edge in self.links}),
            },
            "nodes": self.nodes,
            "links": self.links,
        }


def _source_manifest(sources: dict[str, bytes]) -> list[dict[str, Any]]:
    """Build a canonical, raw-byte manifest for every resolved source unit."""
    canonical: dict[str, bytes] = {}
    for filename, content in sources.items():
        key = _canonical_path(filename)
        previous = canonical.get(key)
        if previous is not None and previous != content:
            raise ValueError(f"source path resolved to conflicting contents: {key}")
        canonical[key] = content
    manifest = []
    for file_id, filename in enumerate(sorted(canonical)):
        content = canonical[filename]
        manifest.append(
            {
                "file_id": file_id,
                "path": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_length": len(content),
                "encoding": "utf-8",
            }
        )
    return manifest


def _attach_source_manifest(data: dict[str, Any], sources: dict[str, bytes]) -> None:
    manifest = _source_manifest(sources)
    by_path = {entry["path"]: entry for entry in manifest}
    for node in data["nodes"]:
        filename = node.get("file")
        entry = by_path.get(_canonical_path(filename)) if filename else None
        node["file_id"] = entry["file_id"] if entry else None
    data["graph"]["source_files"] = manifest
    data["graph"]["source_mapping_coverage"] = dict(Counter(node.get("source_mapping_status", "missing") for node in data["nodes"]))


def _canonicalize_graph(data: dict[str, Any]) -> None:
    """Make serialized node IDs/order independent of Slither set iteration."""
    nodes = data["nodes"]
    links = data["links"]
    by_id = {node["id"]: node for node in nodes}

    def stable_node_payload(node: dict[str, Any]) -> str:
        # `file` is an absolute host path; file_id + exact byte anchor carry the
        # same ordering information without making workspace location a key.
        payload = {key: value for key, value in node.items() if key not in {"id", "order", "file"}}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    base = {node_id: stable_node_payload(node) for node_id, node in by_id.items()}
    colors = {node_id: hashlib.sha256(payload.encode("utf-8")).hexdigest() for node_id, payload in base.items()}
    for _ in range(8):
        incident: dict[str, list[str]] = {node_id: [] for node_id in by_id}
        for edge in links:
            edge_attrs = {
                key: value
                for key, value in edge.items()
                if key not in {"source", "target", "callsite"}
            }
            attrs = json.dumps(edge_attrs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            callsite = colors.get(edge.get("callsite"), "")
            incident[edge["source"]].append(f"O|{attrs}|{colors[edge['target']]}|{callsite}")
            incident[edge["target"]].append(f"I|{attrs}|{colors[edge['source']]}|{callsite}")
        updated = {
            node_id: hashlib.sha256(
                (base[node_id] + "\n" + "\n".join(sorted(incident[node_id]))).encode("utf-8")
            ).hexdigest()
            for node_id in by_id
        }
        if updated == colors:
            break
        colors = updated

    ordered = sorted(
        nodes,
        key=lambda node: (
            node.get("file_id") if isinstance(node.get("file_id"), int) else -1,
            node.get("byte_start") if isinstance(node.get("byte_start"), int) else -1,
            node.get("byte_end") if isinstance(node.get("byte_end"), int) else -1,
            node["label"],
            colors[node["id"]],
            base[node["id"]],
        ),
    )
    counters: Counter[str] = Counter()
    remap: dict[str, str] = {}
    for order, node in enumerate(ordered):
        prefix = node["label"].lower()
        counters[prefix] += 1
        remap[node["id"]] = f"{prefix}_{counters[prefix]}"
        node["order"] = order
    for node in ordered:
        node["id"] = remap[node["id"]]
    for edge in links:
        edge["source"] = remap[edge["source"]]
        edge["target"] = remap[edge["target"]]
        if edge.get("callsite") in remap:
            edge["callsite"] = remap[edge["callsite"]]
    links.sort(key=lambda edge: json.dumps(edge, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    data["nodes"] = ordered


def _declared(contract: Any, name: str) -> list[Any]:
    return list(getattr(contract, f"{name}_declared", getattr(contract, name, [])))


def _function_key(function: Any) -> str:
    mapping = getattr(function, "source_mapping", None)
    filename = str(getattr(getattr(mapping, "filename", None), "absolute", "") or "")
    name = getattr(function, "canonical_name", None) or getattr(function, "full_name", None) or str(function)
    return f"{filename}:{name}"


def _contract_key(contract: Any) -> str:
    mapping = getattr(contract, "source_mapping", None)
    filename = str(getattr(getattr(mapping, "filename", None), "absolute", "") or "")
    return f"{filename}:{getattr(contract, 'name', contract)}"


def _function_label(function: Any, is_modifier: bool) -> str:
    if is_modifier:
        return "SOLIDITY_MODIFIER"
    kind = getattr(getattr(function, "function_type", None), "name", "NORMAL")
    return {
        "CONSTRUCTOR": "CONSTRUCTOR",
        "FALLBACK": "FALLBACK",
        "RECEIVE": "RECEIVE",
        "CONSTRUCTOR_VARIABLES": "STATE_INITIALIZER",
        "CONSTRUCTOR_CONSTANT_VARIABLES": "STATE_INITIALIZER",
    }.get(kind, "FUNCTION")


def _contract_functions(contract: Any) -> list[tuple[Any, str]]:
    modifiers = _declared(contract, "modifiers")
    modifier_ids = {id(modifier) for modifier in modifiers}
    result: list[tuple[Any, str]] = []
    seen: set[int] = set()
    for function in _declared(contract, "functions") + modifiers:
        if id(function) not in seen:
            seen.add(id(function))
            result.append((function, _function_label(function, id(function) in modifier_ids)))
    return result


def _call_target(call: Any) -> Any:
    if isinstance(call, tuple):
        call = call[-1]
    return getattr(call, "function", call)


def _call_name(call: Any) -> str:
    call = _call_target(call)
    return getattr(call, "canonical_name", None) or getattr(call, "full_name", None) or str(call)


def _variable_name(variable: Any) -> str:
    return getattr(variable, "canonical_name", None) or getattr(variable, "name", None) or str(variable)


def _variable_key(variable: Any) -> str:
    mapping = getattr(variable, "source_mapping", None)
    filename = str(getattr(getattr(mapping, "filename", None), "absolute", "") or "")
    return f"{filename}:{_variable_name(variable)}"


def _operation_label(operation: Any) -> str:
    name = type(operation).__name__
    if str(operation).startswith("MODIFIER_CALL"):
        return "MODIFIER_INVOCATION"
    if name == "EventCall":
        return "EVENT_EMIT"
    if name == "PhiCallback":
        return "PHI"
    if "Call" in name or name in {"Send", "Transfer"}:
        return "CALL"
    if name == "Member" and type(getattr(operation, "variable_left", None)).__name__ == "EnumContract":
        return "ENUM_MEMBER"
    return {
        "Assignment": "ASSIGNMENT",
        "Binary": "BINARY_OPERATION",
        "Condition": "CONDITION",
        "Delete": "DELETE",
        "Index": "INDEX_ACCESS",
        "InitArray": "ARRAY_LITERAL",
        "Length": "LENGTH_ACCESS",
        "Member": "MEMBER_ACCESS",
        "NewArray": "NEW_ARRAY",
        "NewContract": "CONTRACT_CREATION",
        "NewElementaryType": "NEW_VALUE",
        "NewStructure": "NEW_STRUCT",
        "Nop": "NO_OP",
        "Phi": "PHI",
        "Return": "RETURN",
        "TypeConversion": "TYPE_CONVERSION",
        "Unary": "UNARY_OPERATION",
        "Unpack": "TUPLE_UNPACK",
    }.get(name, "OPERATION")


def _call_edge_type(call: Any) -> str:
    name = type(call).__name__
    if str(call).startswith("MODIFIER_CALL"):
        return "MODIFIER_CALL"
    if name == "Send":
        return "ETHER_SEND"
    if name == "Transfer":
        return "ETHER_TRANSFER"
    if name == "LibraryCall":
        return "LIBRARY_CALL"
    if name == "HighLevelCall":
        return "EXTERNAL_CALL"
    if name == "InternalDynamicCall":
        return "INTERNAL_DYNAMIC_CALL"
    if name == "InternalCall":
        return "INTERNAL_CALL"
    if name == "SolidityCall":
        function = str(getattr(call, "function", "")).split("(", 1)[0]
        return "SELFDESTRUCT" if function in {"selfdestruct", "suicide"} else "BUILTIN_CALL"
    if name == "LowLevelCall":
        return {
            "delegatecall": "DELEGATECALL",
            "staticcall": "STATICCALL",
            "callcode": "CALLCODE",
        }.get(getattr(call, "function_name", ""), "LOW_LEVEL_CALL")
    return "CALL"


DOT_REPRESENTATIONS: dict[str, set[str] | None] = {
    "ast": {"AST", "CONTAINS"},
    "cfg": {"CFG", "EVAL_ORDER", "TRUE_BRANCH", "FALSE_BRANCH", "TRY_SUCCESS", "TRY_FAILURE"},
    "cdg": {"CDG", "DOMINATE", "POST_DOMINATE"},
    "ddg": {
        "READS", "WRITES", "STATE_READ", "STATE_WRITE", "REACHING_DEF", "REF", "OPERAND", "ARGUMENT",
        "INDEX_BASE", "INDEX_KEY", "MEMBER_BASE", "MEMBER_FIELD", "VALUE_TO_ARGUMENT",
        "ARGUMENT_TO_PARAMETER", "RETURN_VALUE", "RETURN_TO_CALLER", "RECEIVER", "CALL_VALUE", "CALL_GAS", "CALL_READS_STATE", "CALL_WRITES_STATE",
    },
    "pdg": {
        "CFG", "EVAL_ORDER", "TRUE_BRANCH", "FALSE_BRANCH", "TRY_SUCCESS", "TRY_FAILURE", "CDG",
        "READS", "WRITES", "STATE_READ", "STATE_WRITE", "REACHING_DEF", "REF", "OPERAND", "ARGUMENT",
        "INDEX_BASE", "INDEX_KEY", "MEMBER_BASE", "MEMBER_FIELD", "VALUE_TO_ARGUMENT",
        "ARGUMENT_TO_PARAMETER", "RETURN_VALUE", "RETURN_TO_CALLER", "RECEIVER", "CALL_VALUE", "CALL_GAS", "CALL_READS_STATE", "CALL_WRITES_STATE",
    },
    "calls": {
        "INTERNAL_CALL", "EXTERNAL_CALL", "LIBRARY_CALL", "LOW_LEVEL_CALL", "SOLIDITY_CALL", "MODIFIER_CALL",
        "ETHER_SEND", "ETHER_TRANSFER", "DELEGATECALL", "STATICCALL", "CALLCODE", "DYNAMIC_DELEGATECALL",
        "XCFG_CALL", "XCFG_RETURN", "ARGUMENT", "VALUE_TO_ARGUMENT", "ARGUMENT_TO_PARAMETER",
        "RETURN_VALUE", "RETURN_TO_CALLER", "RECEIVER", "CALL_VALUE", "CALL_GAS", "CALL_READS_STATE", "CALL_WRITES_STATE",
    },
    "cpg": None,
}


def to_dot(graph: dict[str, Any], edge_labels: set[str] | None = None, representation: str | None = None) -> str:
    """Render a compact typed DOT view without requiring a Python graph library."""
    if edge_labels is not None and representation is not None:
        raise ValueError("Choose either explicit DOT edges or a DOT representation preset.")
    if representation is not None:
        if representation not in DOT_REPRESENTATIONS:
            raise ValueError(f"Unknown DOT representation: {representation}")
        edge_labels = DOT_REPRESENTATIONS[representation]

    def quote(value: Any) -> str:
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ') + '"'

    def quote_node_label(node: dict[str, Any]) -> str:
        value = f"{node['label']}\n{node['name']}"
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', ' ').replace('\n', '\\n') + '"'

    edges = [edge for edge in graph["links"] if edge_labels is None or edge["label"] in edge_labels]
    used_ids = {endpoint for edge in edges for endpoint in (edge["source"], edge["target"])}
    selected_nodes = [node for node in graph["nodes"] if edge_labels is None or node["id"] in used_ids]
    lines = ["digraph CPG {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    lines += [f"  {quote(node['id'])} [label={quote_node_label(node)}];" for node in selected_nodes]
    lines += [f"  {quote(edge['source'])} -> {quote(edge['target'])} [label={quote(edge['label'])}, fontsize=8];" for edge in edges]
    return "\n".join(lines + ["}", ""])
