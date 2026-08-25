"""Adapter for the GWAS Catalog study/trait metadata APIs.

Three upstream routes are wrapped behind one interface:

``v2``
    ``/gwas/rest/api/v2`` -- the current REST API. It alone exposes
    ``full_summary_stats`` (the FTP location) and ``full_summary_stats_available``.
``v1``
    ``/gwas/rest/api`` -- the HAL/HATEOAS predecessor. Still healthy, and the
    fallback when v2 errors.
``solr``
    ``/gwas/api/search`` -- the free-text index behind the website. Used to turn
    a phenotype string into EFO terms, which is what the structured endpoints
    need.

At audit time (2026-08-24) ``/v2/studies`` returned HTTP 500 on every request
while v1 was fully healthy, so the fallback is not hypothetical. Every
:class:`~gwaspoker.catalog.models.Study` records which route produced it in
``api_source``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from gwaspoker.catalog.models import (
    UNKNOWN,
    Ancestry,
    EfoTrait,
    Provenance,
    Study,
    ValueSource,
)
from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import (
    FAILURES,
    AccessionNotFoundError,
    CatalogApiError,
    FailureCategory,
    GWASPokerError,
    http_status_category,
)
from gwaspoker.http import HttpClient

logger = logging.getLogger(__name__)

ACCESSION_RE = re.compile(r"^GCST\d{4,}$", re.IGNORECASE)

#: When a filter can only be applied client-side (the v1 API cannot filter on
#: summary-statistics availability), fetch this many times the requested count
#: before filtering, up to MAX_FETCH.
OVERSAMPLE_FACTOR = 8
MAX_FETCH = 400


def is_accession(value: str) -> bool:
    """True if ``value`` looks like a GWAS Catalog study accession."""
    return bool(ACCESSION_RE.match(value.strip()))


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def _year_from_date(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = re.match(r"(\d{4})", str(value))
    return int(match.group(1)) if match else None


def _clean(value: Any) -> Optional[str]:
    """Normalize a text field, treating the Catalog's ``NA`` markers as absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NR", "-"}:
        return None
    return text


