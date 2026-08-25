"""What did the user actually give us?

Every command that takes a target -- ``assess``, ``probe``, ``scan``,
``download`` -- accepts the same three things: a GWAS Catalog accession, a
direct URL to a summary-statistics file, or (where meaningful) a local path.
This module is the one place that decides which is which.

Why one place
-------------
The classification was previously repeated in four spots with three different
rules, which had produced real inconsistencies: ``assess`` and ``probe``
accepted ``ftp://`` and then crashed inside :mod:`requests` (which has no FTP
adapter), while ``download`` and ``scan`` rejected the same string outright.

Nothing downstream of resolution changes with the input type::

    accession OR direct URL OR local path
                    |
              bounded probe            <- identical
                    |
          compression detection        <- identical
                    |
             header detection          <- identical
                    |
          canonical column mapping     <- identical
                    |
          value-domain validation      <- identical
                    |
           PRS readiness assessment    <- identical

Only the step that produces a URL differs. A direct URL skips the GWAS Catalog
entirely; it does not skip, shorten or otherwise weaken the analysis.

Recording the type
------------------
:attr:`InputTarget.input_type` is carried into reports and provenance so an
external-validation experiment can separate "GWAS Catalog studies" from
"arbitrary public URLs" without inferring it from the string afterwards.

A direct URL never triggers a full download. The bounded probe applies exactly
as it does for an accession -- ``--probe-bytes`` is the ceiling either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from gwaspoker.failures import FailureCategory, GWASPokerError

logger = logging.getLogger(__name__)

#: Schemes the HTTP layer can actually fetch.
FETCHABLE_SCHEMES = frozenset({"http", "https"})

#: Schemes accepted for rewriting, subject to the host allow-list below.
REWRITABLE_SCHEMES = frozenset({"ftp"})

#: Hosts known to serve the *same paths* over HTTPS as over FTP.
#:
#: ``ftp://host/path`` does not universally imply ``https://host/path`` -- many
#: FTP servers have no HTTP front end at all, and some that do use a different
#: path prefix. Rewriting blindly would silently send the probe somewhere the
#: user did not ask for, and a 404 from the wrong URL is worse than an honest
#: "unsupported scheme".
#:
#: Each entry is a host suffix, verified by hand. Add one only after checking
#: that the HTTPS path really mirrors the FTP path.
FTP_HTTPS_MIRRORS: tuple[str, ...] = (
    "ftp.ebi.ac.uk",  # verified: https://ftp.ebi.ac.uk/pub/... mirrors ftp://
    "ftp.ncbi.nlm.nih.gov",  # verified: https://ftp.ncbi.nlm.nih.gov/... mirrors ftp://
    "ftp.sanger.ac.uk",
    "ftp.1000genomes.ebi.ac.uk",
)


def _has_https_mirror(host: str) -> bool:
    """True when ``host`` is known to serve the same paths over HTTPS."""
    host = host.lower().split(":", 1)[0]
    return any(host == mirror or host.endswith("." + mirror) for mirror in FTP_HTTPS_MIRRORS)


class InputType(str, Enum):
    """What the user's target string turned out to be."""

    GWAS_CATALOG_ACCESSION = "gwas_catalog_accession"
    DIRECT_URL = "direct_url"
    LOCAL_FILE = "local_file"

    @property
    def label(self) -> str:
        return {
            "gwas_catalog_accession": "GWAS Catalog accession",
            "direct_url": "direct URL",
            "local_file": "local file",
        }[self.value]


class InputResolutionError(GWASPokerError):
    """The target is not an accession, a usable URL, or an existing file."""

    category = FailureCategory.INVALID_ACCESSION


@dataclass
class InputTarget:
    """A classified target, ready for the stage that produces bytes."""

    raw: str
    input_type: InputType
    url: Optional[str] = None
    accession: Optional[str] = None
    path: Optional[Path] = None
    #: Set when the URL was rewritten (``ftp://`` to ``https://``), so the
    #: report shows what was actually fetched rather than what was typed.
    normalisation_note: Optional[str] = None
    #: Machine-readable name of the rule applied, or ``None``. Reported
    #: alongside the original and normalised URLs so a supplementary table can
    #: state exactly which URLs were altered and why.
    normalisation_rule: Optional[str] = None

    @property
    def is_accession(self) -> bool:
        return self.input_type is InputType.GWAS_CATALOG_ACCESSION

    @property
    def is_direct_url(self) -> bool:
        return self.input_type is InputType.DIRECT_URL

    @property
    def is_local_file(self) -> bool:
        return self.input_type is InputType.LOCAL_FILE

    @property
    def needs_catalog_lookup(self) -> bool:
        """True when a GWAS Catalog query is required to find the file."""
        return self.is_accession

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.raw,
            "input_type": self.input_type.value,
            "accession": self.accession,
            # `original_url` and `url` differ only when a rule fired; keeping
            # both means a report never shows a URL the user did not supply
            # without also showing what it became.
            "original_url": self.raw if self.is_direct_url else None,
            "url": self.url,
            "path": str(self.path) if self.path else None,
            "normalisation_rule": self.normalisation_rule,
            "normalisation_note": self.normalisation_note,
        }


