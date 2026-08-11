# Changelog

All notable changes to Spider are documented here. The project follows
[Semantic Versioning](https://semver.org/) for its package and CLI releases.

## Unreleased

## 0.3.0 - 2026-08-11

### Added

- Directory input for compiling a complete Solidity project into one validated
  graph.
- Common compiler selection across all discovered project pragmas.
- Standard JSON compilation with local remapping dependencies and original
  source anchors.
- `input_kind` and `input_sources` graph metadata.
- Project, duplicate-contract-name, and static-library-call regression coverage,
  plus real DAppSCAN validation gates.

### Fixed

- Contract-level inline-assembly validation now follows `CONTAINS` identity
  instead of contract names, which may repeat across source units.
- The Slither 0.11.5 compatibility shim now handles contract-valued call
  destinations without dropping SlithIR generation.
- The SVG logo uses one background and one crisp path; obsolete horizontal
  rows and per-pixel white rectangles were removed.

### Changed

- GitHub Actions push and pull-request tests now run only when files under
  `spider/` change.

### Documentation

- Documented file, whole-project, and corpus extraction as separate workflows.
- Kept installation metadata in `pyproject.toml` and documented both automatic
  and explicit dependency installation commands.
- Expanded the graph metadata and project-construction contracts.
- Moved the changelog and roadmap into `docs/` while keeping GitHub-discovered
  contribution and security files at the repository root.

## 0.2.0 - 2026-08-11

### Added

- The standalone `spider` Python package and the `spider`, `spider-batch`, and
  `spider-verify` commands.
- Exact `INDEX_BASE`, `INDEX_KEY`, `MEMBER_BASE`, and `MEMBER_FIELD` relations.
- `VALUE_TO_ARGUMENT` producer flow based on compiler-backed reaching
  definitions.
- Source-resolved return-to-caller and direct/transitive state-effect summaries.
- Repeated `--export MODE=PATH` output for AST, CFG, CDG, DDG, PDG, calls,
  complete CPG, and exact-edge views from one compilation.
- Structural and mutation validation for the new relations.

### Changed

- Indexed verifier lookups replace repeated full-edge scans on hot paths.
- Call argument producer lookup is indexed by callsite and variable.
- DOT node labels use real Graphviz line breaks.
- Windows compiler targets are drive-relative to support solc 0.8.11 imports.

### Packaging and schema

- The graph schema is `spider-cpg/1.0` and the Python import path is `spider`.
- The public commands are `spider`, `spider-batch`, and `spider-verify`.
- Source positions use UTF-8 byte offsets; duplicate character-offset fields
  are not part of the schema.
- Downstream graph artifacts and vocabularies must be rebuilt.
