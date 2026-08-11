# Contributing to Spider

Contributions must preserve compiler-derived Solidity semantics, deterministic
output, and validator coverage.

## Development setup

```bash
git clone https://github.com/ImAno177/spider.git
cd spider
python -m pip install -e '.[dev]'
```

Install the Solidity compiler versions required by the fixtures:

```bash
solc-select install 0.4.25
solc-select install 0.8.11
solc-select install 0.8.20
```

## Required checks

Run all local gates:

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q spider tests scripts
python -m pip wheel . --no-deps --wheel-dir out/wheels
```

If a change affects compiler handling, graph semantics, imports, or
cross-contract behavior, also run `spider-batch` on a representative corpus.

## Change guidelines

- Add the smallest fixture that reproduces a semantic bug.
- Add a mutation check when the verifier should reject corrupted output.
- Keep source-resolved facts separate from heuristics. Never encode a guessed
  callback, proxy implementation, or runtime target as a fact.
- Preserve source spans and deterministic ordering.
- Update `README.md`, `docs/SCHEMA.md`, and `CHANGELOG.md` when the public graph
  or CLI changes.
- Do not weaken validation to accept output the extractor cannot justify.
- Keep generated graphs, wheels, caches, and corpus outputs out of commits.

## Pull request description

Include:

- the problem and its root cause
- the resulting semantic or interface change
- tests and corpus checks run
- graph-format or downstream compatibility impact

Keep unrelated refactors out of the same pull request.
