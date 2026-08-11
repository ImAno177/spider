import argparse
import json
from pathlib import Path

from . import __version__
from .extract import DOT_REPRESENTATIONS, extract, to_dot
from .verify import validate


def _export_spec(value: str) -> tuple[str, Path]:
    mode, separator, destination = value.partition("=")
    if not separator or not mode or not destination:
        raise argparse.ArgumentTypeError("export must be MODE=PATH")
    if mode not in DOT_REPRESENTATIONS and not mode.startswith("edge:"):
        choices = ", ".join(sorted(DOT_REPRESENTATIONS))
        raise argparse.ArgumentTypeError(f"export mode must be one of {choices}, or edge:LABEL")
    if mode.startswith("edge:") and not mode.removeprefix("edge:"):
        raise argparse.ArgumentTypeError("edge export requires a label")
    return mode, Path(destination)


def main() -> None:
    parser = argparse.ArgumentParser(prog="spider", description="Extract a compiler-backed Solidity code property graph.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", type=Path, help="Solidity entry file or plain project directory")
    parser.add_argument("output", type=Path, help="NetworkX node-link JSON output")
    parser.add_argument("--export", action="append", type=_export_spec, metavar="MODE=PATH", help="Export a DOT view; repeat for multiple modes or use edge:LABEL=PATH")
    parser.add_argument("--solc-remap", action="append", help="Solidity import remapping; repeat when a project omits vendored dependencies")
    parser.add_argument("--solc-version", help="Require this installed solc version instead of selecting from the source pragma")
    args = parser.parse_args()

    graph = extract(args.source, args.solc_remap, solc_version=args.solc_version)
    errors = validate(graph)
    if errors:
        raise SystemExit("Invalid extracted graph:\n" + "\n".join(f"- {error}" for error in errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    destinations = [destination.resolve() for _, destination in args.export or []]
    if len(destinations) != len(set(destinations)):
        parser.error("each export must use a distinct path")
    for mode, destination in args.export or []:
        destination.parent.mkdir(parents=True, exist_ok=True)
        edge_labels = {mode.removeprefix("edge:")} if mode.startswith("edge:") else None
        representation = None if edge_labels else mode
        destination.write_text(to_dot(graph, edge_labels, representation), encoding="utf-8")
    print(f"Spider: {len(graph['nodes'])} nodes, {len(graph['links'])} edges -> {args.output}")


if __name__ == "__main__":
    main()
