import pytest

from scripts.sync_schema_badge import badge, sync_text


def test_schema_badge_sync() -> None:
    old = '<img src="https://img.shields.io/badge/CPG-old%2F0.1-111111" alt="CPG schema old/0.1">'
    expected = '<img src="https://img.shields.io/badge/CPG-spider--cpg%2F2.0-111111" alt="CPG schema spider-cpg/2.0">'
    assert badge("spider-cpg/2.0") == expected
    assert sync_text(old, "spider-cpg/2.0") == expected
    with pytest.raises(ValueError, match="expected one"):
        sync_text("no badge", "spider-cpg/2.0")
