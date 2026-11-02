from __future__ import annotations

ERROR_CLASSES = (
    "constructor_lparen",
    "pragma_mismatch",
    "missing_import",
    "parser",
    "undeclared",
    "timeout",
    "other",
)


def classify_compile_error(text: str | None) -> str:
    """Return the first matching compile-error class, or other."""
    if not text:
        return "other"
    lowered = text.lower()
    if "timeoutexpired" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "expected identifier" in lowered and ("lparen" in lowered or "constructor" in lowered):
        return "constructor_lparen"
    if (
        "source file requires different compiler version" in lowered
        or "no installed solc satisfies" in lowered
        or "is not installed" in lowered
        or "parsererror: source file requires" in lowered
    ):
        return "pragma_mismatch"
    if (
        ('source "' in lowered and "not found" in lowered)
        or "file not found" in lowered
        or "file outside of allowed directories" in lowered
        or "source not found" in lowered
    ):
        return "missing_import"
    if "undeclared identifier" in lowered or "declarationerror" in lowered or "identifier not found or not unique" in lowered:
        return "undeclared"
    if "parsererror" in lowered or "expected " in lowered or "syntaxerror" in lowered:
        return "parser"
    return "other"
