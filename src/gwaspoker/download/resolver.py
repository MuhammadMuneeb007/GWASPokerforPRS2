"""Resolving a study accession to the right summary-statistics file.

The FTP layout is documented and regular::

    /pub/databases/gwas/summary_statistics/
        GCST90038001-GCST90039000/          <- 1000-accession block
            GCST90038646/
                GCST90038646_buildGRCh37.tsv            <- raw data (1.2 GB)
                GCST90038646_buildGRCh37.tsv-meta.yaml  <- GWAS-SSF metadata
                md5sum.txt                              <- checksums
                harmonised/
                    ...-EFO_0003821.h.tsv.gz            <- harmonised (378 MB)
                    ...-EFO_0003821-Build37.f.tsv.gz    <- formatted (218 MB)
                    md5sum.txt

Selection is by **convention**, not by size. v1 took the largest entry, which:

* picks a bundled ``.pdf`` when one sits beside the data (``GCST006867``);
* cannot distinguish ``.h.tsv.gz`` from ``.f.tsv.gz`` on meaning;
* prefers a 1.2 GB uncompressed TSV over a 378 MB harmonised gzip.

Every selection records a ``selection_reason``.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional
from urllib.parse import unquote, urljoin

from gwaspoker.catalog.models import FileCandidate, ResolvedFile
from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import FileResolutionError, RemoteAccessError
from gwaspoker.http import HttpClient

logger = logging.getLogger(__name__)

#: Accessions are grouped into directories of 1000.
_BLOCK_SIZE = 1000

_ROW_RE = re.compile(
    r'<a href="(?P<href>[^"?][^"]*)">(?P<name>[^<]+)</a>\s*</td>'
    r"\s*<td[^>]*>(?P<modified>[^<]*)</td>"
    r"\s*<td[^>]*>(?P<size>[^<]*)</td>",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r'<a href="(?P<href>[^"?][^"]*)">(?P<name>[^<]+)</a>', re.IGNORECASE)

_SIZE_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

#: Extensions that identify a summary-statistics data file.
_DATA_EXTENSIONS = (
    ".tsv",
    ".txt",
    ".csv",
    ".tab",
    ".ma",
    ".assoc",
    ".meta",
    ".tbl",
    ".linear",
    ".logistic",
    ".sumstats",
    ".gwas",
    ".regenie",
    ".out",
)
_COMPRESSION_EXTENSIONS = (".gz", ".bgz", ".zip", ".bz2", ".xz", ".zst", ".tar")

#: Files that are never the data file.
_NON_DATA_SUFFIXES = (
    "-meta.yaml",
    ".yaml",
    ".yml",
    ".json",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
    ".htm",
    ".md5",
    ".xlsx",
    ".doc",
    ".docx",
    ".r",
    ".py",
    ".log",
)
_NON_DATA_NAMES = ("md5sum.txt", "readme.txt", "readme.md", "readme", "license", "checksums.txt")


def accession_block(accession: str) -> str:
    """FTP block directory containing ``accession``.

    >>> accession_block("GCST90038646")
    'GCST90038001-GCST90039000'
    >>> accession_block("GCST006867")
    'GCST006001-GCST007000'
    """
    digits = re.sub(r"\D", "", accession)
    if not digits:
        raise FileResolutionError(f"{accession!r} contains no numeric part")
    number = int(digits)
    low = ((number - 1) // _BLOCK_SIZE) * _BLOCK_SIZE + 1
    high = low + _BLOCK_SIZE - 1
    width = max(len(digits), 6)
    return f"GCST{low:0{width}d}-GCST{high:0{width}d}"


def parse_size_label(label: str) -> Optional[int]:
    """Parse an Apache index size such as ``1.2G`` or ``650``.

    Returns ``None`` for ``-`` (a directory) or an unparseable value, rather
    than v1's ``0`` -- which made every byte-sized file rank equal-lowest.

    >>> parse_size_label("1.2G")
    1288490188
    >>> parse_size_label("650")
    650
    >>> parse_size_label("-") is None
    True
    """
    text = (label or "").strip()
    if not text or text == "-":
        return None
    unit = text[-1].upper()
    if unit in _SIZE_UNITS:
        try:
            return int(float(text[:-1]) * _SIZE_UNITS[unit])
        except ValueError:
            return None
    return int(text) if text.isdigit() else None


def classify_file(name: str) -> str:
    """Classify a directory entry: data, metadata, checksum, readme, auxiliary."""
    lowered = name.lower().rstrip("/")
    if name.endswith("/"):
        return "directory"
    if lowered in _NON_DATA_NAMES or lowered.startswith("readme"):
        return "checksum" if "md5" in lowered or "checksum" in lowered else "readme"
    if lowered.endswith("-meta.yaml"):
        return "metadata"
    if lowered.endswith(_NON_DATA_SUFFIXES):
        return "auxiliary"

    stem = lowered
    for suffix in _COMPRESSION_EXTENSIONS:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.endswith(_DATA_EXTENSIONS):
        return "data"
    if lowered.endswith(_COMPRESSION_EXTENSIONS):
        # Compressed with no inner extension: probably still the data file.
        return "data"
    return "auxiliary"


def score_candidate(candidate: FileCandidate, *, prefer_harmonised: bool) -> FileCandidate:
    """Score one candidate by naming convention, then extension, then size."""
    name = candidate.name.lower()
    score = 0.0
    reasons: list[str] = []

    if candidate.kind != "data":
        candidate.score = -100.0
        candidate.reasons = (f"not a data file ({candidate.kind})",)
        return candidate

    # -- GWAS Catalog harmonised naming conventions --------------------
    if ".h.tsv" in name:
        score += 6.0
        reasons.append("fully harmonised file (.h.tsv)")
    elif ".f.tsv" in name:
        score += 4.0
        reasons.append("format-harmonised file (.f.tsv)")

    if candidate.is_harmonised:
        if prefer_harmonised:
            score += 5.0
            reasons.append("in harmonised/ and harmonised output was requested")
        else:
            score -= 5.0
            reasons.append("in harmonised/ but the raw file was requested")
    elif prefer_harmonised:
        score -= 1.0
        reasons.append("raw file while harmonised output was requested")
    else:
        score += 2.0
        reasons.append("raw submitted file, as requested")

    # -- Extension quality ---------------------------------------------
    if name.endswith((".tsv.gz", ".txt.gz", ".csv.gz", ".tsv.bgz")):
        score += 3.0
        reasons.append("compressed tabular text")
    elif name.endswith((".tsv", ".txt", ".csv", ".tab")):
        score += 2.0
        reasons.append("plain tabular text")
    elif name.endswith(".gz"):
        score += 1.5
        reasons.append("gzip-compressed")
    elif name.endswith((".zip", ".tar", ".tar.gz")):
        score += 0.5
        reasons.append("archive; needs extraction before use")

    # -- Accession in the filename --------------------------------------
    if re.search(r"gcst\d+", name):
        score += 1.0
        reasons.append("filename carries the study accession")

    # -- Size as a tiebreaker only ---------------------------------------
    if candidate.size_bytes:
        # log10 scaling: a 1 GB file scores ~0.9 over a 1 MB file, never enough
        # to overturn a naming-convention decision.
        import math

        score += min(1.5, math.log10(max(candidate.size_bytes, 1)) / 6.0)
        reasons.append("size used only as a tiebreaker")
    elif candidate.size_bytes == 0:
        score -= 10.0
        reasons.append("zero-length file")

    candidate.score = score
    candidate.reasons = tuple(reasons)
    return candidate


class SummaryStatisticsResolver:
    """Resolves accessions and directory URLs to a specific file."""

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

    def directory_url_for(self, accession: str, *, hint: Optional[str] = None) -> str:
        """Directory URL for a study, from the API hint or the block convention.

        The v2 API's ``full_summary_stats`` field is used when present. v1 of the
        API does not expose it, so the documented directory convention is the
        fallback -- verified by a request before use, never assumed.
        """
        accession = accession.strip().upper()
        if hint:
            url = hint if hint.endswith("/") else hint + "/"
            if url.startswith("ftp://"):
                url = "https://" + url[len("ftp://") :]
            return url.replace("http://ftp.ebi.ac.uk", "https://ftp.ebi.ac.uk")
        return f"{self.config.ftp_base}/{accession_block(accession)}/{accession}/"

    def list_directory(self, url: str) -> list[FileCandidate]:
        """List an FTP-over-HTTP directory index.

        Parses the Apache autoindex table. Sizes come from the index rather than
        a HEAD per file, so listing a directory costs one request.
        """
        if not url.endswith("/"):
            url += "/"
        result = self.http.get(url)
        if result.status_code == 404:
            raise FileResolutionError(f"No summary-statistics directory at {url} (HTTP 404)")
        if not result.ok:
            raise RemoteAccessError(f"HTTP {result.status_code} listing {url}")

        text = result.content.decode("utf-8", errors="replace")
        candidates: list[FileCandidate] = []
        seen: set[str] = set()
        is_harmonised_dir = "/harmonised/" in url

        for match in _ROW_RE.finditer(text):
            href = match.group("href")
            name = html.unescape(match.group("name")).strip()
            if _is_navigation(href, name):
                continue
            size = parse_size_label(match.group("size"))
            candidates.append(
                FileCandidate(
                    name=unquote(name),
                    url=urljoin(url, href),
                    size_bytes=size,
                    size_label=match.group("size").strip() or None,
                    last_modified=match.group("modified").strip() or None,
                    is_harmonised=is_harmonised_dir,
                    is_directory=name.endswith("/"),
                    kind=classify_file(name),
                )
            )
            seen.add(href)

        if not candidates:
            # Some mirrors serve a <pre> listing with no size column.
            for match in _LINK_RE.finditer(text):
                href = match.group("href")
                name = html.unescape(match.group("name")).strip()
                if _is_navigation(href, name) or href in seen:
                    continue
                candidates.append(
                    FileCandidate(
                        name=unquote(name),
                        url=urljoin(url, href),
                        is_harmonised=is_harmonised_dir,
                        is_directory=name.endswith("/"),
                        kind=classify_file(name),
                    )
                )

        logger.debug("Listed %d entries at %s", len(candidates), url)
        return candidates

    def resolve(
        self,
        accession: Optional[str] = None,
        *,
        directory_url: Optional[str] = None,
        harmonised: str = "auto",
        location_hint: Optional[str] = None,
    ) -> ResolvedFile:
        """Choose the summary-statistics file for a study.

        ``harmonised`` is ``auto`` (prefer harmonised, accept raw), ``yes``
        (harmonised only) or ``no`` (raw only).
        """
        if directory_url is None:
            if accession is None:
                raise FileResolutionError("Either an accession or a directory URL is required")
            directory_url = self.directory_url_for(accession, hint=location_hint)

        top_level = self.list_directory(directory_url)
        prefer_harmonised = harmonised in ("auto", "yes")

        all_candidates = list(top_level)
        harmonised_dir_url: Optional[str] = None
        for candidate in top_level:
            if candidate.is_directory and candidate.name.rstrip("/").lower() == "harmonised":
                harmonised_dir_url = candidate.url
                break

        if harmonised_dir_url and prefer_harmonised:
            try:
                all_candidates.extend(self.list_directory(harmonised_dir_url))
            except (FileResolutionError, RemoteAccessError) as exc:
                logger.debug("harmonised/ listed but unreadable: %s", exc)

        data_candidates = [c for c in all_candidates if c.kind == "data" and not c.is_directory]
        if harmonised == "yes":
            data_candidates = [c for c in data_candidates if c.is_harmonised]
            if not data_candidates:
                raise FileResolutionError(
                    f"--harmonised yes was requested but {directory_url} publishes no "
                    "harmonised summary statistics"
                )
        elif harmonised == "no":
            data_candidates = [c for c in data_candidates if not c.is_harmonised]
            if not data_candidates:
                raise FileResolutionError(
                    f"--harmonised no was requested but {directory_url} publishes only "
                    "harmonised summary statistics"
                )

        if not data_candidates:
            raise FileResolutionError(
                f"No summary-statistics data file found at {directory_url}. "
                f"Entries present: {', '.join(c.name for c in all_candidates[:10]) or 'none'}"
            )

        scored = [score_candidate(c, prefer_harmonised=prefer_harmonised) for c in data_candidates]
        scored.sort(key=lambda c: c.score, reverse=True)
        best = scored[0]

        reason = "; ".join(best.reasons) if best.reasons else "only data file present"
        if len(scored) > 1:
            reason += f" (scored {best.score:.2f} against {len(scored) - 1} other candidate(s))"

        return ResolvedFile(
            url=best.url,
            name=best.name,
            size_bytes=best.size_bytes,
            is_harmonised=best.is_harmonised,
            selection_reason=reason,
            candidates=tuple(scored),
            directory_url=directory_url,
            metadata_url=_find_sidecar(all_candidates, best.name, "-meta.yaml"),
            checksum_url=_find_checksum(all_candidates, best.is_harmonised),
        )

    def fetch_expected_md5(self, checksum_url: str, filename: str) -> Optional[str]:
        """Look ``filename`` up in an ``md5sum.txt``."""
        try:
            result = self.http.get(checksum_url)
        except RemoteAccessError as exc:
            logger.debug("Could not read %s: %s", checksum_url, exc)
            return None
        if not result.ok:
            return None
        for line in result.content.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].strip().lstrip("*") == filename:
                return parts[0].strip()
        return None


def _is_navigation(href: str, name: str) -> bool:
    """True for sort links, the parent link, and other index furniture."""
    if href.startswith(("?", "/", "#")) or href in ("../",):
        return True
    lowered = name.strip().lower()
    return lowered in ("parent directory", "name", "last modified", "size", "description", "")


def _find_sidecar(candidates: list[FileCandidate], data_name: str, suffix: str) -> Optional[str]:
    """Find ``<data_name><suffix>`` among the listed entries."""
    target = data_name + suffix
    for candidate in candidates:
        if candidate.name == target:
            return candidate.url
    return None


def _find_checksum(candidates: list[FileCandidate], harmonised: bool) -> Optional[str]:
    """Find the ``md5sum.txt`` in the same directory as the chosen file."""
    for candidate in candidates:
        if candidate.name.lower() == "md5sum.txt" and candidate.is_harmonised == harmonised:
            return candidate.url
    for candidate in candidates:
        if candidate.name.lower() == "md5sum.txt":
            return candidate.url
    return None
