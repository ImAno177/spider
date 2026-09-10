"""Public extraction facade and Slither project compilation."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson
from slither.exceptions import SlitherException
from slither.slither import Slither
from slither.slithir import convert as _slither_convert
from slither.slithir.operations import Assignment, HighLevelCall
from slither.slithir.variables import TupleVariable
from slither.solc_parsing import slither_compilation_unit_solc as _slither_compilation_unit_solc
from slither.visitors.expression.constants_folding import ConstantFolding as _slither_constant_folding
from slither.visitors.slithir import expression_to_slithir as _slither_expression

from ._builder import build_graph as _build_graph
from ._graph import DOT_REPRESENTATIONS, to_dot
from .solc import solc_candidates, solidity_sources

__all__ = ["DOT_REPRESENTATIONS", "extract", "to_dot"]


def _solidity_tokens(source: str) -> list[tuple[str, str]]:
    """Tokenize enough Solidity to read named import aliases without regex."""
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            index = len(source) if closing < 0 else closing + 2
            continue
        if character in {"'", '"'}:
            quote = character
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                end += 1
                if source[end - 1] == quote:
                    break
            tokens.append(("string", source[index + 1 : end - 1] if source[end - 1 : end] == quote else source[index + 1 : end]))
            index = end
            continue
        if character.isalpha() or character in "_$":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            tokens.append(("identifier", source[index:end]))
            index = end
            continue
        tokens.append((character, character))
        index += 1
    return tokens


def _solidity_import_aliases(source: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return import paths and ``(foreign, local)`` named-import pairs."""
    tokens = _solidity_tokens(source)
    imports: list[tuple[str, list[tuple[str, str]]]] = []
    index = 0
    while index < len(tokens):
        if tokens[index] != ("identifier", "import"):
            index += 1
            continue
        cursor = index + 1
        aliases: list[tuple[str, str]] = []
        if cursor < len(tokens) and tokens[cursor] == ("{", "{"):
            cursor += 1
            while cursor < len(tokens) and tokens[cursor] != ("}", "}"):
                if tokens[cursor][0] != "identifier":
                    cursor += 1
                    continue
                foreign = tokens[cursor][1]
                local = foreign
                cursor += 1
                if cursor < len(tokens) and tokens[cursor] == ("identifier", "as"):
                    cursor += 1
                    if cursor >= len(tokens) or tokens[cursor][0] != "identifier":
                        break
                    local = tokens[cursor][1]
                    cursor += 1
                aliases.append((foreign, local))
                if cursor < len(tokens) and tokens[cursor] == (",", ","):
                    cursor += 1
            if cursor < len(tokens) and tokens[cursor] == ("}", "}"):
                cursor += 1
        while cursor < len(tokens) and tokens[cursor][0] != "string" and tokens[cursor] not in {
            ("identifier", "from"),
            (";", ";"),
        }:
            cursor += 1
        if cursor < len(tokens) and tokens[cursor] == ("identifier", "from"):
            cursor += 1
        if cursor < len(tokens) and tokens[cursor][0] == "string":
            imports.append((tokens[cursor][1], aliases))
            index = cursor
        else:
            index += 1
    return imports


def _recover_import_alias(local_name: str, import_directive: Any, scope: Any) -> str:
    """Recover a solc 0.5.x numeric alias from the original import statement."""
    source_path = Path(scope.filename.absolute)
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SlitherException(f"Cannot recover import alias {local_name!r}: cannot read {source_path}: {error}") from error
    imported = posixpath.normpath(str(getattr(import_directive, "_filename", "")).replace("\\", "/"))
    used = posixpath.normpath(str(scope.filename.used).replace("\\", "/"))
    candidates: list[tuple[int, str]] = []
    for raw_path, aliases in _solidity_import_aliases(source):
        raw = posixpath.normpath(raw_path.replace("\\", "/"))
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(used), raw))
        score = 2 if raw == imported or resolved == imported else 0
        for foreign, local in aliases:
            if local == local_name:
                candidates.append((score, foreign))
    if not candidates:
        raise SlitherException(f"Cannot recover import alias {local_name!r} in {source_path}")
    best_score = max(score for score, _ in candidates)
    names = {name for score, name in candidates if score == best_score}
    if len(names) != 1:
        raise SlitherException(f"Ambiguous import alias {local_name!r} in {source_path}: {sorted(names)}")
    return next(iter(names))


_slither_import_aliases = _slither_compilation_unit_solc._handle_import_aliases


def _handle_import_aliases_with_recovery(symbol_aliases: list[dict[str, Any]], import_directive: Any, scope: Any) -> None:
    recovered: list[dict[str, Any]] = []
    for alias in symbol_aliases:
        foreign = alias.get("foreign")
        if isinstance(foreign, int) and not isinstance(foreign, bool):
            foreign = {"name": _recover_import_alias(alias["local"], import_directive, scope)}
        recovered.append({**alias, "foreign": foreign})
    _slither_import_aliases(recovered, import_directive, scope)


# solc 0.5.12 emits numeric ``symbolAliases`` references. Slither rejects them
# even though the original import statement contains the exact source name.
_slither_compilation_unit_solc._handle_import_aliases = _handle_import_aliases_with_recovery

# Slither 0.11.5 leaves a single-return call type wrapped in a list, then tries
# to use that list as a dict key. Normalize the representation before its own
# type propagation; remove this shim when upstream fixes the conversion.
_propagate_types = _slither_convert.propagate_types


def _propagate_single_type(ir: Any, node: Any) -> Any:
    destination_type = getattr(getattr(ir, "destination", None), "type", None)
    if isinstance(ir, HighLevelCall) and isinstance(destination_type, list) and len(destination_type) == 1:
        ir.destination.set_type(destination_type[0])
    return _propagate_types(ir, node)


