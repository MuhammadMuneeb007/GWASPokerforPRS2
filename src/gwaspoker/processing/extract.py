"""Extraction and normalization of a downloaded summary-statistics file.

Replaces v1's Module 4. The decompression and archive handling that v1 achieved
by shelling out to ``7z``, ``tar``, ``gunzip``, ``zcat`` and ``cat`` is done
here with :mod:`gzip`, :mod:`zipfile`, :mod:`tarfile`, :mod:`bz2` and
:mod:`lzma`, so it runs on Windows.

Archive extraction filters member paths against traversal (``../``) and absolute
paths, which v1's ``tar -xvf`` and ``7z x`` did not.
"""

from __future__ import annotations

import logging
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gwaspoker.failures import CompressionError, FailureCategory, GWASPokerError
from gwaspoker.mapping.mapper import MappingResult, get_mapper
from gwaspoker.probe.compression import Compression, detect_compression
from gwaspoker.probe.remote import ProbeResult, RemoteProber
from gwaspoker.processing.formats import human_size, is_spreadsheet
from gwaspoker.processing.normalize import (
    NormalizationReport,
    Transformation,
    note_declined_unsafe,
    rename_to_canonical,
    strip_surrounding_quotes,
    strip_whitespace,
)

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Outcome of extracting and normalizing one file."""

    source: Path
    output_path: Optional[Path] = None
    rows_written: Optional[int] = None
    columns: tuple[str, ...] = ()
    compression: Compression = Compression.UNKNOWN
    delimiter: Optional[str] = None
    encoding: Optional[str] = None
    header_row_index: Optional[int] = None
    mapping: Optional[MappingResult] = None
    report: NormalizationReport = field(default_factory=NormalizationReport)
    extracted_member: Optional[str] = None
    error: Optional[str] = None
    failure_category: Optional[FailureCategory] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.output_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "output_path": str(self.output_path) if self.output_path else None,
            "rows_written": self.rows_written,
            "columns": list(self.columns),
            "compression": self.compression.value,
            "delimiter": self.delimiter,
            "encoding": self.encoding,
            "header_row_index": self.header_row_index,
            "extracted_member": self.extracted_member,
            "mapping": self.mapping.to_dict() if self.mapping else None,
            "normalization": self.report.to_dict(),
            "succeeded": self.succeeded,
            "error": self.error,
            "failure_category": self.failure_category.value if self.failure_category else None,
        }


class Extractor:
    """Decompresses, parses and normalizes a local summary-statistics file."""

    def __init__(self, prober: Optional[RemoteProber] = None) -> None:
        self.prober = prober or RemoteProber()

    def extract(
        self,
        path: Path,
        *,
        output_path: Optional[Path] = None,
        output_delimiter: str = "\t",
        max_rows: Optional[int] = None,
        rename_columns: bool = False,
        rename_to_symbols: bool = False,
        strip_quotes: bool = True,
        strip_space: bool = True,
        overwrite: bool = False,
    ) -> ExtractionResult:
        """Extract ``path`` into a clean tabular file."""
        path = Path(path)
        result = ExtractionResult(source=path)
        note_declined_unsafe(result.report)

        if not path.is_file():
            result.error = f"No such file: {path}"
            result.failure_category = FailureCategory.FILE_NOT_FOUND
            return result

        # 1. Structure: probe the head to learn compression, delimiter, header.
        probe = self.prober.probe_local(path)
        if not probe.succeeded:
            if is_spreadsheet(path.name):
                return self._extract_spreadsheet(
                    path, result, output_path, output_delimiter, overwrite, max_rows
                )
            result.error = probe.error or "The file structure could not be determined"
            result.failure_category = probe.failure_category
            result.compression = probe.compression
            return result

        self._apply_probe(result, probe)

        # 2. Archives need a member extracted before pandas can read them.
        read_path = path
        member: Optional[str] = None
        if result.compression in (Compression.ZIP, Compression.TAR, Compression.TAR_GZIP):
            try:
                read_path, member = self._extract_archive_member(path)
            except (CompressionError, GWASPokerError) as exc:
                result.error = str(exc)
                result.failure_category = getattr(
                    exc, "category", FailureCategory.DECOMPRESSION_ERROR
                )
                return result
            result.extracted_member = member
            result.report.record(
                Transformation(
                    name="extract_archive_member",
                    description=f"Extracted {member!r} from the archive to {read_path.name}.",
                    reversible=False,
                )
            )

        # 3. Read the table.
        try:
            frame = self._read_table(read_path, result, max_rows=max_rows)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            result.error = f"Could not parse {read_path.name}: {exc}"
            result.failure_category = FailureCategory.UNSUPPORTED_FORMAT
            return result

        # 4. Declared normalization only.
        if strip_quotes:
            frame = strip_surrounding_quotes(frame, result.report)
        if strip_space:
            frame = strip_whitespace(frame, result.report)

        result.mapping = get_mapper().map_header([str(c) for c in frame.columns])
        if rename_columns or rename_to_symbols:
            frame = rename_to_canonical(
                frame, result.mapping, result.report, symbols=rename_to_symbols
            )

        # 5. Write.
        destination = Path(output_path) if output_path else _default_output(path)
        if destination.exists() and not overwrite:
            result.error = (
                f"{destination} already exists. Pass --overwrite to replace it, or "
                "choose another --output."
            )
            result.failure_category = FailureCategory.DISK_ERROR
            return result
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(destination, sep=output_delimiter, index=False, encoding="utf-8")
        except OSError as exc:
            result.error = f"Could not write {destination}: {exc}"
            result.failure_category = FailureCategory.DISK_ERROR
            return result

        result.output_path = destination
        result.rows_written = int(len(frame))
        result.columns = tuple(str(c) for c in frame.columns)
        logger.info(
            "Wrote %d rows and %d columns to %s (%s)",
            result.rows_written,
            len(result.columns),
            destination,
            human_size(destination.stat().st_size),
        )
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _apply_probe(result: ExtractionResult, probe: ProbeResult) -> None:
        result.compression = probe.compression
        result.encoding = probe.encoding
        if probe.header is not None:
            result.delimiter = probe.header.delimiter
            result.header_row_index = probe.header.header_row_index

    def _read_table(self, path: Path, result: ExtractionResult, *, max_rows: Optional[int]):
        """Read the table with the structure the probe established.

        Passing an explicit delimiter, encoding and header row is the whole
        point: v1 relied on pandas' inference plus ``comment='#'`` and then
        retried with four different shell pipelines when that failed.
        """
        import pandas as pd

        compression = {
            Compression.GZIP: "gzip",
            Compression.BGZF: "gzip",
            Compression.BZIP2: "bz2",
            Compression.XZ: "xz",
            Compression.ZSTD: "zstd",
        }.get(result.compression)

        return pd.read_csv(
            path,
            sep=result.delimiter or "\t",
            encoding=result.encoding or "utf-8",
            skiprows=result.header_row_index or 0,
            compression=compression if compression else "infer",
            nrows=max_rows,
            dtype=str,  # preserve values exactly as published
            keep_default_na=False,
            na_filter=False,
            engine="python" if (result.delimiter or "") == " " else "c",
        )

    def _extract_spreadsheet(
        self,
        path: Path,
        result: ExtractionResult,
        output_path: Optional[Path],
        output_delimiter: str,
        overwrite: bool,
        max_rows: Optional[int],
    ) -> ExtractionResult:
        """Convert an ``.xlsx`` file, which needs the optional ``[excel]`` extra."""
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is required
            result.error = str(exc)
            result.failure_category = FailureCategory.DEPENDENCY_MISSING
            return result
        try:
            frame = pd.read_excel(path, nrows=max_rows, dtype=str)
        except ImportError:
            result.error = 'Reading .xlsx needs the optional extra: pip install "gwaspoker[excel]"'
            result.failure_category = FailureCategory.DEPENDENCY_MISSING
            return result
        except (ValueError, OSError) as exc:
            result.error = f"Could not read the spreadsheet {path.name}: {exc}"
            result.failure_category = FailureCategory.UNSUPPORTED_FORMAT
            return result

        result.compression = Compression.NONE
        result.mapping = get_mapper().map_header([str(c) for c in frame.columns])
        destination = Path(output_path) if output_path else _default_output(path)
        if destination.exists() and not overwrite:
            result.error = f"{destination} already exists. Pass --overwrite to replace it."
            result.failure_category = FailureCategory.DISK_ERROR
            return result
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(destination, sep=output_delimiter, index=False, encoding="utf-8")
        result.output_path = destination
        result.rows_written = int(len(frame))
        result.columns = tuple(str(c) for c in frame.columns)
        return result

    @staticmethod
    def _extract_archive_member(path: Path) -> tuple[Path, str]:
        """Extract the data member of a zip or tar into a sibling directory.

        Members whose paths escape the destination are refused. v1 passed
        archives straight to ``7z x`` and ``tar -xvf``, which happily honour
        ``../`` and absolute paths.
        """
        from gwaspoker.probe.compression import _pick_archive_member

        destination = path.parent / f"{path.stem}_extracted"
        destination.mkdir(parents=True, exist_ok=True)
        compression = detect_compression(path.read_bytes()[:1024], path.name)

        if compression is Compression.ZIP:
            with zipfile.ZipFile(path) as archive:
                member = _pick_archive_member(archive.namelist())
                if member is None:
                    raise CompressionError(
                        f"No data-like member found in {path.name}",
                        category=FailureCategory.UNSUPPORTED_FORMAT,
                    )
                _reject_unsafe_member(member)
                archive.extract(member, destination)
                return destination / member, member

        mode = "r:gz" if compression is Compression.TAR_GZIP else "r:"
        with tarfile.open(path, mode) as archive:
            names = [m.name for m in archive.getmembers() if m.isfile()]
            member = _pick_archive_member(names)
            if member is None:
                raise CompressionError(
                    f"No data-like member found in {path.name}",
                    category=FailureCategory.UNSUPPORTED_FORMAT,
                )
            _reject_unsafe_member(member)
            info = archive.getmember(member)
            if not info.isfile():
                raise CompressionError(f"{member!r} is not a regular file")
            archive.extract(info, destination)
            return destination / member, member


def _reject_unsafe_member(name: str) -> None:
    """Refuse archive members that would write outside the destination."""
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts or name.startswith(("/", "\\")):
        raise CompressionError(
            f"Refusing to extract {name!r}: the member path escapes the destination " "directory.",
            category=FailureCategory.UNSUPPORTED_FORMAT,
        )


def _default_output(path: Path) -> Path:
    """Default output name: ``<stem>.gwaspoker.tsv`` beside the input.

    The original filename is preserved. v1 wrote every file to a fixed
    ``gwas.csv``, then to ``gwas.csv.modified``, overwriting any previous run.
    """
    from gwaspoker.processing.formats import strip_compression_suffix

    stem = Path(strip_compression_suffix(path.name)).stem
    return path.parent / f"{stem}.gwaspoker.tsv"
