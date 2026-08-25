"""Value-domain validation: does the data support what the header claimed?

Header names propose canonical concepts; sampled values test whether those
concepts are plausible. The two lines of evidence are kept separate throughout,
and GWASPoker reports contradictions rather than silently correcting them.

This is structural sanity checking, not GWAS quality control. See
docs/MAPPING_SCHEMA.md.
"""

from gwaspoker.validation.values import (
    CONCEPT_RULES,
    DEFAULT_MAX_ROWS,
    ColumnValueValidation,
    ValueStatus,
    ValueValidationResult,
    is_missing,
    validate_values,
)

__all__ = [
    "CONCEPT_RULES",
    "DEFAULT_MAX_ROWS",
    "ColumnValueValidation",
    "ValueStatus",
    "ValueValidationResult",
    "is_missing",
    "validate_values",
]
