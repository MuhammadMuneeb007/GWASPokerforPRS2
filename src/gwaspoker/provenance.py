"""Provenance records for reproducible experiments.

The manuscript needs to be able to say, for any reported number, exactly how it
was produced. A :class:`ProvenanceRecord` captures the run environment plus the
per-operation facts -- which endpoint answered, which file was selected and why,
how many bytes actually moved, how long it took, what the header was, and which
verdict followed.

Written by ``--provenance results.json``, and embedded in every JSON report.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gwaspoker import __version__
from gwaspoker.config import GWASPokerConfig


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunEnvironment:
    """The software environment a result was produced in."""

    gwaspoker_version: str = __version__
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    timestamp_utc: str = field(default_factory=_timestamp)
    command: Optional[str] = None
    catalog_data_release: Optional[str] = None
    catalog_api_version: Optional[str] = None
    efo_version: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gwaspoker_version": self.gwaspoker_version,
            "python_version": self.python_version,
            "platform": self.platform,
            "timestamp_utc": self.timestamp_utc,
            "command": self.command,
            "catalog_data_release": self.catalog_data_release,
            "catalog_api_version": self.catalog_api_version,
            "efo_version": self.efo_version,
        }


@dataclass
class ProvenanceRecord:
    """Provenance for one GWASPoker invocation."""

    environment: RunEnvironment = field(default_factory=RunEnvironment)
    configuration: dict[str, Any] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def add_operation(self, name: str, payload: dict[str, Any]) -> None:
        self.operations.append({"operation": name, "recorded_at": _timestamp(), **payload})

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment.to_dict(),
            "configuration": self.configuration,
            "operations": self.operations,
            "failures": self.failures,
        }

    def write(self, path: Path) -> Path:
        """Write the record as pretty-printed JSON."""
        import json

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return path


def build_provenance(
    config: GWASPokerConfig,
    *,
    command: Optional[str] = None,
    catalog_metadata: Optional[dict[str, Any]] = None,
) -> ProvenanceRecord:
    """Start a provenance record for the current invocation.

    ``catalog_metadata`` is the v2 ``/metadata`` document when available; it ties
    the run to a specific GWAS Catalog data release and EFO version.
    """
    environment = RunEnvironment(command=command)
    if catalog_metadata:
        environment.catalog_data_release = catalog_metadata.get("data_release_date")
        environment.catalog_api_version = catalog_metadata.get("version")
        environment.efo_version = catalog_metadata.get("efo_version")

    return ProvenanceRecord(
        environment=environment,
        configuration=config.to_dict(),
    )


def assessment_provenance(result: Any) -> dict[str, Any]:
    """Flatten an :class:`~gwaspoker.catalog.discovery.AssessmentResult`.

    Produces the flat key set the benchmark manifest expects, so a provenance
    file and a manifest row describe the same run in the same vocabulary.
    """
    study = getattr(result, "study", None)
    api = getattr(result, "api_assessment", None)
    resolved = getattr(result, "resolved_file", None)
    probe = getattr(result, "probe", None)
    readiness = getattr(result, "readiness", None)
    header = getattr(probe, "header", None) if probe else None
    ssf = getattr(api, "ssf_metadata", None) if api else None

    input_target = getattr(result, "input_target", None)
    payload: dict[str, Any] = {
        "target": getattr(result, "target", None),
        # Which route located the file. Lets an external-validation experiment
        # separate GWAS Catalog studies from arbitrary public URLs without
        # re-parsing the target string afterwards.
        "input_type": input_target.input_type.value if input_target else None,
        "input_url": getattr(input_target, "url", None),
        "study_accession": getattr(study, "study_accession", None),
        "reported_trait": getattr(study, "reported_trait", None),
        "api_source": getattr(study, "api_source", None),
        "ssf_status": getattr(ssf, "ssf_status", None),
        "api_available": getattr(api, "available", None),
        "api_availability": getattr(api, "availability", None).value if api else None,
        "api_sufficient": getattr(api, "sufficient_for_prs_assessment", None),
        "api_route": getattr(api, "route", None),
        "api_bytes": getattr(api, "bytes_received", None),
        "api_latency_seconds": round(getattr(api, "latency_seconds", 0.0), 4) if api else None,
        "api_endpoints_tried": list(getattr(api, "endpoints_tried", ())) if api else [],
        "remote_file_url": getattr(resolved, "url", None),
        "remote_file_name": getattr(resolved, "name", None),
        "remote_file_size": getattr(resolved, "size_bytes", None),
        "harmonised": getattr(resolved, "is_harmonised", None),
        "file_selection_reason": getattr(resolved, "selection_reason", None),
        "probe_required": getattr(result, "probe_required", None),
        "probe_performed": getattr(result, "probe_performed", None),
        "forced_probe": getattr(result, "forced_probe", None),
        "prs_verdict": readiness.verdict.value if readiness else None,
        "prs_confidence": round(readiness.confidence, 3) if readiness else None,
        "readiness_evidence_source": getattr(readiness, "evidence_source", None),
        "total_bytes_transferred": getattr(result, "bytes_transferred", None),
        "elapsed_seconds": round(getattr(result, "elapsed_seconds", 0.0), 3),
        "error": getattr(result, "error", None),
        "failure_category": (
            result.failure_category.value if getattr(result, "failure_category", None) else None
        ),
    }

    if probe is not None:
        transfer = probe.transfer
        payload.update(
            {
                "probe_limit_bytes": transfer.requested_bytes,
                "probe_bytes_transferred": transfer.received_bytes,
                "probe_range_supported": transfer.range_supported,
                "probe_range_used": transfer.range_used,
                "probe_latency_seconds": round(transfer.transfer_time_seconds, 4),
                "probe_transfer_reduction": transfer.transfer_reduction,
                "file_format": probe.format_label,
                "compression": probe.compression.value,
                "detected_encoding": probe.encoding,
            }
        )
    if header is not None:
        payload.update(
            {
                "detected_delimiter": header.delimiter,
                "detected_header_row_index": header.header_row_index,
                "detected_header": list(header.raw_header),
                "header_confidence": round(header.confidence, 3),
            }
        )
    if probe is not None and probe.mapping is not None:
        payload["canonical_mappings"] = {
            c.raw_name: c.canonical_name for c in probe.mapping.columns
        }
    if study is not None:
        payload.update(study.samples.to_dict())

    return payload
