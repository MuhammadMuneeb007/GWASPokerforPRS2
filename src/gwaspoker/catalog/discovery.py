"""Study discovery and the metadata-first assessment workflow.

This module is the orchestration layer. It owns the decision the whole tool
exists to make:

.. code-block:: text

    GWAS Catalog REST API  (v2, falling back to v1)
              |
    study / trait discovery
              |
    GWAS-SSF sidecar metadata   (a static -meta.yaml file, ~700 bytes)
              |
    structured file assessment
              |
        sufficient? --- yes --> verdict, zero data bytes transferred
              |
              no
              |
    bounded remote probe (Range: bytes=0-N)
              |
          verdict

A note on naming. This is deliberately **not** called "API-first". The GWAS
Catalog REST API answers questions about *studies*; it says nothing about a
summary-statistics file's columns. The Summary Statistics API that once did is
withdrawn (HTTP 410), and the Catalog states that API access to the full
genome-wide collection is being redeveloped. The structured route GWASPoker
actually uses is the GWAS-SSF ``-meta.yaml`` sidecar -- a static file on the
repository, not an API -- so the code and the manuscript say so.

``--force-probe`` runs the probe even when the structured route was sufficient,
which is what makes the two routes comparable in the benchmark.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gwaspoker.catalog.models import (
    ApiAssessment,
    ApiAvailability,
    EfoTrait,
    ResolvedFile,
    Study,
)
from gwaspoker.catalog.rest_api import GwasCatalogClient
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
from gwaspoker.inputs import InputResolutionError, InputTarget, resolve_input
from gwaspoker.metadata.ancestry import AncestryMatch, match_population
from gwaspoker.metadata.samples import SampleSizeResolver
from gwaspoker.probe.remote import ProbeResult, RemoteProber
from gwaspoker.readiness.models import ReadinessAssessment, ReadinessVerdict
from gwaspoker.readiness.prs import assess_from_declared_fields, assess_from_mapping

logger = logging.getLogger(__name__)

#: Failure categories that justify recording a definite ``False`` rather than
#: leaving a field unknown. Everything else -- timeouts, 5xx, connection
#: resets -- means "we did not find out", and must not become a negative
#: measurement in the output.
_DEFINITE_ABSENCE = frozenset(
    {
        FailureCategory.FILE_NOT_FOUND,
        FailureCategory.HTTP_404,
        FailureCategory.NOT_REPRESENTED,
    }
)


def _matches_any(text: Optional[str], patterns: Sequence[str]) -> bool:
    """Case-insensitive substring match against any pattern."""
    if not text:
        return False
    lowered = text.lower()
    return any(p.strip().lower() in lowered for p in patterns if p.strip())


@dataclass
class SearchResult:
    """A study returned by a search, with what is known about its files.

    The availability fields answer, at a glance, how much work getting PRS
    weights out of this study will be:

    ``file_available``
        A summary-statistics data file exists in the study's repository
        directory.
    ``metadata_available``
        The GWAS-SSF ``-meta.yaml`` sidecar is retrievable. This is a **static
        metadata file served over HTTP, not an API** -- the GWAS Catalog's
        Summary Statistics API is withdrawn, and the replacement for the
        full-summary-statistics collection is still being redeveloped.
        Retrievable metadata does **not** on its own mean a probe is
        unnecessary; see ``probe_needed``.
    ``harmonised_available``
        A ``harmonised/`` product is published alongside the raw submission.
    ``ssf_status``
        The declared ``file_type``: ``GWAS-SSF`` when the file declares
        conformance to the standard, otherwise the declared string
        (``pre-GWAS-SSF``, ``non-GWAS-SSF``), or ``None`` when unestablished.
    ``prs_from_metadata``
        The PRS verdict derivable from the declaration alone. Only a GWAS-SSF
        declaration fixes the mandatory column set, so this is ``READY``
        exactly when ``ssf_status == "GWAS-SSF"`` and ``None`` otherwise.
    ``probe_needed``
        Whether bytes must be read from the data file to reach a verdict.
        ``False`` only when the declaration already settles it.

    Every field is ``None`` rather than ``False`` when it could not be
    determined. A network timeout must never be recorded as "checked, absent" --
    these columns are intended for a manuscript dataset, and the difference
    between "no" and "unknown" is the difference between a measurement and a
    guess.
    """

    study: Study
    ancestry_match: Optional[AncestryMatch] = None
    matched_traits: tuple[str, ...] = ()

    file_available: Optional[bool] = None
    metadata_available: Optional[bool] = None
    harmonised_available: Optional[bool] = None
    ssf_status: Optional[str] = None
    resolved_file: Optional[ResolvedFile] = None
    file_check_error: Optional[str] = None
    file_check_category: Optional[str] = None

    @property
    def is_ssf(self) -> Optional[bool]:
        """True/False for the GWAS-SSF column; ``None`` when unestablished."""
        if self.ssf_status is None:
            return None
        return self.ssf_status == "GWAS-SSF"

    @property
    def prs_from_metadata(self) -> Optional[str]:
        """PRS verdict derivable from the declaration alone.

        A GWAS-SSF v1.0 declaration fixes the mandatory columns
        (``chromosome``, ``base_pair_location``, ``effect_allele``,
        ``other_allele``, an effect measure, ``standard_error``,
        ``effect_allele_frequency``, ``p_value``), which satisfies every
        required PRS field. No other declaration guarantees anything, so this
        is ``READY`` exactly when the file declares GWAS-SSF.
        """
        return "READY" if self.is_ssf else None

    @property
    def probe_needed(self) -> Optional[bool]:
        """Must bytes be read from the data file to reach a PRS verdict?

        ``None`` means the question does not arise or could not be settled:
        there is no file, or the checks did not complete.
        """
        if self.file_available is not True:
            return None
        return not self.is_ssf

    def to_dict(self) -> dict[str, Any]:
        data = self.study.to_dict()
        if self.ancestry_match is not None:
            data["ancestry_match_score"] = round(self.ancestry_match.score, 3)
            data["ancestry_match_reason"] = self.ancestry_match.reason
        data["matched_traits"] = list(self.matched_traits)
        data.update(
            {
                "file_available": self.file_available,
                "metadata_available": self.metadata_available,
                "harmonised_available": self.harmonised_available,
                "ssf_status": self.ssf_status,
                "prs_from_metadata": self.prs_from_metadata,
                "probe_needed": self.probe_needed,
                "resolved_file": self.resolved_file.to_dict() if self.resolved_file else None,
                "file_check_error": self.file_check_error,
                "file_check_category": self.file_check_category,
            }
        )
        return data


@dataclass
class AssessmentResult:
    """The complete outcome of assessing one study or URL."""

    target: str
    #: What the target turned out to be: a GWAS Catalog accession, a direct
    #: URL, or a local file. Recorded so an external-validation experiment can
    #: separate catalogue studies from arbitrary public URLs.
    input_target: Optional[InputTarget] = None
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
        payload: dict[str, Any] = {
            "target": self.target,
            "input_type": (self.input_target.input_type.value if self.input_target else None),
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
        if self.input_target is not None:
            payload["input"] = self.input_target.to_dict()
        return payload


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
        #: How many studies the last :meth:`search` dropped via ``exclude``.
        #: Reported to the user so a filter never removes results silently.
        self.last_excluded: int = 0

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
        exclude: Sequence[str] = (),
    ) -> list[SearchResult]:
        """Find studies for a phenotype. **Metadata only -- no file checks.**

        The trait is resolved through the Catalog's own ontology index first, so
        results are driven by EFO annotation rather than by string similarity.
        Free-text study hits supplement that, catching studies whose *reported*
        trait matches even where the EFO mapping is broader.

        File availability is deliberately *not* done here: it is a separate,
        much more expensive stage. Call :meth:`check_files` on the returned list
        when you want the File / SSF Meta / Harmonised / GWAS-SSF columns. Keeping
        the two apart is what lets a caller show "retrieving studies" and
        "checking N files" as distinct phases with honest progress.

        ``exclude`` drops studies whose reported trait contains any of the given
        substrings, case-insensitively. The count dropped is returned to the
        caller through :attr:`last_excluded` so nothing disappears silently.
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

        if exclude:
            kept = [r for r in results if not _matches_any(r.study.reported_trait, exclude)]
            self.last_excluded = len(results) - len(kept)
            results = kept
        else:
            self.last_excluded = 0

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
        return results[:limit]

    # ------------------------------------------------------------------
    # file availability -- the expensive stage
    # ------------------------------------------------------------------

    def check_files(
        self,
        results: Sequence[SearchResult],
        *,
        workers: Optional[int] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[SearchResult]:
        """Fill the file-availability fields for already-retrieved studies.

        Each study costs a directory listing, often a second listing for
        ``harmonised/``, and a metadata sidecar fetch -- so **two or three
        requests**, not two. Done sequentially that is roughly a second per
        study, which is why this runs on a thread pool.

        Concurrency does not bypass the rate limit: every worker goes through
        the same process-wide limiter in :class:`~gwaspoker.http.HttpClient`, so
        ``workers`` controls how much latency is overlapped, not how many
        requests per second are issued.
        """
        results = list(results)
        if not results:
            return results

        worker_count = max(1, min(workers or self.config.max_workers, len(results)))
        completed = 0
        lock = threading.Lock()

        def run(result: SearchResult) -> None:
            nonlocal completed
            try:
                self._check_file_availability(result)
            finally:
                with lock:
                    completed += 1
                    if progress is not None:
                        progress(completed, len(results), result.study.study_accession)

        if worker_count == 1:
            for result in results:
                run(result)
            return results

        logger.info(
            "Checking %d published file(s) with %d workers (rate limit %.1f req/s)",
            len(results),
            worker_count,
            self.config.max_requests_per_second,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(run, result) for result in results]
            for future in as_completed(futures):
                # run() never raises, but surface a programming error rather
                # than silently dropping it.
                future.result()
        return results

    def _check_file_availability(self, result: SearchResult) -> None:
        """Fill the availability fields for one study.

        Failures are recorded on the result and never raised: a study whose
        files cannot be listed still belongs in the table, marked as such.

        The distinction that matters is **absent versus unknown**. A 404 means
        the directory or the file genuinely is not there, and ``False`` is the
        right answer. A timeout or a 5xx means we did not find out, and the
        field stays ``None``. These columns are intended for a manuscript
        dataset; recording a transient network failure as "no file" would put a
        fabricated negative into it.
        """
        study = result.study
        accession = study.study_accession

        if study.summary_statistics_available is False:
            # The Catalog says this study publishes only top associations.
            # There is no directory to list, so nothing further is unknown.
            result.file_available = False
            result.metadata_available = False
            result.harmonised_available = False
            return

        try:
            resolved = self.resolver.resolve(
                accession,
                harmonised="auto",
                location_hint=study.summary_statistics_location,
            )
        except GWASPokerError as exc:
            record = FAILURES.record_exception("search_file_check", exc, study=accession)
            result.file_check_error = str(exc)
            result.file_check_category = record.category.value
            # Only a definite "not there" answer justifies False.
            if record.category in _DEFINITE_ABSENCE:
                result.file_available = False
                result.metadata_available = False
                result.harmonised_available = False
            return

        result.resolved_file = resolved
        result.file_available = True
        result.harmonised_available = any(c.is_harmonised for c in resolved.candidates)
        if not study.summary_statistics_location:
            study.summary_statistics_location = resolved.directory_url

        if not resolved.metadata_url:
            # The directory listed cleanly and contains no sidecar: a definite
            # absence, so False rather than None.
            result.metadata_available = False
            return

        meta = self.sumstats.fetch_ssf_metadata(resolved.metadata_url)
        if meta is None:
            # The sidecar was listed but could not be fetched or parsed. We do
            # not know whether it describes a conformant file.
            result.metadata_available = None
            result.file_check_error = f"sidecar listed but unreadable: {resolved.metadata_url}"
            result.file_check_category = FailureCategory.METADATA_MISSING.value
            return

        result.metadata_available = True
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
        """Assess a GWAS Catalog accession or a direct summary-statistics URL.

        Everything after the file is located is identical for both: the same
        bounded probe, the same header detection, mapping, value validation and
        readiness rules. Only the step that produces a URL differs.
        """
        started = time.perf_counter()
        result = AssessmentResult(target=target, forced_probe=force_probe)
        harmonised = harmonised or self.config.prefer_harmonised

        try:
            resolved_input = resolve_input(target)
        except InputResolutionError as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            result.elapsed_seconds = time.perf_counter() - started
            return result
        result.input_target = resolved_input

        if resolved_input.is_direct_url:
            self._assess_url(result, resolved_input.url, prs_target, probe_bytes)
            result.elapsed_seconds = time.perf_counter() - started
            return result

        self._assess_accession(
            result,
            resolved_input.accession,
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
        """A bare URL has no catalogue metadata, so it goes straight to the probe.

        The probe is still bounded: a direct URL never triggers a full download.
        """
        notes = [
            "A direct URL carries no GWAS Catalog metadata, so the structured "
            "assessment route does not apply; the file was probed directly.",
        ]
        if result.input_target is not None and result.input_target.normalisation_note:
            notes.append(result.input_target.normalisation_note)
        result.notes = tuple(notes)
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
                    validation=probe.value_validation,
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
        """Probe a GWAS Catalog accession or a direct URL, bounded either way."""
        harmonised = harmonised or self.config.prefer_harmonised
        resolved_input = resolve_input(target)

        if resolved_input.is_direct_url:
            probe = self.prober.probe_url(resolved_input.url, probe_bytes=probe_bytes)
            return probe, None, None

        accession = resolved_input.accession
        study = self.catalog.get_study(accession)
        resolved = self.resolver.resolve(
            accession,
            harmonised=harmonised,
            location_hint=study.summary_statistics_location,
        )
        probe = self.prober.probe_url(resolved.url, probe_bytes=probe_bytes, filename=resolved.name)
        return probe, study, resolved
