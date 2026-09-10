from pathlib import Path

import pytest

from spider.resolver import imports, resolve_closure


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_import_scanner_handles_forms_comments_and_strings() -> None:
    source = r'''
        // import "commented.sol";
        string constant S = "import 'inside-string.sol';";
        /* import ("block-comment.sol"); */
        import "direct.sol";
        import {Thing as Alias} from './named.sol';
        import * as Namespace from "./star.sol";
        import/*comment*/ "escaped\\name.sol";
    '''
    assert imports(source) == ["direct.sol", "./named.sol", "./star.sol", "escaped\\name.sol"]


def test_resolve_closure_preserves_relative_aliases_cycles_and_context_remapping(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(
        project,
        "contracts/A.sol",
        'import "./B.sol"; import "@dep/X.sol"; contract A {}',
    )
    _write(project, "contracts/B.sol", 'import "./A.sol"; contract B {}')
    _write(project, "vendor/pkg/X.sol", 'import "./Y.sol"; contract X {}')
    _write(project, "vendor/pkg/Y.sol", "contract Y {}")

    result = resolve_closure(
        project,
        ["contracts/A.sol"],
        ["@/=lib/general/", "contracts/:@dep/=vendor/pkg/"],
    )
    assert list(result) == ["contracts/A.sol", "contracts/B.sol", "vendor/pkg/X.sol", "vendor/pkg/Y.sol"]
    assert result["contracts/A.sol"] == (project / "contracts/A.sol").resolve()
    assert result["vendor/pkg/X.sol"] == (project / "vendor/pkg/X.sol").resolve()


def test_relative_import_is_normalized_before_context_remapping(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "contracts/A.sol", 'import "./B.sol"; contract A {}')
    dependency = _write(project, "src/B.sol", "contract B {}")
    result = resolve_closure(project, ["contracts/A.sol"], ["contracts/=src/"])
    assert result == {
        "contracts/A.sol": (project / "contracts/A.sol").resolve(),
        "src/B.sol": dependency.resolve(),
    }


def test_resolve_closure_uses_local_dependency_roots_without_basename_search(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "contracts/A.sol", 'import "pkg/X.sol"; contract A {}')
    dependency = _write(project, "node_modules/pkg/X.sol", "contract X {}")
    result = resolve_closure(project, ["contracts/A.sol"])
    assert result["pkg/X.sol"] == dependency.resolve()


def test_missing_dependency_reports_importer_import_and_tried_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "contracts/A.sol", 'import "missing/X.sol"; contract A {}')
    with pytest.raises(ValueError) as raised:
        resolve_closure(project, ["contracts/A.sol"])
    message = str(raised.value)
    assert "MISSING_DEPENDENCY" in message
    assert "contracts/A.sol" in message
    assert "missing/X.sol" in message
    assert "tried=[" in message


def test_ambiguous_dependency_reports_all_candidates(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "contracts/A.sol", 'import "pkg/X.sol"; contract A {}')
    node_module = _write(project, "node_modules/pkg/X.sol", "contract X {}")
    library = _write(project, "lib/pkg/X.sol", "contract X {}")
    with pytest.raises(ValueError) as raised:
        resolve_closure(project, ["contracts/A.sol"])
    message = str(raised.value)
    assert "AMBIGUOUS_DEPENDENCY" in message
    assert str(node_module) in message and str(library) in message


def test_external_remapping_is_explicit_and_keeps_absolute_source_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    _write(project, "contracts/A.sol", 'import "@external/X.sol"; contract A {}')
    dependency = _write(external, "X.sol", "contract X {}")
    result = resolve_closure(project, ["contracts/A.sol"], [f"@external/={external.as_posix()}/"])
    key = f"{external.resolve().as_posix()}/X.sol"
    assert result[key] == dependency.resolve()


def test_entry_and_relative_imports_cannot_escape_project_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "contracts/A.sol", 'import "../../outside.sol"; contract A {}')
    with pytest.raises(ValueError, match="source-unit path escapes project root"):
        resolve_closure(project, ["contracts/A.sol"])


def test_regular_file_symlink_payload_keeps_logical_source_name(tmp_path: Path) -> None:
    project = tmp_path / "project"
    alias = _write(project, "aliases/Target.sol", "../real/Target.sol")
    target = _write(project, "real/Target.sol", 'import "./Dependency.sol"; contract Target {}')
    dependency = _write(project, "aliases/Dependency.sol", "contract Dependency {}")

    result = resolve_closure(project, ["aliases/Target.sol"])

    assert result["aliases/Target.sol"] == target.resolve()
    assert result["aliases/Dependency.sol"] == dependency.resolve()
    assert alias.read_text(encoding="utf-8") == "../real/Target.sol"


@pytest.mark.parametrize(
    ("payload", "target_text", "error"),
    [
        ("../../outside.sol", "contract Outside {}", "SYMLINK_PAYLOAD_ESCAPE"),
        ("../missing.sol", None, "SYMLINK_PAYLOAD_MISSING"),
        ("../Target.txt", "not Solidity", "SYMLINK_PAYLOAD_NOT_SOLIDITY"),
    ],
)
def test_regular_file_symlink_payload_rejects_invalid_targets(
    tmp_path: Path, payload: str, target_text: str | None, error: str
) -> None:
    project = tmp_path / "project"
    link = _write(project, "aliases/Target.sol", payload)
    if target_text is not None:
        _write(project, f"{payload.rsplit('/', 1)[-1]}", target_text)

    with pytest.raises(ValueError, match=error):
        resolve_closure(project, ["aliases/Target.sol"])
    assert link.read_text(encoding="utf-8") == payload