class GwasCatalogClient:
    """Normalizing client for the GWAS Catalog metadata APIs."""

    def __init__(
        self,
        config: Optional[GWASPokerConfig] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.config = config or get_config()
        self.http = http or HttpClient(self.config)
        self._v2_healthy: Optional[bool] = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> GwasCatalogClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Study retrieval
    # ------------------------------------------------------------------

    def get_study(self, accession: str) -> Study:
        """Fetch one study, preferring v2 and falling back to v1.

        Raises :class:`AccessionNotFoundError` only on a definite 404 from a
        healthy endpoint -- never because one route happened to error.
        """
        accession = accession.strip().upper()
        if not is_accession(accession):
            raise AccessionNotFoundError(f"{accession!r} is not a GCST accession")

        order = self._route_order()
        last_error: Optional[GWASPokerError] = None
        definite_404 = False

        for route in order:
            try:
                study = (
                    self._get_study_v2(accession)
                    if route == "v2"
                    else self._get_study_v1(accession)
                )
            except AccessionNotFoundError as exc:
                definite_404 = True
                last_error = exc
                continue
            except CatalogApiError as exc:
                logger.debug("Route %s failed for %s: %s", route, accession, exc)
                last_error = exc
                if route == "v2":
                    self._v2_healthy = False
                continue
            if study is not None:
                return study

        if definite_404:
            raise AccessionNotFoundError(
                f"{accession} is not present in the GWAS Catalog "
                f"(confirmed by an HTTP 404 from the metadata API)"
            )
        raise CatalogApiError(
            f"Could not retrieve {accession} from any metadata route "
            f"({', '.join(order)}): {last_error}"
        )

    def _route_order(self) -> list[str]:
        preference = self.config.prefer_api_version
        if preference == "v2":
            return ["v2"]
        if preference == "v1":
            return ["v1"]
        if self._v2_healthy is False:
            return ["v1", "v2"]
        return ["v2", "v1"]

    # -- v2 --------------------------------------------------------------

    def _get_study_v2(self, accession: str) -> Optional[Study]:
        url = f"{self.config.rest_api_v2_base}/studies/{accession}"
        payload, result = self.http.get_json(url)
        if result.status_code == 404:
            raise AccessionNotFoundError(f"{accession} not found (v2 HTTP 404)")
        if not result.ok:
            raise CatalogApiError(
                f"v2 studies endpoint returned HTTP {result.status_code}",
                category=http_status_category(result.status_code),
            )
        if not isinstance(payload, dict):
            raise CatalogApiError("v2 studies endpoint returned a non-JSON body")
        self._v2_healthy = True
        return self._study_from_v2(payload, url)

    def _study_from_v2(self, data: dict[str, Any], url: str) -> Study:
        """Map a ``StudyDto`` onto :class:`Study`."""
        accession = str(data.get("accession_id") or "").upper()
        traits = tuple(
            EfoTrait(label=t.get("efo_trait", ""), efo_id=t.get("efo_id"))
            for t in (data.get("efo_traits") or [])
            if isinstance(t, dict) and t.get("efo_trait")
        )
        background = tuple(
            EfoTrait(label=t.get("efo_trait", ""), efo_id=t.get("efo_id"))
            for t in (data.get("bg_efo_traits") or [])
            if isinstance(t, dict) and t.get("efo_trait")
        )
        ancestries = tuple(
            Ancestry(stage="initial", ancestral_groups=(group,))
            for group in (data.get("discovery_ancestry") or [])
            if group
        ) + tuple(
            Ancestry(stage="replication", ancestral_groups=(group,))
            for group in (data.get("replication_ancestry") or [])
            if group
        )
        pubmed = _clean(data.get("pubmed_id"))
        study = Study(
            study_accession=accession,
            reported_trait=_clean(data.get("disease_trait")),
            mapped_traits=traits,
            background_traits=background,
            pubmed_id=pubmed,
            summary_statistics_available=data.get("full_summary_stats_available"),
            summary_statistics_location=_clean(data.get("full_summary_stats")),
            ancestries=ancestries,
            initial_sample_description=_clean(data.get("initial_sample_size")),
            replication_sample_description=_clean(data.get("replication_sample_size")),
            genotyping_technologies=tuple(data.get("genotyping_technologies") or []),
            cohorts=tuple(data.get("cohort") or []),
            snp_count=_int_or_none(data.get("snp_count")),
            api_source="gwas_catalog_rest_v2",
            raw=data,
        )
        prov = Provenance(ValueSource.STRUCTURED_API, detail=url, confidence=1.0)
        for key in (
            "reported_trait",
            "mapped_traits",
            "summary_statistics_available",
            "summary_statistics_location",
            "initial_sample_description",
        ):
            study.provenance[key] = prov
        self._attach_publication(study)
        return study

    # -- v1 --------------------------------------------------------------

    def _get_study_v1(self, accession: str) -> Optional[Study]:
        url = f"{self.config.rest_api_v1_base}/studies/{accession}"
        payload, result = self.http.get_json(url)
        if result.status_code == 404:
            raise AccessionNotFoundError(f"{accession} not found (v1 HTTP 404)")
        if not result.ok:
            raise CatalogApiError(
                f"v1 studies endpoint returned HTTP {result.status_code}",
                category=http_status_category(result.status_code),
            )
        if not isinstance(payload, dict):
            raise CatalogApiError("v1 studies endpoint returned a non-JSON body")
        return self._study_from_v1(payload, url)

    def _study_from_v1(self, data: dict[str, Any], url: str) -> Study:
        """Map a v1 HAL study resource onto :class:`Study`."""
        accession = str(data.get("accessionId") or "").upper()
        ancestries: list[Ancestry] = []
        for anc in data.get("ancestries") or []:
            if not isinstance(anc, dict):
                continue
            groups = tuple(
                g.get("ancestralGroup", "")
                for g in (anc.get("ancestralGroups") or [])
                if isinstance(g, dict) and g.get("ancestralGroup")
            )
            countries = tuple(
                c.get("countryName", "")
                for c in (anc.get("countryOfRecruitment") or [])
                if isinstance(c, dict) and _clean(c.get("countryName"))
            )
            ancestries.append(
                Ancestry(
                    stage=str(anc.get("type") or UNKNOWN),
                    number_of_individuals=_int_or_none(anc.get("numberOfIndividuals")),
                    ancestral_groups=groups,
                    countries_of_recruitment=countries,
                )
            )

        pub = data.get("publicationInfo") or {}
        author = (pub.get("author") or {}) if isinstance(pub, dict) else {}
        disease = data.get("diseaseTrait") or {}
        full_pvalue = data.get("fullPvalueSet")

        study = Study(
            study_accession=accession,
            reported_trait=_clean(disease.get("trait")) if isinstance(disease, dict) else None,
            pubmed_id=_clean(pub.get("pubmedId")) if isinstance(pub, dict) else None,
            publication_title=_clean(pub.get("title")) if isinstance(pub, dict) else None,
            publication_journal=_clean(pub.get("publication")) if isinstance(pub, dict) else None,
            publication_date=_clean(pub.get("publicationDate")) if isinstance(pub, dict) else None,
            first_author=_clean(author.get("fullname")) if isinstance(author, dict) else None,
            study_year=_year_from_date(
                pub.get("publicationDate") if isinstance(pub, dict) else None
            ),
            summary_statistics_available=full_pvalue if isinstance(full_pvalue, bool) else None,
            ancestries=tuple(ancestries),
            initial_sample_description=_clean(data.get("initialSampleSize")),
            replication_sample_description=_clean(data.get("replicationSampleSize")),
            genotyping_technologies=tuple(
                g.get("genotypingTechnology", "")
                for g in (data.get("genotypingTechnologies") or [])
                if isinstance(g, dict) and g.get("genotypingTechnology")
            ),
            cohorts=tuple(c for c in str(data.get("cohort") or "").split("|") if c),
            snp_count=_int_or_none(data.get("snpCount")),
            api_source="gwas_catalog_rest_v1",
            raw=data,
        )
        prov = Provenance(ValueSource.STRUCTURED_API, detail=url, confidence=1.0)
        for key in (
            "reported_trait",
            "summary_statistics_available",
            "initial_sample_description",
            "ancestries",
        ):
            study.provenance[key] = prov

        # v1 does not carry the FTP location; the resolver derives it from the
        # documented directory convention and verifies it before use.
        self._attach_efo_traits_v1(study)
        return study

    def _attach_efo_traits_v1(self, study: Study) -> None:
        """Follow the v1 ``efoTraits`` link. Best-effort: absence is not fatal."""
        if study.mapped_traits:
            return
        url = f"{self.config.rest_api_v1_base}/studies/{study.study_accession}/efoTraits"
        try:
            payload, result = self.http.get_json(url)
        except GWASPokerError as exc:
            FAILURES.record_exception("efo_traits", exc, study=study.study_accession, url=url)
            return
        if not result.ok or not isinstance(payload, dict):
            return
        traits = (payload.get("_embedded") or {}).get("efoTraits") or []
        study.mapped_traits = tuple(
            EfoTrait(
                label=t.get("trait", ""),
                efo_id=t.get("shortForm"),
                uri=t.get("uri"),
            )
            for t in traits
            if isinstance(t, dict) and t.get("trait")
        )
        if study.mapped_traits:
            study.provenance["mapped_traits"] = Provenance(
                ValueSource.STRUCTURED_API, detail=url, confidence=1.0
            )

    def _attach_publication(self, study: Study) -> None:
        """Fill publication details for v2 studies, which carry only a PMID."""
        if not study.pubmed_id or study.publication_title:
            return
        url = f"{self.config.rest_api_v2_base}/publications/{study.pubmed_id}"
        try:
            payload, result = self.http.get_json(url)
        except GWASPokerError as exc:
            FAILURES.record_exception("publication", exc, study=study.study_accession, url=url)
            return
        if not result.ok or not isinstance(payload, dict):
            return
        study.publication_title = _clean(payload.get("title"))
        study.publication_journal = _clean(payload.get("journal"))
        study.publication_date = _clean(payload.get("publication_date"))
        first = payload.get("first_author") or {}
        if isinstance(first, dict):
            study.first_author = _clean(first.get("full_name"))
        study.study_year = _year_from_date(study.publication_date)

    # ------------------------------------------------------------------
    # Trait resolution and study search
    # ------------------------------------------------------------------

    def search_traits(self, text: str, *, limit: int = 20) -> list[EfoTrait]:
        """Resolve free text to EFO terms using the catalogue's own index.

        This replaces the v1 approach of running ``fuzz.token_sort_ratio`` at a
        fixed threshold of 50 against a hand-downloaded TSV. The index is the
        same one the website's search box uses, so a term the website finds is a
        term GWASPoker finds.
        """
        url = self.config.solr_search_base
        params = {"q": text, "max": max(limit * 4, 40)}
        try:
            payload, result = self.http.get_json(url, params=params)
        except GWASPokerError as exc:
            FAILURES.record_exception("trait_search", exc, url=url)
            return []
        if not result.ok or not isinstance(payload, dict):
            FAILURES.record(
                "trait_search",
                FailureCategory.API_ERROR,
                f"Trait index returned HTTP {result.status_code}",
                url=url,
            )
            return []

        docs = ((payload.get("response") or {}).get("docs")) or []
        traits: list[EfoTrait] = []
        seen: set[str] = set()
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("resourcename") != "trait":
                continue
            label = doc.get("mappedTrait") or doc.get("title")
            if not label or label in seen:
                continue
            seen.add(label)
            short = doc.get("shortForm")
            efo_id = short[0] if isinstance(short, list) and short else short
            traits.append(EfoTrait(label=label, efo_id=efo_id, uri=doc.get("mappedUri")))
            if len(traits) >= limit:
                break
        logger.debug("Trait index resolved %r to %d term(s)", text, len(traits))
        return traits

    def find_study_accessions_for_text(self, text: str, *, limit: int = 100) -> list[str]:
        """Accessions the free-text index associates directly with ``text``.

        Used as a supplement to ontology-driven retrieval: it catches studies
        whose *reported* trait matches even when the EFO mapping is broader.
        """
        params = {"q": text, "max": limit}
        try:
            payload, result = self.http.get_json(self.config.solr_search_base, params=params)
        except GWASPokerError as exc:
            FAILURES.record_exception("study_text_search", exc, url=self.config.solr_search_base)
            return []
        if not result.ok or not isinstance(payload, dict):
            return []
        docs = ((payload.get("response") or {}).get("docs")) or []
        accessions: list[str] = []
        for doc in docs:
            if isinstance(doc, dict) and doc.get("resourcename") == "study":
                acc = doc.get("accessionId")
                if acc and acc not in accessions:
                    accessions.append(acc)
        return accessions

    def studies_by_efo_trait(
        self,
        trait_label: str,
        *,
        limit: int = 50,
        summary_stats_only: bool = False,
    ) -> list[Study]:
        """All studies annotated with an exact EFO trait label.

        v2 can filter on ``full_pvalue_set`` server-side. v1 cannot, so when the
        caller wants only studies with summary statistics we oversample and
        filter here -- otherwise the first page, which is mostly top-association
        studies, would crowd out the files GWASPoker is actually for.
        """
        studies = self._studies_by_efo_trait_v2(trait_label, limit, summary_stats_only)
        if studies is None:
            fetch_limit = min(limit * OVERSAMPLE_FACTOR, MAX_FETCH) if summary_stats_only else limit
            studies = self._studies_by_efo_trait_v1(trait_label, fetch_limit)
        if studies is None:
            return []
        if summary_stats_only:
            studies = [s for s in studies if s.summary_statistics_available]
        return studies[:limit]

    def _studies_by_efo_trait_v2(
        self, trait_label: str, limit: int, summary_stats_only: bool
    ) -> Optional[list[Study]]:
        if self.config.prefer_api_version == "v1" or self._v2_healthy is False:
            return None
        url = f"{self.config.rest_api_v2_base}/studies"
        params: dict[str, Any] = {
            "efo_trait": trait_label,
            "size": min(limit, 100),
            "show_child_trait": "true",
        }
        if summary_stats_only:
            params["full_pvalue_set"] = "true"
        collected: list[Study] = []
        page = 0
        while len(collected) < limit:
            params["page"] = page
            try:
                payload, result = self.http.get_json(url, params=params)
            except GWASPokerError as exc:
                FAILURES.record_exception("studies_by_trait", exc, url=url)
                return None
            if not result.ok or not isinstance(payload, dict):
                logger.debug("v2 studies-by-trait returned HTTP %s", result.status_code)
                self._v2_healthy = False
                return None
            self._v2_healthy = True
            items = (payload.get("_embedded") or {}).get("studies") or payload.get("content") or []
            if not items:
                break
            for item in items:
                if isinstance(item, dict):
                    collected.append(self._study_from_v2(item, url))
            meta = payload.get("page") or {}
            if page + 1 >= int(meta.get("totalPages") or 0):
                break
            page += 1
        return collected[:limit]

    def _studies_by_efo_trait_v1(self, trait_label: str, limit: int) -> Optional[list[Study]]:
        url = f"{self.config.rest_api_v1_base}/studies/search/findByEfoTrait"
        collected: list[Study] = []
        page = 0
        while len(collected) < limit:
            params = {"efoTrait": trait_label, "size": min(limit, 100), "page": page}
            try:
                payload, result = self.http.get_json(url, params=params)
            except GWASPokerError as exc:
                FAILURES.record_exception("studies_by_trait", exc, url=url)
                return None
            if not result.ok or not isinstance(payload, dict):
                FAILURES.record(
                    "studies_by_trait",
                    http_status_category(result.status_code),
                    f"v1 findByEfoTrait returned HTTP {result.status_code}",
                    url=url,
                )
                return None
            items = (payload.get("_embedded") or {}).get("studies") or []
            if not items:
                break
            for item in items:
                if isinstance(item, dict):
                    collected.append(self._study_from_v1(item, url))
            meta = payload.get("page") or {}
            if page + 1 >= int(meta.get("totalPages") or 0):
                break
            page += 1
        return collected[:limit]

    # ------------------------------------------------------------------
    # Citation helpers (preserved from Module 2)
    # ------------------------------------------------------------------

    def pmid_to_doi(self, pmid: str) -> Optional[str]:
        """Resolve a PubMed ID to a DOI via the NCBI ID converter."""
        url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        try:
            payload, result = self.http.get_json(url, params={"ids": pmid, "format": "json"})
        except GWASPokerError as exc:
            FAILURES.record_exception("pmid_to_doi", exc, url=url)
            return None
        if not result.ok or not isinstance(payload, dict):
            return None
        for record in payload.get("records") or []:
            if isinstance(record, dict) and record.get("doi"):
                return str(record["doi"])
        return None

    def doi_to_bibtex(self, doi: str) -> Optional[str]:
        """Fetch a BibTeX record from doi.org content negotiation."""
        url = f"https://doi.org/{doi}"
        try:
            result = self.http.get(url, headers={"Accept": "application/x-bibtex"})
        except GWASPokerError as exc:
            FAILURES.record_exception("doi_to_bibtex", exc, url=url)
            return None
        if not result.ok:
            return None
        return result.content.decode("utf-8", errors="replace")

    def api_metadata(self) -> Optional[dict[str, Any]]:
        """The v2 ``/metadata`` document: data release date, EFO version, and so on.

        Recorded in provenance so a benchmark run can be tied to a specific
        catalogue release.
        """
        url = f"{self.config.rest_api_v2_base}/metadata"
        try:
            payload, result = self.http.get_json(url)
        except GWASPokerError:
            return None
        return payload if result.ok and isinstance(payload, dict) else None
