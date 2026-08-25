"""Compression detection and incremental decompression of partial streams.

The central requirement: recover the beginning of a compressed file's *content*
from the beginning of its *bytes*, without holding, writing or transferring the
whole file.

``zlib.decompressobj(wbits=31)`` does exactly this for gzip -- it decodes as much
as the input allows and stops, without complaining that the stream is
incomplete. A 64 KB gzip prefix typically yields a few hundred KB of text, which
is far more than a header needs.

Zip and tar are handled by reading their leading directory structures out of the
buffer. Formats that fundamentally cannot be decoded from a prefix (bzip2 needs
a complete block; xz needs its index) are detected and reported honestly rather
than guessed at.

v1 shelled out to ``gunzip -c | head``, ``zcat``, ``7z`` and ``tar``, and had a
four-deep nested fallback chain of ``subprocess.run`` calls ending in
``exit(0)``. None of it runs on Windows.
"""

from __future__ import annotations

import io
import logging
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gwaspoker.failures import CompressionError, FailureCategory

logger = logging.getLogger(__name__)

#: Bytes of decompressed output that is always more than enough for a header
#: plus several data rows.
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024


class Compression(str, Enum):
    """Detected container/compression type."""

    NONE = "none"
    GZIP = "gzip"
    BGZF = "bgzf"
    ZIP = "zip"
    TAR = "tar"
    TAR_GZIP = "tar.gz"
    BZIP2 = "bzip2"
    XZ = "xz"
    ZSTD = "zstd"
    UNKNOWN = "unknown"


@dataclass
class DecompressionResult:
    """Decoded bytes from a compressed prefix, and how they were obtained."""

    compression: Compression
    data: bytes
    complete: bool
    member_name: Optional[str] = None
    note: Optional[str] = None
    compressed_bytes_consumed: int = 0

    @property
    def expansion_ratio(self) -> Optional[float]:
        if not self.compressed_bytes_consumed:
            return None
        return len(self.data) / self.compressed_bytes_consumed

    def to_dict(self) -> dict[str, object]:
        return {
            "compression": self.compression.value,
            "decompressed_bytes": len(self.data),
            "compressed_bytes_consumed": self.compressed_bytes_consumed,
            "expansion_ratio": round(self.expansion_ratio, 2) if self.expansion_ratio else None,
            "complete": self.complete,
            "member_name": self.member_name,
            "note": self.note,
        }


#: Magic-number signatures, longest first.
_MAGIC: tuple[tuple[bytes, Compression], ...] = (
    (b"PK\x03\x04", Compression.ZIP),
    (b"PK\x05\x06", Compression.ZIP),
    (b"\xfd7zXZ\x00", Compression.XZ),
    (b"BZh", Compression.BZIP2),
    (b"\x28\xb5\x2f\xfd", Compression.ZSTD),
    (b"\x1f\x8b", Compression.GZIP),
)

#: Extension hints, longest first so ".tar.gz" beats ".gz".
_EXTENSIONS: tuple[tuple[str, Compression], ...] = (
    (".tar.gz", Compression.TAR_GZIP),
    (".tar.bz2", Compression.BZIP2),
    (".tar.xz", Compression.XZ),
    (".tgz", Compression.TAR_GZIP),
    (".bgz", Compression.BGZF),
    (".gz", Compression.GZIP),
    (".zip", Compression.ZIP),
    (".tar", Compression.TAR),
    (".bz2", Compression.BZIP2),
    (".xz", Compression.XZ),
    (".zst", Compression.ZSTD),
)


