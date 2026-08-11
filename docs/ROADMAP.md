# Roadmap

This roadmap records intended work, not release commitments. Changes must keep
inferred relations separate from compiler-resolved facts.

## Planned

- Add query helpers for common graph traversals.
- Publish a machine-readable schema reference and version compatibility table.
- Extend directory extraction to framework-managed build configurations while
  preserving the plain Standard JSON path.
- Benchmark extraction and validation on larger multi-file projects.
- Expand compiler and fixture coverage for custom errors, user-defined value
  types, and recent Solidity syntax.

## Under consideration

- Storage-slot and alias relations with explicit evidence and confidence.
- Optional context-sensitive interprocedural summaries.
- Structured Yul and EVM subgraphs linked to Solidity source anchors.

## Out of scope

- Guessing proxy implementations, callbacks, or dynamic runtime targets.
- Replacing symbolic execution or a full smart-contract security audit.
- Treating compiler, parser, or unsupported-semantics failures as successful
  extraction.
