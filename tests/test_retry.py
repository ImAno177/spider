import json
import subprocess
import sys
from pathlib import Path

import spider.batch as batch
import spider.solc as solc
from spider import ERROR_CLASSES, classify_compile_error
from spider.extract import ExtractError, extract
from spider.solc import compatible_project_versions, expand_candidates, pragmas_from, solc_candidates

CONSTRUCTOR_MSG = (
    "file.sol:5:5: ParserError: Expected identifier, got 'LParen'\n"
    "    constructor() public {\n"
    "               ^\n"
)


def _usable(requested: str) -> dict[str, str | bool]:
    return {"requested": requested, "reported": requested, "binary_sha256": "abc", "usable": True}


def test_constructor_lparen() -> None:
    assert classify_compile_error(CONSTRUCTOR_MSG) == "constructor_lparen"
    assert expand_candidates([["^0.4.18"]], [(0, 4, 18), (0, 4, 26)], True)[0] == "0.4.26"


def test_pragma_mismatch() -> None:
    assert classify_compile_error("Source file requires different compiler version") == "pragma_mismatch"
    versions = [(0, 4, 26), (0, 5, 17), (0, 6, 12)]
    assert compatible_project_versions(["^0.4.24", ">=0.5.0"], versions) == []
    union = expand_candidates([["^0.4.24"], [">=0.5.0"]], versions, False)
    families = {tuple(map(int, item.split(".")[:2])) for item in union}
    assert (0, 4) in families and (0, 5) in families


def test_missing_import_stops_ladder(tmp_path: Path, monkeypatch) -> None:
    assert classify_compile_error('Source "./A.sol" not found: File not found') == "missing_import"
    source = tmp_path / "Entry.sol"
    source.write_text("pragma solidity ^0.8.0;\nimport \"./A.sol\";\ncontract C {}\n", encoding="utf-8")
    calls: list[str] = []

    def fake_candidates(path, version=None, *, expand=True):
        return [("0.8.20", Path("/fake/solc-0.8.20")), ("0.8.10", Path("/fake/solc-0.8.10"))]

    class Boom:
        def __init__(self, *args, **kwargs):
            calls.append(str(kwargs.get("solc") or args[0]))
            raise RuntimeError('Source "./A.sol" not found: File not found')

    extract_mod = sys.modules["spider.extract"]
    monkeypatch.setattr(extract_mod, "solc_candidates", fake_candidates)
    monkeypatch.setattr(extract_mod, "Slither", Boom)
    try:
        extract(source)
    except ExtractError as error:
        assert all(item[2] == "missing_import" for item in error.attempts)
        assert {item[0] for item in error.attempts} == {"0.8.20"}
        assert len(calls) == 1
        assert "0.8.20//missing_import" in str(error)
    else:
        raise AssertionError("missing import did not stop the ladder")


def test_parser_undeclared_timeout_other() -> None:
    assert classify_compile_error("ParserError: Expected ';' but got 'Identifier'") == "parser"
    assert classify_compile_error("DeclarationError: Undeclared identifier.") == "undeclared"
    assert classify_compile_error("TimeoutExpired") == "timeout"
    assert classify_compile_error("") == "other"
    assert classify_compile_error(None) == "other"
    assert "parser" in ERROR_CLASSES and "undeclared" in ERROR_CLASSES


def test_back_compat_non_constructor(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "C.sol"
    source.write_text("pragma solidity ^0.8.10;\ncontract C {}\n", encoding="utf-8")
    versions = [(0, 6, 12), (0, 8, 10), (0, 8, 20)]
    monkeypatch.setattr(solc, "installed_solc_versions", lambda: versions)
    monkeypatch.setattr(solc, "compiler_fingerprint", _usable)
    monkeypatch.setattr(solc, "artifact_path", lambda item: Path(f"/solc/{item}"))
    old = compatible_project_versions(["^0.8.10"], versions)
    assert expand_candidates([["^0.8.10"]], versions, False)[0] == old[0]
    assert [item for item, _ in solc_candidates(source, expand=False)] == old


def test_pragmas_from_ignores_comments(tmp_path: Path) -> None:
    source = tmp_path / "Two.sol"
    source.write_text(
        "// pragma solidity 0.8.7;\n"
        "pragma solidity ^0.5.17;\n"
        "/* pragma solidity 0.7.4; */\n"
        "pragma solidity >=0.5.0 <0.6.0;\n"
        'contract C { string constant S = "// pragma solidity 0.8.0;"; }\n',
        encoding="utf-8",
    )
    assert pragmas_from(source) == ["^0.5.17", ">=0.5.0 <0.6.0"]
    assert solc.pragma_from(source) == "^0.5.17"


def test_batch_error_class(tmp_path: Path, monkeypatch) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "Bad.sol").write_text("pragma solidity ^0.8.0; contract C {", encoding="utf-8")
    review = tmp_path / "review"

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "ParserError: Expected ';' but got 'Identifier'")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["spider-batch", str(corpus), str(review)])
    batch.main()
    record = json.loads((review / "manifest.jsonl").read_text(encoding="utf-8"))
    summary = json.loads((review / "summary.json").read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert record["error_class"] == "parser"
    assert summary["error_classes"] == {"parser": 1}
