"""GWASPoker: API-aware pre-download triage of GWAS summary statistics for PRS workflows.

GWASPoker answers one question cheaply, before a multi-gigabyte transfer is
committed to: *is this summary-statistics file usable for a polygenic risk
score?*

It answers it structurally first (GWAS Catalog REST metadata and the GWAS-SSF
``-meta.yaml`` sidecar), and only falls back to a bounded byte-range probe of the
data file when the structured route cannot settle the question.

GWASPoker is a decision layer, not a replacement for GWAS-SSF, the GWAS Catalog
APIs, GWASLab, MungeSumstats, PRSice, PLINK or LDpred2. It tells you which file
to hand to those tools.
"""

from __future__ import annotations

__version__ = "2.1.0"
__all__ = [
    "__version__",
    "GWASPokerConfig",
    "get_config",
    "FailureCategory",
    "FailureRecord",
    "GWASPokerError",
]

from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import FailureCategory, FailureRecord, GWASPokerError
