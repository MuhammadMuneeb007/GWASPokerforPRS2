"""Optional hand-offs to downstream tools. Nothing here is a hard dependency."""

from gwaspoker.integrations.gwaslab import (
    GwaslabResult,
    gwaslab_available,
    gwaslab_version,
    run_gwaslab,
)

__all__ = ["GwaslabResult", "gwaslab_available", "gwaslab_version", "run_gwaslab"]