def detect_compression(data: bytes, filename: str = "") -> Compression:
    """Identify the container from magic bytes, with the filename as a tiebreaker.

    Magic bytes win over the extension: files served as ``.tsv`` that are in fact
    gzipped are common in the Catalog's older submissions.
    """
    lowered = filename.lower().rstrip("/")

    for magic, compression in _MAGIC:
        if data.startswith(magic):
            if compression is Compression.GZIP:
                if _is_bgzf(data):
                    return Compression.BGZF
                # A gzip whose payload is a tar is a .tar.gz.
                if lowered.endswith((".tar.gz", ".tgz")) or _gzip_payload_is_tar(data):
                    return Compression.TAR_GZIP
            return compression

    if _looks_like_tar(data):
        return Compression.TAR

    for suffix, compression in _EXTENSIONS:
        if lowered.endswith(suffix):
            logger.debug("No magic bytes matched; using extension hint %s", suffix)
            return compression

    return Compression.NONE


def _is_bgzf(data: bytes) -> bool:
    """BGZF is gzip with an ``BC`` extra subfield -- used by tabix-indexed files."""
    if len(data) < 18 or not data.startswith(b"\x1f\x8b"):
        return False
    flg = data[3]
    if not flg & 0x04:  # FEXTRA
        return False
    xlen = int.from_bytes(data[10:12], "little")
    extra = data[12 : 12 + xlen]
    return b"BC" in extra


def _looks_like_tar(data: bytes) -> bool:
    """POSIX tar carries ``ustar`` at offset 257 of its first header block."""
    return len(data) >= 265 and data[257:262] in (b"ustar",)


def _gzip_payload_is_tar(data: bytes) -> bool:
    """Peek inside a gzip stream to see whether the payload is a tar archive."""
    try:
        head = zlib.decompressobj(31).decompress(data, 512)
    except zlib.error:
        return False
    return _looks_like_tar(head)


def decompress_prefix(
    data: bytes,
    compression: Compression,
    *,
    max_output: int = MAX_DECOMPRESSED_BYTES,
) -> DecompressionResult:
    """Decode the start of a compressed buffer.

    ``data`` is expected to be a *prefix*, so a truncated stream is normal and
    not an error. Raises :class:`CompressionError` only when the format cannot
    be decoded from a prefix at all.
    """
    if compression in (Compression.NONE, Compression.UNKNOWN):
        return DecompressionResult(
            compression=Compression.NONE,
            data=data,
            complete=True,
            compressed_bytes_consumed=len(data),
        )

    if compression in (Compression.GZIP, Compression.BGZF):
        return _decompress_gzip(data, max_output, compression)

    if compression is Compression.TAR_GZIP:
        inner = _decompress_gzip(data, max_output, Compression.GZIP)
        result = _read_tar_member(inner.data, max_output)
        result.compression = Compression.TAR_GZIP
        result.compressed_bytes_consumed = len(data)
        return result

    if compression is Compression.TAR:
        result = _read_tar_member(data, max_output)
        result.compressed_bytes_consumed = len(data)
        return result

    if compression is Compression.ZIP:
        return _read_zip_member(data, max_output)

    if compression is Compression.BZIP2:
        return _decompress_bzip2(data, max_output)

    if compression is Compression.XZ:
        return _decompress_xz(data, max_output)

    if compression is Compression.ZSTD:
        return _decompress_zstd(data, max_output)

    raise CompressionError(
        f"Unsupported compression: {compression.value}",
        category=FailureCategory.UNSUPPORTED_COMPRESSION,
    )


