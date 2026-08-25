"""Full-file download with progress, resume and checksum verification.

v1 was ``os.system('wget "<url>" -P <dir>')``: no progress reporting to Python,
no checksum verification, no resume, and a second run produced ``file.gz.1``
which a later helper renamed over the original.

Here the transfer is a bounded, observable Python stream:

* the ``-meta.yaml`` sidecar and ``md5sum.txt`` are fetched alongside the data;
* the published MD5 is verified after transfer, and a mismatch is an error;
* an interrupted download resumes from a ``.part`` file via a Range request;
* an existing complete file is never overwritten without ``--overwrite``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from gwaspoker.catalog.models import ResolvedFile
from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import RemoteAccessError
from gwaspoker.http import HttpClient

logger = logging.getLogger(__name__)

#: Suffix for an in-progress transfer. A file only takes its real name once it
#: is complete and verified, so a partial file can never be mistaken for data.
PART_SUFFIX = ".part"


@dataclass
class DownloadResult:
    """Outcome of a complete download."""

    url: str
    path: Optional[Path] = None
    bytes_downloaded: int = 0
    total_bytes: Optional[int] = None
    elapsed_seconds: float = 0.0
    resumed_from: int = 0
    skipped: bool = False
    checksum_expected: Optional[str] = None
    checksum_actual: Optional[str] = None
    checksum_verified: Optional[bool] = None
    sidecar_paths: tuple[Path, ...] = ()
    error: Optional[str] = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.path is not None

    @property
    def throughput_mb_s(self) -> Optional[float]:
        if self.elapsed_seconds <= 0 or not self.bytes_downloaded:
            return None
        return self.bytes_downloaded / self.elapsed_seconds / 1_048_576

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "path": str(self.path) if self.path else None,
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "throughput_mb_s": round(self.throughput_mb_s, 2) if self.throughput_mb_s else None,
            "resumed_from": self.resumed_from,
            "skipped": self.skipped,
            "checksum_expected": self.checksum_expected,
            "checksum_actual": self.checksum_actual,
            "checksum_verified": self.checksum_verified,
            "sidecar_paths": [str(p) for p in self.sidecar_paths],
            "succeeded": self.succeeded,
            "error": self.error,
            "notes": list(self.notes),
        }


class SummaryStatisticsDownloader:
    """Streams a complete file to disk."""

    def __init__(
        self,
        config: Optional[GWASPokerConfig] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.config = config or get_config()
        self.http = http or HttpClient(self.config)

    def close(self) -> None:
        self.http.close()

    def download(
        self,
        url: str,
        output_dir: Path,
        *,
        filename: Optional[str] = None,
        expected_md5: Optional[str] = None,
        overwrite: bool = False,
        resume: Optional[bool] = None,
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> DownloadResult:
        """Download ``url`` into ``output_dir``, keeping its published filename."""
        output_dir = Path(output_dir)
        name = filename or _filename_from_url(url)
        target = output_dir / name
        part = output_dir / (name + PART_SUFFIX)
        allow_resume = self.config.allow_resume if resume is None else resume

        result = DownloadResult(url=url, checksum_expected=expected_md5)

        if target.exists() and not overwrite:
            result.path = target
            result.skipped = True
            result.bytes_downloaded = target.stat().st_size
            result.total_bytes = result.bytes_downloaded
            result.notes = (
                f"{target.name} already exists; not overwritten. Pass --overwrite to replace it.",
            )
            if expected_md5:
                actual = compute_md5(target)
                result.checksum_actual = actual
                result.checksum_verified = actual == expected_md5
                if not result.checksum_verified:
                    result.error = (
                        f"Existing file {target.name} does not match the published MD5 "
                        f"(expected {expected_md5}, found {actual}). Re-download with --overwrite."
                    )
            return result

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.error = f"Could not create {output_dir}: {exc}"
            return result

        start_at = 0
        if allow_resume and part.exists() and not overwrite:
            start_at = part.stat().st_size
            result.resumed_from = start_at
            logger.info("Resuming %s from byte %d", name, start_at)
        elif part.exists():
            part.unlink()

        started = time.perf_counter()
        try:
            chunks, status, remaining = self.http.iter_download(
                url, start=start_at, chunk_size=self.config.download_chunk_bytes
            )
        except RemoteAccessError as exc:
            result.error = str(exc)
            return result

        if start_at and status != 206:
            # Server ignored the resume request: start again from scratch.
            logger.info("Server does not support resume for %s; restarting", name)
            start_at = 0
            result.resumed_from = 0
            result.notes = (*result.notes, "resume unsupported by the server; restarted")

        total = (remaining + start_at) if remaining is not None else None
        result.total_bytes = total

        written = start_at
        mode = "ab" if start_at else "wb"
        try:
            with part.open(mode) as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(written, total)
        except OSError as exc:
            result.error = f"Write failed for {part}: {exc}"
            result.bytes_downloaded = written - start_at
            return result
        except RemoteAccessError as exc:
            result.error = f"Transfer interrupted: {exc}. Re-run to resume from {written} bytes."
            result.bytes_downloaded = written - start_at
            return result

        result.elapsed_seconds = time.perf_counter() - started
        result.bytes_downloaded = written - start_at

        if expected_md5 and self.config.verify_checksum:
            actual = compute_md5(part)
            result.checksum_actual = actual
            result.checksum_verified = actual == expected_md5
            if not result.checksum_verified:
                # The transfer is kept under its .part name so a corrupt file can
                # never be mistaken for verified data, and can still be examined.
                result.error = (
                    f"Checksum mismatch for {name}: expected {expected_md5}, computed {actual}. "
                    f"The transferred bytes have been kept as {part.name} for inspection."
                )
                logger.error("%s", result.error)
                return result

        try:
            if target.exists():
                target.unlink()
            part.replace(target)
        except OSError as exc:
            result.error = f"Could not finalise {target}: {exc}"
            return result

        result.path = target
        return result

    def download_sidecars(
        self,
        resolved: ResolvedFile,
        output_dir: Path,
        *,
        overwrite: bool = False,
    ) -> tuple[Path, ...]:
        """Fetch the ``-meta.yaml`` and ``md5sum.txt`` next to the data file.

        These are a few hundred bytes each and make the download
        self-documenting: the GWAS-SSF metadata records the format, genome
        build, sample size and checksum of the file it accompanies.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for url in (resolved.metadata_url, resolved.checksum_url):
            if not url:
                continue
            name = _filename_from_url(url)
            destination = output_dir / name
            if destination.exists() and not overwrite:
                saved.append(destination)
                continue
            try:
                response = self.http.get(url)
            except RemoteAccessError as exc:
                logger.debug("Could not fetch sidecar %s: %s", url, exc)
                continue
            if not response.ok:
                continue
            try:
                destination.write_bytes(response.content)
            except OSError as exc:
                logger.debug("Could not write sidecar %s: %s", destination, exc)
                continue
            saved.append(destination)
        return tuple(saved)


def compute_md5(path: Path, *, chunk_size: int = 1_048_576) -> str:
    """MD5 of a file, read in chunks so a multi-gigabyte file fits in memory."""
    digest = hashlib.md5()  # noqa: S324 - matches the checksums the Catalog publishes
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filename_from_url(url: str) -> str:
    from urllib.parse import unquote, urlparse

    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return _sanitise_filename(name or "download")


#: Characters that are illegal in NTFS filenames. v1 wrote trait-derived
#: directory names straight through, so a trait containing ':' failed on Windows.
_ILLEGAL = '<>:"|?*'


def _sanitise_filename(name: str) -> str:
    """Make a filename safe on Windows as well as POSIX."""
    cleaned = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.strip(" .") or "download"
    stem = cleaned.split(".")[0].upper()
    if stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned
