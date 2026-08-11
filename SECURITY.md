# Security policy

## Supported versions

Security fixes target the latest release on the default branch.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or a private Security Advisory for
`ImAno177/spider` when available. Do not publish exploit details in a public
issue.

Include:

- the affected Spider version or commit;
- the Solidity input and compiler version;
- impact and expected behavior;
- minimal reproduction steps; and
- whether the report concerns Spider itself or an analyzed contract.

This policy covers vulnerabilities in Spider, its packaging, and its processing
of untrusted Solidity input. Spider extracts program graphs; it does not certify
that an analyzed contract is secure.

Incorrect graph relations, unsupported contract semantics, and extraction
failures should normally be reported as correctness issues unless they create a
security impact in Spider or a downstream system.