def _decompress_gzip(data: bytes, max_output: int, kind: Compression) -> DecompressionResult:
    """Incrementally inflate a gzip prefix.

    ``wbits=31`` selects gzip framing. BGZF is a sequence of complete gzip
    members, so each is inflated in turn until the buffer runs out.
    """
    output = bytearray()
    consumed = 0
    remaining = data
    members = 0

    while remaining and len(output) < max_output:
        decompressor = zlib.decompressobj(31)
        try:
            chunk = decompressor.decompress(remaining, max_output - len(output))
        except zlib.error as exc:
            if output:
                # The first member(s) decoded; a later one is cut short. Fine.
                break
            raise CompressionError(f"gzip stream could not be decoded: {exc}") from exc
        output.extend(chunk)
        members += 1
        consumed = len(data) - len(decompressor.unused_data)
        if not decompressor.eof:
            # Ran out of input mid-member: expected for a bounded probe.
            return DecompressionResult(
                compression=kind,
                data=bytes(output),
                complete=False,
                note="gzip stream truncated at the probe boundary, as intended",
                compressed_bytes_consumed=len(data),
            )
        remaining = decompressor.unused_data
        if not remaining:
            return DecompressionResult(
                compression=kind,
                data=bytes(output),
                complete=True,
                note=f"{members} complete gzip member(s)" if members > 1 else None,
                compressed_bytes_consumed=consumed,
            )

    return DecompressionResult(
        compression=kind,
        data=bytes(output),
        complete=False,
        note="output limit reached" if len(output) >= max_output else None,
        compressed_bytes_consumed=consumed or len(data),
    )


def _read_zip_member(data: bytes, max_output: int) -> DecompressionResult:
    """Read the first plausible data member from a zip prefix.

    Zip stores its central directory at the *end* of the archive, which a prefix
    does not contain. :class:`zipfile.ZipFile` therefore fails, and we fall back
    to inflating the first local file header directly.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = _pick_archive_member([i.filename for i in archive.infolist()])
            if member is None:
                raise CompressionError(
                    "No data-like member found in the zip archive",
                    category=FailureCategory.UNSUPPORTED_FORMAT,
                )
            with archive.open(member) as handle:
                payload = handle.read(max_output)
            return DecompressionResult(
                compression=Compression.ZIP,
                data=payload,
                complete=True,
                member_name=member,
                compressed_bytes_consumed=len(data),
            )
    except (zipfile.BadZipFile, EOFError, KeyError):
        logger.debug("Zip central directory absent from prefix; reading the local header")

    return _read_zip_local_header(data, max_output)


def _read_zip_local_header(data: bytes, max_output: int) -> DecompressionResult:
    """Inflate the first member using only its local file header."""
    if len(data) < 30 or not data.startswith(b"PK\x03\x04"):
        raise CompressionError(
            "Zip prefix is too short to contain a local file header",
            category=FailureCategory.DECOMPRESSION_ERROR,
        )
    method = int.from_bytes(data[8:10], "little")
    name_len = int.from_bytes(data[26:28], "little")
    extra_len = int.from_bytes(data[28:30], "little")
    name_end = 30 + name_len
    name = data[30:name_end].decode("utf-8", errors="replace")
    body = data[name_end + extra_len :]

    if method == 0:  # stored
        return DecompressionResult(
            compression=Compression.ZIP,
            data=body[:max_output],
            complete=False,
            member_name=name,
            note="stored (uncompressed) zip member read from the local header",
            compressed_bytes_consumed=len(data),
        )
    if method != 8:  # deflate
        raise CompressionError(
            f"Zip compression method {method} is not supported from a prefix",
            category=FailureCategory.UNSUPPORTED_COMPRESSION,
        )

    try:
        payload = zlib.decompressobj(-15).decompress(body, max_output)
    except zlib.error as exc:
        raise CompressionError(f"Zip member could not be inflated: {exc}") from exc
    return DecompressionResult(
        compression=Compression.ZIP,
        data=payload,
        complete=False,
        member_name=name,
        note="inflated from the local file header; central directory not in prefix",
        compressed_bytes_consumed=len(data),
    )


def _read_tar_member(data: bytes, max_output: int) -> DecompressionResult:
    """Read the first data-like member from a tar prefix."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            names = [m.name for m in archive.getmembers() if m.isfile()]
            member = _pick_archive_member(names)
            if member is None:
                raise CompressionError(
                    "No data-like member found in the tar archive",
                    category=FailureCategory.UNSUPPORTED_FORMAT,
                )
            handle = archive.extractfile(member)
            payload = handle.read(max_output) if handle else b""
            return DecompressionResult(
                compression=Compression.TAR,
                data=payload,
                complete=True,
                member_name=member,
            )
    except tarfile.TarError:
        logger.debug("Tar index incomplete in prefix; reading the first header block")

    # Fall back to the first 512-byte header block, which a prefix does contain.
    if len(data) < 512:
        raise CompressionError(
            "Tar prefix is shorter than one header block",
            category=FailureCategory.DECOMPRESSION_ERROR,
        )
    name = data[:100].rstrip(b"\x00").decode("utf-8", errors="replace")
    payload = data[512 : 512 + max_output]
    return DecompressionResult(
        compression=Compression.TAR,
        data=payload,
        complete=False,
        member_name=name or None,
        note="read from the first tar header block; archive index not in prefix",
    )