def is_direct_url(value: str) -> bool:
    """True when ``value`` is a URL with a scheme we can handle.

    >>> is_direct_url("https://example.org/gwas.txt.gz")
    True
    >>> is_direct_url("ftp://ftp.ebi.ac.uk/pub/x.tsv")
    True
    >>> is_direct_url("GCST90012345")
    False
    >>> is_direct_url("C:/data/gwas.tsv")
    False
    """
    parsed = urlparse(str(value).strip())
    scheme = parsed.scheme.lower()
    if scheme not in (FETCHABLE_SCHEMES | REWRITABLE_SCHEMES):
        return False
    return bool(parsed.netloc)


def normalise_url(value: str) -> tuple[str, Optional[str]]:
    """Return a fetchable URL, plus a note when a rewrite rule was applied.

    ``ftp://`` is rewritten to ``https://`` **only for hosts known to mirror
    the same paths** (:data:`FTP_HTTPS_MIRRORS`). For any other host the URL is
    refused rather than silently redirected somewhere the user did not ask for:
    :mod:`requests` has no FTP adapter, and guessing an HTTPS equivalent that
    may not exist would turn "unsupported scheme" into a misleading 404 against
    a URL that was never requested.
    """
    text = str(value).strip()
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()

    if scheme in FETCHABLE_SCHEMES:
        return text, None

    if scheme in REWRITABLE_SCHEMES:
        if not _has_https_mirror(parsed.netloc):
            raise InputResolutionError(
                f"{text!r} uses ftp://, which GWASPoker cannot fetch (the HTTP layer "
                f"has no FTP adapter), and {parsed.netloc!r} is not a host known to "
                "serve the same paths over HTTPS. Supply the https:// URL directly, or "
                "add the host to FTP_HTTPS_MIRRORS once you have verified that its "
                "HTTPS paths mirror its FTP paths."
            )
        rewritten = urlunparse(("https", *tuple(parsed)[1:]))
        note = (
            f"ftp:// was rewritten to https:// under rule 'ftp_https_mirror' because "
            f"{parsed.netloc} is a verified mirror; the HTTP layer has no FTP adapter"
        )
        logger.info("Rewrote %s to %s (ftp_https_mirror)", text, rewritten)
        return rewritten, note

    raise InputResolutionError(f"{text!r} uses an unsupported URL scheme: {scheme!r}")


def resolve_input(
    value: str,
    *,
    allow_local: bool = False,
    require_existing_file: bool = True,
) -> InputTarget:
    """Classify a user-supplied target.

    Order of checks matters. A URL is recognised first because a URL is
    unambiguous; an accession next because it has a strict shape; a local path
    last, and only when ``allow_local`` is set, because almost any string could
    be a path and treating a typo'd accession as a filename would produce a
    confusing "no such file" instead of "not a valid accession".
    """
    from gwaspoker.catalog.rest_api import is_accession

    text = str(value).strip()
    if not text:
        raise InputResolutionError("no target was given")

    if is_direct_url(text):
        url, note = normalise_url(text)
        rule = "ftp_https_mirror" if note else None

        # Known share links serve a landing page unless rewritten. Isolated in
        # url_resolvers.py so host quirks never reach the HTTP layer.
        from gwaspoker.url_resolvers import resolve_public_share_url

        share = resolve_public_share_url(url)
        if share.was_rewritten:
            url = share.url
            rule = share.rule
            note = "; ".join(filter(None, (note, share.note)))

        return InputTarget(
            raw=text,
            input_type=InputType.DIRECT_URL,
            url=url,
            normalisation_note=note or None,
            normalisation_rule=rule,
        )

    if is_accession(text):
        return InputTarget(
            raw=text,
            input_type=InputType.GWAS_CATALOG_ACCESSION,
            accession=text.upper(),
        )

    if allow_local:
        path = Path(text).expanduser()
        if path.exists() or not require_existing_file:
            return InputTarget(raw=text, input_type=InputType.LOCAL_FILE, path=path)
        raise InputResolutionError(
            f"{text!r} is not a GCST accession, an http(s)/ftp URL, or an existing file"
        )

    raise InputResolutionError(
        f"{text!r} is neither a GCST accession nor an http(s)/ftp URL. "
        "For a local file use `gwaspoker scan`."
    )
