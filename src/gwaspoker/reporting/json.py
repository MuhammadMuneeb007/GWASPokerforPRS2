"""JSON reports.

Every JSON report carries a ``provenance`` block, so a result file is
self-describing: the reader can tell which GWASPoker version produced it, which
catalogue release it queried, and how many bytes it moved.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from gwaspoker.provenance import ProvenanceRecord


def _default(value: Any) -> Any:
    """Serialise the types that appear inside GWASPoker results."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):  # Enum
        return value.value
    return str(value)


def dumps(payload: Any, *, indent: int = 2) -> str:
    """Serialise a GWASPoker result to JSON text."""
    return json.dumps(payload, indent=indent, default=_default, ensure_ascii=False)


def write_json(
    payload: Any,
    path: Path,
    *,
    provenance: Optional[ProvenanceRecord] = None,
    kind: str = "report",
) -> Path:
    """Write a JSON report with its provenance block attached."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, Any] = {"report_type": kind}
    if provenance is not None:
        document["provenance"] = provenance.to_dict()
    document["results"] = payload

    path.write_text(dumps(document), encoding="utf-8")
    return path


def search_payload(results: Iterable[Any]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in results]


def probe_payload(probe: Any, *, study: Any = None, resolved: Any = None) -> dict[str, Any]:
    return {
        "study": study.to_dict() if study is not None else None,
        "resolved_file": resolved.to_dict() if resolved is not None else None,
        "probe": probe.to_dict(),
    }


def assessment_payload(results: Iterable[Any]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in results]