def _decompress_bzip2(data: bytes, max_output: int) -> DecompressionResult:
    import bz2

    try:
        payload = bz2.BZ2Decompressor().decompress(data, max_output)
    except (OSError, ValueError, EOFError) as exc:
        raise CompressionError(
            "bzip2 needs a complete 900 KB block before any output can be produced; "
            f"the probe prefix was insufficient ({exc}). Increase --probe-bytes or "
            "download the file.",
            category=FailureCategory.DECOMPRESSION_ERROR,
        ) from exc
    return DecompressionResult(
        compression=Compression.BZIP2,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


def _decompress_xz(data: bytes, max_output: int) -> DecompressionResult:
    import lzma

    try:
        payload = lzma.LZMADecompressor().decompress(data, max_output)
    except lzma.LZMAError as exc:
        raise CompressionError(
            f"xz stream could not be decoded from a prefix: {exc}",
            category=FailureCategory.DECOMPRESSION_ERROR,
        ) from exc
    return DecompressionResult(
        compression=Compression.XZ,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


def _decompress_zstd(data: bytes, max_output: int) -> DecompressionResult:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CompressionError(
                "Zstandard support needs Python 3.14+ or the 'zstandard' package",
                category=FailureCategory.UNSUPPORTED_COMPRESSION,
            ) from exc
        decompressor = zstandard.ZstdDecompressor().decompressobj()
        try:
            payload = decompressor.decompress(data)[:max_output]
        except zstandard.ZstdError as exc:
            raise CompressionError(f"zstd stream could not be decoded: {exc}") from exc
    else:
        try:
            payload = zstd.ZstdDecompressor().decompress(data)[:max_output]
        except Exception as exc:  # the stdlib module raises its own error type
            raise CompressionError(f"zstd stream could not be decoded: {exc}") from exc
    return DecompressionResult(
        compression=Compression.ZSTD,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


#: Extensions that plausibly hold summary statistics, in preference order.
_DATA_EXTENSIONS: tuple[str, ...] = (
    ".tsv",
    ".txt",
    ".csv",
    ".ma",
    ".assoc",
    ".meta",
    ".tbl",
    ".linear",
    ".logistic",
    ".sumstats",
    ".gwas",
    ".out",
    ".regenie",
)

#: Members that are never the data file.
_ARCHIVE_NOISE: tuple[str, ...] = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".xlsx",
    ".doc",
    ".docx",
    ".md5",
    "readme",
    "license",
    "__macosx",
    ".ds_store",
)


def _pick_archive_member(names: list[str]) -> Optional[str]:
    """Choose the member of an archive most likely to be the summary statistics.

    Preference order: a known data extension, then anything not obviously noise.
    Directory entries and macOS resource forks are excluded. v1 instead took the
    largest member, which selects a bundled PDF when one is present.
    """
    candidates = [
        n
        for n in names
        if not n.endswith("/") and not any(noise in n.lower() for noise in _ARCHIVE_NOISE)
    ]
    if not candidates:
        return None
    for extension in _DATA_EXTENSIONS:
        for name in candidates:
            lowered = name.lower()
            if lowered.endswith(extension) or lowered.endswith(extension + ".gz"):
                return name
    return candidates[0]
