from __future__ import annotations

import argparse
import re
import runpy
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
SCHEMA = ROOT / "spider" / "schema.py"
BADGE = re.compile(r'<img src="https://img\.shields\.io/badge/CPG-[^"]+" alt="CPG schema [^"]+">')


def badge(graph_format: str) -> str:
    value = quote(graph_format.replace("-", "--"), safe="")
    return f'<img src="https://img.shields.io/badge/CPG-{value}-111111" alt="CPG schema {graph_format}">'


def sync_text(text: str, graph_format: str) -> str:
    updated, count = BADGE.subn(badge(graph_format), text)
    if count != 1:
        raise ValueError(f"expected one CPG schema badge, found {count}")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize the README CPG badge with spider/schema.py.")
    parser.add_argument("--check", action="store_true", help="Fail instead of writing when the badge is stale")
    args = parser.parse_args()
    graph_format = runpy.run_path(str(SCHEMA))["GRAPH_FORMAT"]
    current = README.read_text(encoding="utf-8")
    updated = sync_text(current, graph_format)
    if args.check and updated != current:
        raise SystemExit(f"README CPG badge is stale; expected {graph_format}")
    if updated != current:
        README.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