_slither_convert.propagate_types = _propagate_single_type


# Slither 0.11.5 assumes every tuple-shaped assignment has a tuple RHS. Solidity
# 0.4 also permits `(bool ok,) = address.call(...)`, whose RHS is a single bool.
_post_assignment = _slither_expression.ExpressionToSlithIR._post_assignement_operation


def _assign_single_tuple_call(visitor: Any, expression: Any) -> None:
    left = expression.expression_left.context.get(_slither_expression.key)
    right = expression.expression_right.context.get(_slither_expression.key)
    targets = [target for target in left if target is not None] if isinstance(left, list) else []
    if len(targets) == 1 and right is not None and not isinstance(right, (list, TupleVariable)):
        _slither_expression.get(expression.expression_left)
        _slither_expression.get(expression.expression_right)
        operation = Assignment(targets[0], right, targets[0].type)
        operation.set_expression(expression)
        visitor._result.append(operation)
        _slither_expression.set_val(expression, None)
        return
    _post_assignment(visitor, expression)


_slither_expression.ExpressionToSlithIR._post_assignement_operation = _assign_single_tuple_call


# Slither does not rewrite ternaries in its synthetic state-initializer
# function.  Collapse only conditions proven constant by Slither itself; a
# runtime-dependent ternary keeps the upstream SlithIR error instead of being
# approximated.
_visit_conditional = _slither_expression.ExpressionToSlithIR._visit_conditional_expression
_post_conditional = _slither_expression.ExpressionToSlithIR._post_conditional_expression


def _visit_constant_conditional(visitor: Any, expression: Any) -> None:
    try:
        folded = _slither_constant_folding(expression.if_expression, "bool").result()
    except Exception:
        _visit_conditional(visitor, expression)
        return
    selected = expression.then_expression if bool(folded.value) else expression.else_expression
    visitor._visit_expression(selected)
    _slither_expression.set_val(expression, _slither_expression.get(selected))


def _post_constant_conditional(visitor: Any, expression: Any) -> None:
    if _slither_expression.key in expression.context:
        return
    _post_conditional(visitor, expression)


_slither_expression.ExpressionToSlithIR._visit_conditional_expression = _visit_constant_conditional
_slither_expression.ExpressionToSlithIR._post_conditional_expression = _post_constant_conditional


def _project_compilation(
    project: Path,
    source_files: list[Path],
    solc: Path,
    solc_args: str,
    solc_remaps: list[str] | None,
) -> Slither:
    sources = {
        source.relative_to(project).as_posix(): {"content": source.read_text(encoding="utf-8")}
        for source in source_files
    }
    remappings: list[str] = []
    for remapping in solc_remaps or []:
        prefix, separator, target = remapping.rpartition("=")
        if not separator:
            remappings.append(remapping)
            continue
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = project / target_path
        if not target_path.is_dir():
            remappings.append(remapping)
            continue
        target_path = target_path.resolve()
        try:
            normalized_target = target_path.relative_to(project).as_posix().rstrip("/") + "/"
        except ValueError:
            normalized_target = target_path.as_posix().rstrip("/") + "/"
        remappings.append(f"{prefix}={normalized_target}")
        for dependency in solidity_sources(target_path):
            key = normalized_target + dependency.relative_to(target_path).as_posix()
            content = dependency.read_text(encoding="utf-8")
            previous = sources.get(key)
            if previous is not None and previous["content"] != content:
                raise ValueError(f"remapping source resolves to conflicting contents: {key}")
            sources[key] = {"content": content}

    standard_json = SolcStandardJson({"language": "Solidity", "sources": sources, "settings": {"remappings": remappings}})
    if "--optimize" in solc_args:
        standard_json.to_dict()["settings"]["optimizer"] = {"enabled": True}
    if "--via-ir" in solc_args:
        standard_json.to_dict()["settings"]["viaIR"] = True
    compilation = CryticCompile(standard_json, solc=str(solc), solc_args=solc_args, solc_working_dir=str(project))
    return Slither(compilation)


def extract(path: str | Path, solc_remaps: list[str] | None = None, solc_version: str | None = None) -> dict[str, Any]:
    """Return one CPG for a Solidity entry file or plain project directory."""
    path = Path(path).resolve()
    project_input = path.is_dir()
    source_files = solidity_sources(path)
    sources = {str(source): source.read_bytes() for source in source_files}
    last_error: BaseException | None = None
    slither: Slither | None = None
    selected_solc_args = ""
    compiler_target = path.relative_to(path.anchor).as_posix() if path.drive else str(path)
    compiler_working_dir = {"solc_working_dir": path.anchor} if path.drive else {}
    for selected_solc, solc in solc_candidates(path, solc_version):
        attempts = ["", "--optimize"]
        if tuple(map(int, selected_solc.split("."))) >= (0, 8, 13):
            attempts.append("--via-ir --optimize")
        for selected_solc_args in attempts:
            try:
                if project_input:
                    slither = _project_compilation(path, source_files, solc, selected_solc_args, solc_remaps)
                else:
                    # solc 0.8.11 on Windows drops the drive prefix from absolute
                    # source-unit names. Compile a drive-relative target while
                    # preserving its directory hierarchy for relative imports.
                    slither = Slither(compiler_target, solc_remaps=solc_remaps, solc=str(solc), solc_args=selected_solc_args, **compiler_working_dir)
                break
            except (Exception, SystemExit) as error:
                last_error = error
        if slither is not None:
            break
    if slither is None:
        assert last_error is not None
        raise last_error
    return _build_graph(slither, path, sources, source_files, selected_solc, selected_solc_args, project_input)
