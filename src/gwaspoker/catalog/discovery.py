"""Study discovery and the API-first assessment workflow.

This module is the orchestration layer. It owns the decision the whole tool
exists to make:

.. code-block:: text

    study metadata (REST v2 -> v1)
              |
    structured assessment (GWAS-SSF meta.yaml; legacy sumstats API status)
              |
        sufficient? --- yes --> verdict, zero data bytes transferred
              |
              no
              |
    bounded remote probe (Range: bytes=0-N)
              |
          verdict

``--force-probe`` runs the probe even when the structured route was sufficient,
which is what makes the two routes comparable in the benchmark.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gwaspoker.catalog.models import (
    ApiAssessment,
    ApiAvailability,
    EfoTrait,
    ResolvedFile,
    Study,
)
from gwaspoker.catalog.rest_api import GwasCatalogClient, is_accession
from gwaspoker.catalog.sumstats_api import SSF_MANDATORY_FIELDS, SummaryStatisticsAssessor
from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.download.resolver import SummaryStatisticsResolver
from gwaspoker.failures import (
    FAILURES,
    AccessionNotFoundError,
    CatalogApiError,
    FailureCategory,
    FileResolutionError,
    GWASPokerError,
)
from gwaspoker.http import HttpClient
from gwaspoker.metadata.ancestry import AncestryMatch, match_population
from gwaspoker.metadata.samples import SampleSizeResolver
from gwaspoker.probe.remote import ProbeResult, RemoteProber
from gwaspoker.readiness.models import ReadinessAssessment, ReadinessVerdict
from gwaspoker.readiness.prs import assess_from_declared_fields, assess_from_mapping

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A study returned by a search, with what is known about its files.

    The four availability fields answer, at a glance, how much work getting PRS
    weights out of this study will be:

    ``file_available``
        A summary-statistics data file exists on the FTP site.
    ``api_available``
        The GWAS-SSF ``-meta.yaml`` sidecar is retrievable, so GWASPoker's
        structured route can describe the file without reading it. This is what
        predicts whether ``assess`` will need to probe.
    ``harmonised_available``
        A ``harmonised/`` product is published alongside the raw submission.
    ``ssf_status``
        ``GWAS-SSF`` when the file declares conformance to the standard (and so
        has a guaranteed mandatory column set), ``pre-GWAS-SSF`` when it does
        not, ``None`` when it could not be established.

    Each is ``None`` rather than ``False`` when it could not be determined --
    "not checked" and "checked and absent" are different facts.
    """

    study: Study
    ancestry_match: Optional[AncestryMatch] = None
    matched_traits: tuple[str, ...] = ()

    file_available: Optional[bool] = None
    api_available: Optional[bool] = None
    harmonised_available: Optional[bool] = None
    ssf_status: Optional[str] = None
    resolved_file: Optional[ResolvedFile] = None
    file_check_error: Optional[str] = None

    @property
    def is_ssf(self) -> Optional[bool]:
        """True/False for the GWAS-SSF column; ``None`` when unestablished."""
        if self.ssf_status is None:
            return None
        return self.ssf_status == "GWAS-SSF"

    def to_dict(self) -> dict[str, Any]:
        data = self.study.to_dict()
        if self.ancestry_match is not None:
            data["ancestry_match_score"] = round(self.ancestry_match.score, 3)
            data["ancestry_match_reason"] = self.ancestry_match.reason
        data["matched_traits"] = list(self.matched_traits)
        data.update(
            {
                "file_available": self.file_available,
                "api_available": self.api_available,
                "harmonised_available": self.harmonised_available,
                "ssf_status": self.ssf_status,
                "resolved_file": self.resolved_file.to_dict() if self.resolved_file else None,
                "file_check_error": self.file_check_error,
            }
        )
        return data


@dataclass
class AssessmentResult:
    """The complete outcome of assessing one study or URL."""

    target: str
    study: Optional[Study] = None
    api_assessment: Optional[ApiAssessment] = None
    resolved_file: Optional[ResolvedFile] = None
    probe: Optional[ProbeResult] = None
    readiness: Optional[ReadinessAssessment] = None
    probe_required: bool = False
    probe_performed: bool = False
    forced_probe: bool = False
    error: Optional[str] = None
    failure_category: Optional[FailureCategory] = None
    elapsed_seconds: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        return self.readiness.verdict.value if self.readiness else ReadinessVerdict.UNKNOWN.value

    @property
    def bytes_transferred(self) -> int:
        """All bytes moved for this assessment: metadata plus any data probe."""
        total = self.api_assessment.bytes_received if self.api_assessment else 0
        if self.probe is not None:
            total += self.probe.transfer.received_bytes
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "study": self.study.to_dict() if self.study else None,
            "api_assessment": self.api_assessment.to_dict() if self.api_assessment else None,
            "resolved_file": self.resolved_file.to_dict() if self.resolved_file else None,
            "probe": self.probe.to_dict() if self.probe else None,
            "readiness": self.readiness.to_dict() if self.readiness else None,
            "probe_required": self.probe_required,
            "probe_performed": self.probe_performed,
            "forced_probe": self.forced_probe,
            "bytes_transferred": self.bytes_transferred,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "error": self.error,
            "failure_category": self.failure_category.value if self.failure_category else None,
            "notes": list(self.notes),
        }


