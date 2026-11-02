"""Public extraction facade and Slither project compilation."""

from __future__ import annotations

import os
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson
from slither.slither import Slither
from slither.slithir import convert as _slither_convert
from slither.slithir.operations import Assignment, HighLevelCall
from slither.slithir.variables import TupleVariable
from slither.visitors.slithir import expression_to_slithir as _slither_expression

from ._builder import build_graph as _build_graph
from ._graph import DOT_REPRESENTATIONS, to_dot
from .errors import classify_compile_error
from .solc import first_solidity_segment, solc_candidates, solidity_sources

__all__ = ["DOT_REPRESENTATIONS", "ExtractError", "extract", "to_dot"]


class ExtractError(RuntimeError):
    """Raised when every solc candidate in the retry ladder fails."""

    def __init__(self, message: str, attempts: list[tuple[str, str, str]]) -> None:
        super().__init__(message)
        self.attempts = attempts


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


def _project_compilation(
    project: Path,
    source_files: list[Path],
    solc: Path,
    solc_args: str,
    solc_remaps: list[str] | None,
    contents: dict[Path, str] | None = None,
) -> Slither:
    sources = {
        source.relative_to(project).as_posix(): {
            "content": contents[source] if contents is not None and source in contents else source.read_text(encoding="utf-8", errors="replace")
        }
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
    compile_contents: dict[Path, str] = {}
    for source in source_files:
        text = source.read_text(encoding="utf-8", errors="replace")
        segment = first_solidity_segment(text)
        if segment != text:
            compile_contents[source] = segment
    try:
        max_tries = int(os.environ.get("SPIDER_MAX_SOLC_TRIES", "8"))
    except ValueError:
        max_tries = 8
    attempt_log: list[tuple[str, str, str]] = []
    tried_versions: set[str] = set()
    stop = False
    prefix_dir = tempfile.TemporaryDirectory() if compile_contents and not project_input else nullcontext()
    with prefix_dir as tmp:
        dest = Path(tmp) / path.name if tmp else None
        if dest is not None:
            dest.write_text(compile_contents[path], encoding="utf-8")
        for selected_solc, solc in solc_candidates(path, solc_version):
            if selected_solc not in tried_versions:
                if len(tried_versions) >= max_tries:
                    break
                tried_versions.add(selected_solc)
            attempts = ["", "--optimize"]
            if tuple(map(int, selected_solc.split("."))) >= (0, 8, 13):
                attempts.append("--via-ir --optimize")
            for selected_solc_args in attempts:
                try:
                    if project_input:
                        compiled = _project_compilation(path, source_files, solc, selected_solc_args, solc_remaps, compile_contents or None)
                    elif dest is not None:
                        compiled = Slither(str(dest), solc_remaps=solc_remaps, solc=str(solc), solc_args=selected_solc_args)
                    else:
                        # solc 0.8.11 on Windows drops the drive prefix from absolute
                        # source-unit names. Compile a drive-relative target while
                        # preserving its directory hierarchy for relative imports.
                        compiled = Slither(compiler_target, solc_remaps=solc_remaps, solc=str(solc), solc_args=selected_solc_args, **compiler_working_dir)
                    if not compiled.contracts:
                        raise RuntimeError("No contracts were found")
                    slither = compiled
                    break
                except (Exception, SystemExit) as error:
                    last_error = error
                    error_class = classify_compile_error(str(error))
                    attempt_log.append((selected_solc, selected_solc_args, error_class))
                    if error_class == "missing_import":
                        stop = True
                        break
            if slither is not None or stop:
                break
        if slither is None:
            last_text = "" if last_error is None else str(last_error)
            tried = "; ".join(f"{version}/{args}/{cls}" for version, args, cls in attempt_log)
            raise ExtractError(f"{last_text}\n[spider] tried: {tried}", attempt_log) from last_error
        return _build_graph(slither, path, sources, source_files, selected_solc, selected_solc_args, project_input)
