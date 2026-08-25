"""Structured assessment of summary statistics, without reading the data file.

Two routes are tried, cheapest and most authoritative first.

**Route 1 -- the GWAS-SSF ``-meta.yaml`` sidecar.** Every modern study directory
publishes a metadata file of roughly 700 bytes next to the data file. It states
``file_type``, and when that is ``GWAS-SSF v1.0`` the data file's mandatory
columns are fixed by the standard, in a defined order. PRS readiness then
follows from the declaration alone -- no byte of the (often multi-gigabyte) data
file needs to move. The sidecar also supplies ``genome_assembly``,
``sample_size``, ``case_control_study`` and ``data_file_md5sum``.

**Route 2 -- the legacy Summary Statistics REST API.** Withdrawn upstream:
``https://www.ebi.ac.uk/gwas/summary-statistics/api/...`` answers **HTTP 410
Gone** with *"This API has been deprecated."*, and the v2 API documentation
states that a replacement "is under development". GWASPoker still queries it,
because measuring its status is more honest than assuming it, and reports
:attr:`ApiAvailability.DEPRECATED` -- never "unavailable", which would imply a
transient fault.

If neither route settles the question, the caller falls back to
:mod:`gwaspoker.probe`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from gwaspoker.catalog.models import ApiAssessment, ApiAvailability, SsfMetadata
from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import GWASPokerError
from gwaspoker.http import HttpClient

logger = logging.getLogger(__name__)

#: Mandatory GWAS-SSF v1.0 fields, in the order the standard defines.
#: Position 4 is an effect measure: exactly one of ``beta``, ``odds_ratio``,
#: ``hazard_ratio`` or -- as a documented last resort -- ``z-score``.
SSF_MANDATORY_FIELDS: tuple[str, ...] = (
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "beta",
    "standard_error",
    "effect_allele_frequency",
    "p_value",
)

#: Alternatives permitted in the effect-measure slot.
SSF_EFFECT_ALTERNATIVES: tuple[str, ...] = ("beta", "odds_ratio", "hazard_ratio", "z-score")

#: Fields the standard encourages but does not require.
SSF_ENCOURAGED_FIELDS: tuple[str, ...] = ("variant_id", "rsid", "ci_upper", "ci_lower", "info", "n")


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v).strip())
    return ()


def parse_ssf_metadata(text: str, url: str = "") -> SsfMetadata:
    """Parse a GWAS-SSF ``-meta.yaml`` document.

    Pure function over text -- no network -- so it is directly unit-testable.
    """
    import yaml

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("meta.yaml did not parse to a mapping")

    meta = SsfMetadata(
        url=url,
        data_file_name=data.get("data_file_name"),
        file_type=data.get("file_type"),
        genome_assembly=data.get("genome_assembly"),
        coordinate_system=data.get("coordinate_system"),
        is_harmonised=_as_bool(data.get("is_harmonised")),
        is_sorted=_as_bool(data.get("is_sorted")),
        md5sum=data.get("data_file_md5sum"),
        analysis_software=data.get("analysis_software"),
        trait_description=_as_tuple(data.get("trait_description")),
        raw=data,
    )

    samples = data.get("samples")
    if isinstance(samples, list) and samples:
        total = 0
        found_total = False
        categories: list[str] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            size = _as_int(sample.get("sample_size"))
            if size is not None:
                total += size
                found_total = True
            cases = _as_int(sample.get("case_count"))
            controls = _as_int(sample.get("control_count"))
            if cases is not None:
                meta.case_count = (meta.case_count or 0) + cases
            if controls is not None:
                meta.control_count = (meta.control_count or 0) + controls
            cc = _as_bool(sample.get("case_control_study"))
            if cc is not None and meta.case_control_study is None:
                meta.case_control_study = cc
            categories.extend(_as_tuple(sample.get("sample_ancestry_category")))
        if found_total:
            meta.sample_size = total
        deduped: list[str] = []
        for cat in categories:
            if cat not in deduped:
                deduped.append(cat)
        meta.ancestry_categories = tuple(deduped)

    return meta


class SummaryStatisticsAssessor:
    """Builds an :class:`ApiAssessment` for a study, without touching the data file."""

    def __init__(
        self,
        config: Optional[GWASPokerConfig] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.config = config or get_config()
        self.http = http or HttpClient(self.config)

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------

    def fetch_ssf_metadata(self, metadata_url: str) -> Optional[SsfMetadata]:
        """Fetch and parse one ``-meta.yaml`` sidecar."""
        started = time.perf_counter()
        try:
            result = self.http.get(metadata_url)
        except GWASPokerError as exc:
            logger.debug("SSF metadata fetch failed for %s: %s", metadata_url, exc)
            return None
        if not result.ok:
            logger.debug("SSF metadata at %s returned HTTP %s", metadata_url, result.status_code)
            return None
        try:
            meta = parse_ssf_metadata(
                result.content.decode("utf-8", errors="replace"), metadata_url
            )
        except (ValueError, ImportError) as exc:
            logger.debug("SSF metadata at %s did not parse: %s", metadata_url, exc)
            return None
        meta.bytes_received = result.byte_count
        meta.latency_seconds = time.perf_counter() - started
        return meta

    def query_legacy_sumstats_api(
        self, accession: str, *, associations: int = 5
    ) -> tuple[ApiAvailability, int, float, tuple[str, ...], Optional[str]]:
        """Query the withdrawn association endpoint and classify the answer.

        Returns ``(availability, bytes_received, latency, fields, error)``.
        The endpoint is expected to answer 410; the point of asking is to record
        that fact per study rather than assert it.
        """
        url = f"{self.config.sumstats_api_base}/studies/{accession}/associations"
        started = time.perf_counter()
        try:
            payload, result = self.http.get_json(url, params={"size": associations})
        except GWASPokerError as exc:
            latency = time.perf_counter() - started
            availability = (
                ApiAvailability.TIMEOUT
                if "timeout" in str(exc).lower()
                else ApiAvailability.SERVER_ERROR
            )
            return availability, 0, latency, (), str(exc)

        latency = time.perf_counter() - started
        size = result.byte_count

        if result.status_code == 410:
            return (
                ApiAvailability.DEPRECATED,
                size,
                latency,
                (),
                "The GWAS Catalog Summary Statistics API has been withdrawn (HTTP 410 Gone).",
            )
        if result.status_code == 404:
            return (
                ApiAvailability.NOT_REPRESENTED,
                size,
                latency,
                (),
                f"{accession} is not served by this endpoint (HTTP 404).",
            )
        if 500 <= result.status_code < 600:
            return (
                ApiAvailability.SERVER_ERROR,
                size,
                latency,
                (),
                f"HTTP {result.status_code} from the summary statistics API.",
            )
        if not result.ok:
            return (
                ApiAvailability.SERVER_ERROR,
                size,
                latency,
                (),
                f"HTTP {result.status_code} from the summary statistics API.",
            )
        if not isinstance(payload, dict):
            return (
                ApiAvailability.SCHEMA_ERROR,
                size,
                latency,
                (),
                "Response body was not a JSON object.",
            )

        fields = _extract_association_fields(payload)
        if not fields:
            return (
                ApiAvailability.SCHEMA_ERROR,
                size,
                latency,
                (),
                "No association records found in the response.",
            )
        return ApiAvailability.AVAILABLE, size, latency, fields, None

    # ------------------------------------------------------------------

    def assess(
        self,
        accession: Optional[str],
        *,
        metadata_url: Optional[str] = None,
        query_legacy_api: bool = True,
        associations: int = 5,
    ) -> ApiAssessment:
        """Try to settle PRS readiness from structured sources alone."""
        assessment = ApiAssessment(study_accession=accession)
        endpoints: list[str] = []
        notes: list[str] = []
        total_bytes = 0
        total_latency = 0.0

        # Route 1: the GWAS-SSF sidecar.
        if metadata_url:
            endpoints.append(metadata_url)
            meta = self.fetch_ssf_metadata(metadata_url)
            if meta is not None:
                assessment.ssf_metadata = meta
                total_bytes += meta.bytes_received
                total_latency += meta.latency_seconds
                if meta.is_ssf:
                    assessment.available = True
                    assessment.availability = ApiAvailability.AVAILABLE
                    assessment.sufficient_for_prs_assessment = True
                    assessment.route = "gwas_ssf_metadata"
                    assessment.fields_available = SSF_MANDATORY_FIELDS
                    notes.append(
                        f"File declares {meta.file_type}; the mandatory column set is "
                        "fixed by the standard, so the data file need not be read."
                    )
                else:
                    assessment.available = True
                    assessment.availability = ApiAvailability.AVAILABLE
                    assessment.route = "gwas_ssf_metadata"
                    notes.append(
                        f"Metadata declares file_type={meta.file_type!r}; the column set "
                        "is not guaranteed, so file-level inspection is required."
                    )
            else:
                notes.append("No GWAS-SSF metadata sidecar was retrievable for this file.")

        # Route 2: the withdrawn association endpoint.
        if query_legacy_api and accession:
            legacy_url = f"{self.config.sumstats_api_base}/studies/{accession}/associations"
            endpoints.append(legacy_url)
            availability, size, latency, fields, error = self.query_legacy_sumstats_api(
                accession, associations=associations
            )
            total_bytes += size
            total_latency += latency
            assessment.associations_requested = associations

            if availability is ApiAvailability.AVAILABLE:
                assessment.available = True
                assessment.availability = ApiAvailability.AVAILABLE
                if not assessment.fields_available:
                    assessment.fields_available = fields
                    assessment.route = "summary_statistics_api"
                    assessment.sufficient_for_prs_assessment = _fields_cover_prs(fields)
                notes.append(
                    f"Summary Statistics API returned {len(fields)} standardized field(s)."
                )
            else:
                if error:
                    notes.append(error)
                if not assessment.available:
                    assessment.availability = availability
                    assessment.error = error

        assessment.bytes_received = total_bytes
        assessment.latency_seconds = total_latency
        assessment.endpoints_tried = tuple(endpoints)
        assessment.notes = tuple(notes)
        if assessment.availability is ApiAvailability.NOT_QUERIED and endpoints:
            assessment.availability = ApiAvailability.NOT_REPRESENTED
        return assessment


def _extract_association_fields(payload: dict[str, Any]) -> tuple[str, ...]:
    """Pull the field names out of an association-list response.

    Kept tolerant of shape because the replacement API is not published yet;
    when it appears, only this function should need revisiting.
    """
    embedded = payload.get("_embedded")
    records: Any = None
    if isinstance(embedded, dict):
        for value in embedded.values():
            if isinstance(value, dict) and value:
                records = next(iter(value.values()), None)
                break
            if isinstance(value, list) and value:
                records = value[0]
                break
    if records is None:
        for key in ("associations", "content", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                records = value[0]
                break
            if isinstance(value, dict) and value:
                records = next(iter(value.values()), None)
                break
    if isinstance(records, dict):
        return tuple(k for k in records if not k.startswith("_"))
    return ()


def _fields_cover_prs(fields: tuple[str, ...]) -> bool:
    """True if a field list supports a PRS readiness verdict on its own.

    Deliberately strict: it demands allele identity, an effect measure, a
    p-value and a locus. A partial field list is reported as insufficient so
    that the file probe runs, rather than guessed at.
    """
    lowered = {f.lower() for f in fields}

    def has(*names: str) -> bool:
        return any(n in lowered for n in names)

    locus = has("chromosome", "chr", "chromosome_position") and has(
        "base_pair_location", "position", "bp"
    )
    locus = locus or has("variant_id", "rsid", "rs_id")
    allele = has("effect_allele") and has("other_allele")
    effect = has("beta", "odds_ratio", "hazard_ratio", "z-score", "z_score")
    pvalue = has("p_value", "p-value", "pvalue")
    return bool(locus and allele and effect and pvalue)