class DiscoveryService:
    """Search, assess and probe. One shared HTTP session for all of it."""

    def __init__(
        self,
        config: Optional[GWASPokerConfig] = None,
        *,
        enable_llm: bool = False,
    ) -> None:
        self.config = config or get_config()
        self.http = HttpClient(self.config)
        self.catalog = GwasCatalogClient(self.config, self.http)
        self.sumstats = SummaryStatisticsAssessor(self.config, self.http)
        self.resolver = SummaryStatisticsResolver(self.config, self.http)
        self.prober = RemoteProber(self.config, self.http)
        self.samples = SampleSizeResolver(enable_llm=enable_llm, llm_model=self.config.llm_model)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> DiscoveryService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        trait: str,
        *,
        population: Optional[str] = None,
        limit: int = 25,
        summary_stats_only: bool = False,
        resolve_samples: bool = True,
        check_files: bool = True,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[SearchResult]:
        """Find studies for a phenotype.

        The trait is resolved through the Catalog's own ontology index first, so
        results are driven by EFO annotation rather than by string similarity.
        Free-text study hits supplement that, catching studies whose *reported*
        trait matches even where the EFO mapping is broader.

        With ``check_files`` (the default) each returned study is also inspected
        on the FTP site to fill ``file_available``, ``api_available``,
        ``harmonised_available`` and ``ssf_status``. That costs one directory
        listing plus one sidecar fetch per study -- roughly a second each -- so
        it is done **after** filtering and truncation, on the ``limit`` studies
        that will actually be shown rather than on everything retrieved.
        """
        traits = self.catalog.search_traits(trait, limit=8)
        if traits:
            logger.info(
                "Resolved %r to EFO term(s): %s",
                trait,
                ", ".join(f"{t.label} ({t.efo_id})" for t in traits[:5]),
            )
        else:
            logger.warning(
                "The GWAS Catalog trait index returned no ontology term for %r; "
                "falling back to a free-text study search.",
                trait,
            )

        collected: dict[str, SearchResult] = {}

        for efo in traits:
            if len(collected) >= limit:
                break
            studies = self.catalog.studies_by_efo_trait(
                efo.label,
                limit=limit * 2,
                summary_stats_only=summary_stats_only,
            )
            for study in studies:
                self._add_result(collected, study, efo, population)

        if len(collected) < limit:
            self._add_free_text_hits(collected, trait, population, limit, summary_stats_only)

        results = list(collected.values())
        if resolve_samples:
            # Cheap: no network unless the LLM fallback is enabled.
            for result in results:
                self.samples.resolve(result.study)

        if population:
            results = [r for r in results if r.ancestry_match and r.ancestry_match.score > 0]

        results.sort(
            key=lambda r: (
                r.ancestry_match.score if r.ancestry_match else 0.0,
                1 if r.study.summary_statistics_available else 0,
                r.study.samples.implied_total() or 0,
            ),
            reverse=True,
        )
        results = results[:limit]

        if check_files:
            for index, result in enumerate(results, start=1):
                if progress is not None:
                    progress(index, len(results), result.study.study_accession)
                self._check_file_availability(result)

        return results

    def _check_file_availability(self, result: SearchResult) -> None:
        """Fill the availability fields for one study.

        Costs one FTP directory listing plus, when a sidecar exists, one small
        metadata fetch. Failures are recorded on the result and never raised:
        a study whose files cannot be listed still belongs in the table, marked
        as such.
        """
        study = result.study
        accession = study.study_accession

        if study.summary_statistics_available is False:
            # The Catalog says this study publishes only top associations.
            # There is no directory to list, so nothing further is unknown.
            result.file_available = False
            result.api_available = False
            result.harmonised_available = False
            return

        try:
            resolved = self.resolver.resolve(
                accession,
                harmonised="auto",
                location_hint=study.summary_statistics_location,
            )
        except (FileResolutionError, GWASPokerError) as exc:
            result.file_available = False
            result.file_check_error = str(exc)
            FAILURES.record_exception("search_file_check", exc, study=accession)
            return

        result.resolved_file = resolved
        result.file_available = True
        result.harmonised_available = any(c.is_harmonised for c in resolved.candidates)
        if not study.summary_statistics_location:
            study.summary_statistics_location = resolved.directory_url

        if not resolved.metadata_url:
            # No GWAS-SSF sidecar: the structured route cannot describe this
            # file, so `assess` will have to probe it.
            result.api_available = False
            return

        meta = self.sumstats.fetch_ssf_metadata(resolved.metadata_url)
        if meta is None:
            result.api_available = False
            return

        result.api_available = True
        result.ssf_status = meta.ssf_status
        if meta.genome_assembly and not study.genome_build:
            study.genome_build = meta.genome_assembly
        # The sidecar often carries authoritative sample counts; now that we
        # have it, let it improve on whatever regex extraction produced.
        self.samples.resolve(study, ssf_metadata=meta)

    def _add_result(
        self,
        collected: dict[str, SearchResult],
        study: Study,
        efo: Optional[EfoTrait],
        population: Optional[str],
    ) -> None:
        accession = study.study_accession
        if not accession:
            return
        existing = collected.get(accession)
        if existing is not None:
            if efo and efo.label not in existing.matched_traits:
                existing.matched_traits = (*existing.matched_traits, efo.label)
            return
        collected[accession] = SearchResult(
            study=study,
            ancestry_match=match_population(
                population, [g for a in study.ancestries for g in a.ancestral_groups]
            ),
            matched_traits=(efo.label,) if efo else (),
        )

    def _add_free_text_hits(
        self,
        collected: dict[str, SearchResult],
        trait: str,
        population: Optional[str],
        limit: int,
        summary_stats_only: bool,
    ) -> None:
        accessions = self.catalog.find_study_accessions_for_text(trait, limit=limit * 2)
        for accession in accessions:
            if len(collected) >= limit * 2 or accession in collected:
                continue
            try:
                study = self.catalog.get_study(accession)
            except (AccessionNotFoundError, CatalogApiError) as exc:
                FAILURES.record_exception("search", exc, study=accession)
                continue
            if summary_stats_only and not study.summary_statistics_available:
                continue
            self._add_result(collected, study, None, population)

    # ------------------------------------------------------------------
    # assess
    # ------------------------------------------------------------------

    def assess(
        self,
        target: str,
        *,
        prs_target: str = "prs",
        harmonised: Optional[str] = None,
        force_probe: bool = False,
        probe_bytes: Optional[int] = None,
        skip_api: bool = False,
    ) -> AssessmentResult:
        """Assess a study accession or a direct URL for PRS readiness."""
        started = time.perf_counter()
        result = AssessmentResult(target=target, forced_probe=force_probe)
        harmonised = harmonised or self.config.prefer_harmonised

        if _looks_like_url(target):
            self._assess_url(result, target, prs_target, probe_bytes)
            result.elapsed_seconds = time.perf_counter() - started
            return result

        if not is_accession(target):
            result.error = (
                f"{target!r} is neither a GCST accession nor an http(s) URL. "
                "For a local file use `gwaspoker scan`."
            )
            result.failure_category = FailureCategory.INVALID_ACCESSION
            result.elapsed_seconds = time.perf_counter() - started
            return result

        self._assess_accession(
            result,
            target.upper(),
            prs_target=prs_target,
            harmonised=harmonised,
            force_probe=force_probe,
            probe_bytes=probe_bytes,
            skip_api=skip_api,
        )
        result.elapsed_seconds = time.perf_counter() - started
        return result

    def _assess_url(
        self,
        result: AssessmentResult,
        url: str,
        prs_target: str,
        probe_bytes: Optional[int],
    ) -> None:
        """A bare URL has no catalogue metadata, so it goes straight to the probe."""
        result.notes = (
            "A direct URL carries no GWAS Catalog metadata, so the structured "
            "assessment route does not apply; the file was probed directly.",
        )
        result.probe_required = True
        probe = self.prober.probe_url(url, probe_bytes=probe_bytes)
        result.probe = probe
        result.probe_performed = True
        if probe.succeeded and probe.mapping is not None:
            result.readiness = assess_from_mapping(
                probe.mapping,
                target=prs_target,
                evidence_source="file_probe",
                header=probe.header.raw_header if probe.header else (),
            )
        else:
            result.error = probe.error
            result.failure_category = probe.failure_category

    def _assess_accession(
        self,
        result: AssessmentResult,
        accession: str,
        *,
        prs_target: str,
        harmonised: str,
        force_probe: bool,
        probe_bytes: Optional[int],
        skip_api: bool,
    ) -> None:
        # --- 1. Study metadata -----------------------------------------
        try:
            study = self.catalog.get_study(accession)
        except GWASPokerError as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            FAILURES.record_exception("study_metadata", exc, study=accession)
            return
        result.study = study

        if study.summary_statistics_available is False:
            result.error = (
                f"{accession} has no full summary statistics in the GWAS Catalog "
                "(the study reports only top associations), so there is no file to assess."
            )
            result.failure_category = FailureCategory.NOT_REPRESENTED
            return

        # --- 2. Resolve the file ---------------------------------------
        try:
            resolved = self.resolver.resolve(
                accession,
                harmonised=harmonised,
                location_hint=study.summary_statistics_location,
            )
        except (FileResolutionError, GWASPokerError) as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            FAILURES.record_exception("file_resolution", exc, study=accession)
            return
        result.resolved_file = resolved
        if not study.summary_statistics_location:
            study.summary_statistics_location = resolved.directory_url

        # --- 3. Structured assessment -----------------------------------
        if skip_api:
            api = ApiAssessment(
                study_accession=accession,
                availability=ApiAvailability.NOT_QUERIED,
                notes=("--no-api was given; the structured route was skipped.",),
            )
        else:
            api = self.sumstats.assess(accession, metadata_url=resolved.metadata_url)
        result.api_assessment = api

        ssf = api.ssf_metadata
        if ssf is not None:
            if ssf.md5sum:
                resolved.expected_md5 = ssf.md5sum
            if ssf.genome_assembly and not study.genome_build:
                study.genome_build = ssf.genome_assembly
        self.samples.resolve(study, ssf_metadata=ssf)

        # --- 4. Is the structured route sufficient? ----------------------
        result.probe_required = not api.sufficient_for_prs_assessment

        if api.sufficient_for_prs_assessment:
            fields = api.fields_available or SSF_MANDATORY_FIELDS
            result.readiness = assess_from_declared_fields(
                fields,
                target=prs_target,
                evidence_source=f"gwas_ssf_metadata ({ssf.file_type})" if ssf else "structured_api",
                note=(
                    "Assessed from the file's declared GWAS-SSF conformance; the data "
                    "file was not read."
                ),
            )
            result.notes = (
                *result.notes,
                "Raw file probing was unnecessary: the structured metadata was sufficient.",
            )

        # --- 5. Probe when needed, or when forced ------------------------
        if result.probe_required or force_probe:
            probe = self.prober.probe_url(
                resolved.url, probe_bytes=probe_bytes, filename=resolved.name
            )
            result.probe = probe
            result.probe_performed = True

            if probe.succeeded and probe.mapping is not None:
                probe_readiness = assess_from_mapping(
                    probe.mapping,
                    target=prs_target,
                    evidence_source="file_probe",
                    header=probe.header.raw_header if probe.header else (),
                )
                if result.readiness is None:
                    result.readiness = probe_readiness
                else:
                    # Both routes ran (--force-probe). Keep the observed result,
                    # which is the stronger evidence, and note any disagreement.
                    if probe_readiness.verdict is not result.readiness.verdict:
                        result.notes = (
                            *result.notes,
                            f"Structured metadata implied {result.readiness.verdict.value} "
                            f"but the probed header gives {probe_readiness.verdict.value}.",
                        )
                    result.readiness = probe_readiness
            elif result.readiness is None:
                result.error = probe.error
                result.failure_category = probe.failure_category
                if probe.error:
                    FAILURES.record(
                        "probe",
                        probe.failure_category or FailureCategory.UNKNOWN,
                        probe.error,
                        study=accession,
                        url=resolved.url,
                    )

    # ------------------------------------------------------------------
    # probe only
    # ------------------------------------------------------------------

    def probe(
        self,
        target: str,
        *,
        harmonised: Optional[str] = None,
        probe_bytes: Optional[int] = None,
    ) -> tuple[ProbeResult, Optional[Study], Optional[ResolvedFile]]:
        """Probe a study accession or a URL without the structured route."""
        harmonised = harmonised or self.config.prefer_harmonised

        if _looks_like_url(target):
            return self.prober.probe_url(target, probe_bytes=probe_bytes), None, None

        accession = target.upper()
        study = self.catalog.get_study(accession)
        resolved = self.resolver.resolve(
            accession,
            harmonised=harmonised,
            location_hint=study.summary_statistics_location,
        )
        probe = self.prober.probe_url(resolved.url, probe_bytes=probe_bytes, filename=resolved.name)
        return probe, study, resolved


def _looks_like_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://", "ftp://"))
