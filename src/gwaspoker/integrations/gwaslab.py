"""Optional GWASLab hand-off.

GWASPoker stops where GWASLab starts. It decides *which* file to download;
GWASLab does the QC, harmonisation, liftover and format conversion that PRS
pipelines need. None of that is reimplemented here.

The integration is a single function that loads a downloaded file into a
``gwaslab.Sumstats`` object, using the canonical mapping GWASPoker already
established, and optionally writes a standardised output. If GWASLab is not
installed, that is reported as a fact, not an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gwaspoker.mapping.mapper import MappingResult

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "GWASLab integration unavailable.\n"
    'Install the optional dependency with:\n    pip install "gwaspoker[gwaslab]"'
)

#: GWASPoker canonical concept -> the keyword ``gwaslab.Sumstats`` expects.
_GWASLAB_KEYWORDS: dict[str, str] = {
    "variant_id": "snpid",
    "chromosome": "chrom",
    "position": "pos",
    "effect_allele": "ea",
    "other_allele": "nea",
    "effect_allele_frequency": "eaf",
    "beta": "beta",
    "odds_ratio": "OR",
    "standard_error": "se",
    "p_value": "p",
    "neg_log10_p_value": "mlog10p",
    "z_score": "z",
    "sample_size": "n",
    "info_score": "info",
    "direction": "direction",
    "n_studies": "n_case",
}


@dataclass
class GwaslabResult:
    """Outcome of the GWASLab hand-off."""

    available: bool
    succeeded: bool = False
    detected_format: Optional[str] = None
    variant_count: Optional[int] = None
    output_path: Optional[Path] = None
    gwaslab_version: Optional[str] = None
    keywords_used: dict[str, str] = field(default_factory=dict)
    message: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "succeeded": self.succeeded,
            "detected_format": self.detected_format,
            "variant_count": self.variant_count,
            "output_path": str(self.output_path) if self.output_path else None,
            "gwaslab_version": self.gwaslab_version,
            "keywords_used": dict(self.keywords_used),
            "message": self.message,
            "error": self.error,
        }


def gwaslab_available() -> bool:
    """True if GWASLab is importable. Does not import it."""
    import importlib.util

    return importlib.util.find_spec("gwaslab") is not None


def gwaslab_version() -> Optional[str]:
    """Installed GWASLab version, or ``None``."""
    if not gwaslab_available():
        return None
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("gwaslab")
    except (ImportError, PackageNotFoundError):
        return None


def build_keyword_arguments(mapping: Optional[MappingResult]) -> dict[str, str]:
    """Translate a GWASPoker mapping into ``gwaslab.Sumstats`` keywords.

    Passing an explicit mapping is better than relying on GWASLab's own
    auto-detection when GWASPoker has already resolved the columns with a
    curated vocabulary -- and when it has not (``unknown`` columns), letting
    GWASLab decide is the honest choice.
    """
    if mapping is None:
        return {}
    keywords: dict[str, str] = {}
    for concept, columns in mapping.by_concept().items():
        keyword = _GWASLAB_KEYWORDS.get(concept)
        if not keyword or len(columns) != 1:
            continue
        # Only pass mappings we are confident about; leave the rest to GWASLab.
        if columns[0].confidence >= 0.9:
            keywords[keyword] = columns[0].raw_name
    return keywords


def run_gwaslab(
    path: Path,
    *,
    mapping: Optional[MappingResult] = None,
    output_path: Optional[Path] = None,
    build: Optional[str] = None,
    basic_check: bool = True,
) -> GwaslabResult:
    """Load a downloaded file into GWASLab and optionally write a standardised copy.

    Returns a result object in every case; the absence of GWASLab is reported
    through ``available=False`` rather than raised.
    """
    if not gwaslab_available():
        return GwaslabResult(available=False, message=INSTALL_HINT)

    path = Path(path)
    result = GwaslabResult(available=True, gwaslab_version=gwaslab_version())

    if not path.is_file():
        result.error = f"No such file: {path}"
        return result

    try:
        import gwaslab as gl
    except ImportError as exc:  # pragma: no cover - guarded by gwaslab_available
        result.available = False
        result.message = INSTALL_HINT
        result.error = str(exc)
        return result

    keywords = build_keyword_arguments(mapping)
    result.keywords_used = keywords

    try:
        sumstats = gl.Sumstats(str(path), fmt="auto", build=build, **keywords)
        result.detected_format = "auto"
    except (TypeError, ValueError, KeyError) as exc:
        # "auto" format detection failed; retry with only the explicit keywords,
        # which is exactly what GWASPoker's mapping is for.
        logger.debug("GWASLab auto-detection failed (%s); retrying with explicit columns", exc)
        if not keywords:
            result.error = (
                f"GWASLab could not read {path.name} and GWASPoker has no confident "
                f"column mapping to supply: {exc}"
            )
            return result
        try:
            sumstats = gl.Sumstats(str(path), build=build, **keywords)
            result.detected_format = "gwaspoker_mapping"
        except (TypeError, ValueError, KeyError) as retry_exc:
            result.error = f"GWASLab could not read {path.name}: {retry_exc}"
            return result
    except Exception as exc:  # GWASLab raises assorted types on malformed input
        result.error = f"GWASLab failed on {path.name}: {type(exc).__name__}: {exc}"
        return result

    try:
        if basic_check:
            sumstats.basic_check()
        data = getattr(sumstats, "data", None)
        if data is not None:
            result.variant_count = int(len(data))

        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # to_format appends its own suffix, so pass the stem.
            sumstats.to_format(str(output_path.with_suffix("")), fmt="gwaslab")
            result.output_path = output_path
    except Exception as exc:  # noqa: BLE001 - GWASLab error types are not public
        result.error = f"GWASLab post-processing failed: {type(exc).__name__}: {exc}"
        result.succeeded = False
        return result

    result.succeeded = True
    result.message = (
        f"GWASLab {result.gwaslab_version or ''} loaded {result.variant_count or 0:,} "
        f"variants from {path.name}."
    ).strip()
    return result
