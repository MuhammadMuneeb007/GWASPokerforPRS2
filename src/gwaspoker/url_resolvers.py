"""Host-specific rewrites for public file-share links.

Some hosts serve a *landing page* at the URL people copy, and the actual bytes
only at a variant of it. An external run over 768 heterogeneous URLs surfaced
this repeatedly: a Dropbox link ending ``.zip`` returns HTTP 200 with
``text/html``, which GWASPoker used to report as a ZIP decompression error.

Scope is deliberately narrow. Only **Dropbox** is implemented, because that is
the only provider the benchmark showed evidence for. OneDrive, SharePoint and
Google Drive are not implemented: adding rewrite rules for hosts we have no
failing examples of would be speculative, untestable, and would have to be
maintained against APIs that change.

This is isolated from :class:`~gwaspoker.http.HttpClient` on purpose. The HTTP
layer stays a plain, auditable ``requests`` wrapper; host quirks live here.

Every rewrite is reported through :class:`ResolvedURL`, never applied silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

#: Hosts this module knows how to rewrite.
_DROPBOX_HOSTS = frozenset({"www.dropbox.com", "dropbox.com"})


@dataclass
class ResolvedURL:
    """A URL after host-specific rewriting, with the rule that produced it."""

    url: str
    original_url: str
    rule: Optional[str] = None
    note: Optional[str] = None

    @property
    def was_rewritten(self) -> bool:
        return self.url != self.original_url

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "url": self.url,
            "original_url": self.original_url,
            "rule": self.rule,
            "note": self.note,
        }


def _resolve_dropbox(parsed) -> Optional[tuple[str, str, str]]:
    """Turn a Dropbox share link into its direct-download form.

    Dropbox share URLs carry ``?dl=0`` (or no ``dl`` at all), which renders a
    preview page. ``?dl=1`` returns the file itself. Setting the parameter is
    preferred over swapping in the ``dl.dropboxusercontent.com`` host, which is
    an older form Dropbox has deprecated more than once.
    """
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("dl") == "1":
        return None  # already a direct link

    query["dl"] = "1"
    rewritten = urlunparse(parsed._replace(query=urlencode(query)))
    return (
        rewritten,
        "dropbox_direct_download",
        "Dropbox share links serve an HTML preview page unless dl=1 is set; "
        "the parameter was added so the probe receives the file rather than the page",
    )


def resolve_public_share_url(url: str) -> ResolvedURL:
    """Rewrite a known share link to its direct-download form.

    Unknown hosts are returned untouched. This never fetches anything -- it is a
    pure string transformation, so it costs no request and cannot fail.
    """
    text = str(url).strip()
    parsed = urlparse(text)
    host = parsed.netloc.lower().split(":", 1)[0]

    if host in _DROPBOX_HOSTS:
        outcome = _resolve_dropbox(parsed)
        if outcome is not None:
            rewritten, rule, note = outcome
            logger.info("Rewrote %s to %s (%s)", text, rewritten, rule)
            return ResolvedURL(url=rewritten, original_url=text, rule=rule, note=note)

    return ResolvedURL(url=text, original_url=text)
