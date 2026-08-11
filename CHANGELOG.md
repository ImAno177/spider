# Changelog

All notable changes to Spider are documented here. The project follows
[Semantic Versioning](https://semver.org/) for its package and CLI releases.

## [Unreleased]

### Documentation

- Rewrote the project introduction around the extractor's actual inputs,
  outputs, relation scope, and limitations.
- Expanded the schema reference and architecture description.

## [0.2.0] - 2026-08-11

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

[Unreleased]: https://github.com/ImAno177/spider/compare/672b99fc7f42cca05d8ff7b8183003915a43ba03...HEAD
[0.2.0]: https://github.com/ImAno177/spider/commit/672b99fc7f42cca05d8ff7b8183003915a43ba03
