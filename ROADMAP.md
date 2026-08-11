# Roadmap

Spider prioritizes source-faithful Solidity semantics over speculative target
inference.

## Near term

- Add a stable query helper for common graph traversals without introducing a
  server runtime.
- Benchmark extraction and verification on larger multi-file projects.
- Publish a machine-readable schema reference and compatibility matrix.
- Expand fixtures for custom errors, user-defined value types, and newer Yul
  boundary cases.

## Research directions

- Storage-slot and alias modeling with explicit confidence boundaries.
- Optional context-sensitive interprocedural summaries.
- A maintained bridge to general CPG query engines.
- Structured Yul/EVM subgraphs that remain linked to Solidity source anchors.

## Non-goals

- Guessing proxy implementations, callbacks, or dynamic runtime targets.
- Replacing symbolic execution or a full smart-contract security audit.
- Hiding compiler, parser, or unsupported-semantics failures.
