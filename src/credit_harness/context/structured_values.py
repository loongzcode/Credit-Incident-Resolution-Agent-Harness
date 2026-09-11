"""Closed types with partner-independent structured syntax; not free-text sanitization."""
from typing import Annotated

from pydantic import AfterValidator, Field


def not_bare_personal_number(value: str) -> str:
    # Numeric identifiers are deliberately not admitted as opaque refs here.
    # An adapter must tokenize/namespace numeric partner IDs, never pass raw PII.
    if value.isdigit() or (len(value) == 18 and value[:-1].isdigit() and value[-1] in "Xx"):
        raise ValueError("bare personal-number-shaped values are not opaque references")
    return value


OpaqueBusinessRef = Annotated[str, Field(strict=True, min_length=1, max_length=128,
                                       pattern=r"^[A-Za-z0-9_:/.-]+$"), AfterValidator(not_bare_personal_number)]
StructuredErrorCode = Annotated[str, Field(strict=True, min_length=2, max_length=64,
                                          pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
StructuredFieldPath = Annotated[str, Field(strict=True, min_length=1, max_length=128,
    pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,6}\])*(\.[A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,6}\])*)*$")]
StructuredTopic = Annotated[str, Field(strict=True, min_length=1, max_length=128,
    pattern=r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")]
