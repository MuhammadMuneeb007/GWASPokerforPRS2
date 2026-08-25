"""Rich terminal output.

All user-facing formatting lives here so that the domain modules return data and
nothing else. The console never invents a value: a field GWASPoker could not
establish is rendered as a dim ``unknown``, never as ``0``, ``-`` or a blank
that reads as zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gwaspoker.catalog.discovery import AssessmentResult, SearchResult
from gwaspoker.catalog.models import ApiAvailability
from gwaspoker.metadata.ancestry import summarize_ancestries
from gwaspoker.probe.remote import ProbeResult
from gwaspoker.processing.formats import human_size
from gwaspoker.readiness.models import ReadinessAssessment, ReadinessVerdict, RequirementStatus

console = Console()

_VERDICT_STYLE = {
    ReadinessVerdict.READY: "bold green",
    ReadinessVerdict.PARTIAL: "bold yellow",
    ReadinessVerdict.NOT_READY: "bold red",
    ReadinessVerdict.UNKNOWN: "dim",
}

_STATUS_STYLE = {
    RequirementStatus.SATISFIED: "green",
    RequirementStatus.UNCERTAIN: "yellow",
    RequirementStatus.MISSING: "red",
}


def unknown_or(value: Any, formatter=None) -> Text:
    """Render a value, or a dim ``unknown`` when it is absent."""
    if value is None or value == "":
        return Text("unknown", style="dim italic")
    return Text(formatter(value) if formatter else str(value))


def _count(value: Optional[int]) -> Text:
    if value is None:
        return Text("unknown", style="dim italic")
    return Text(f"{value:,}")


def _boolean(value: Optional[bool], true_label: str = "yes", false_label: str = "no") -> Text:
    if value is None:
        return Text("unknown", style="dim italic")
    return Text(true_label, style="green") if value else Text(false_label, style="red")


def _tristate(value: Optional[bool]) -> Text:
    """Compact yes / no / ? for the narrow availability columns.

    ``?`` means the fact could not be established, which is distinct from
    ``no``. It is never rendered as a blank, because a blank cell reads as
    "false" to most people.
    """
    if value is None:
        return Text("?", style="dim")
    return Text("yes", style="green") if value else Text("no", style="red")


# ----------------------------------------------------------------------
# search
# ----------------------------------------------------------------------


def render_search_results(
    results: Iterable[SearchResult], *, trait: str, population: Optional[str] = None
) -> None:
    """Print the search table."""
    results = list(results)
    heading = f"GWAS Catalog studies for [bold]{trait}[/bold]"
    if population:
        heading += f" in [bold]{population}[/bold]"
    console.print()
    console.print(heading)

    if not results:
        console.print(
            "[yellow]No studies matched.[/yellow] Try a broader term, or check the trait "
            "name at https://www.ebi.ac.uk/gwas/"
        )
        return

    table = Table(show_lines=False, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("GCST", style="bold")
    table.add_column("Trait", max_width=34, overflow="ellipsis")
    table.add_column("Population", max_width=22, overflow="ellipsis")
    table.add_column("N", justify="right")
    table.add_column("Cases", justify="right")
    table.add_column("Controls", justify="right")
    # What it takes to actually use this study, at a glance.
    table.add_column("File", justify="center")
    table.add_column("API", justify="center")
    table.add_column("Harmonised", justify="center")
    table.add_column("GWAS-SSF", justify="center")
    table.add_column("Year", justify="right")
    table.add_column("PMID", justify="right")

    for result in results:
        study = result.study
        samples = study.samples
        file_available = (
            result.file_available
            if result.file_available is not None
            else study.summary_statistics_available
        )
        table.add_row(
            study.study_accession,
            unknown_or(study.reported_trait),
            summarize_ancestries([g for a in study.ancestries for g in a.ancestral_groups]),
            _count(samples.total),
            _count(samples.cases),
            _count(samples.controls),
            _tristate(file_available),
            _tristate(result.api_available),
            _tristate(result.harmonised_available),
            _tristate(result.is_ssf),
            unknown_or(study.study_year),
            unknown_or(study.pubmed_id),
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(results)} study/studies. "
        "File = a data file is published · API = GWAS-SSF metadata is retrievable, "
        "so `assess` needs no probe · Harmonised = a harmonised/ product exists · "
        "GWAS-SSF = the file declares the standard's column set.[/dim]"
    )
    console.print("[dim]Sample counts carry provenance; run with --format json to see it.[/dim]")

    unreadable = [r for r in results if r.file_check_error]
    if unreadable:
        console.print(
            f"[dim]{len(unreadable)} study/studies had a published file that could not be "
            "listed; see the failure summary below.[/dim]"
        )


def render_sample_provenance(results: Iterable[SearchResult]) -> None:
    """Print where each sample count came from."""
    table = Table(title="Sample-count provenance", header_style="bold cyan", box=None)
    table.add_column("GCST")
    table.add_column("N source")
    table.add_column("Cases source")
    table.add_column("Controls source")

    for result in results:
        samples = result.study.samples
        table.add_row(
            result.study.study_accession,
            samples.total_source.value,
            samples.cases_source.value,
            samples.controls_source.value,
        )
    console.print(table)


# ----------------------------------------------------------------------
# probe
# ----------------------------------------------------------------------


def render_probe(probe: ProbeResult, *, resolved: Any = None, study: Any = None) -> None:
    """Print the outcome of a bounded probe."""
    console.print()
    if study is not None:
        console.print(
            f"[bold]Study:[/bold] {study.study_accession} — {study.reported_trait or 'unknown trait'}"
        )
    console.print(f"[bold]File:[/bold] {probe.filename}")
    console.print(f"[bold]Source:[/bold] [dim]{probe.source}[/dim]")

    if resolved is not None and resolved.selection_reason:
        console.print(f"[bold]Selected because:[/bold] [dim]{resolved.selection_reason}[/dim]")

    transfer = probe.transfer
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("Format", probe.format_label)
    table.add_row("Remote size", human_size(transfer.remote_file_size))
    table.add_row("Bytes inspected", human_size(transfer.received_bytes))
    if transfer.transfer_reduction is not None:
        table.add_row("Transfer avoided", f"{transfer.transfer_reduction * 100:.4f}%")
    table.add_row(
        "Range requests",
        (
            "used"
            if transfer.range_used
            else (
                "unsupported, stream bounded locally"
                if transfer.range_supported is False
                else "unknown"
            )
        ),
    )
    table.add_row("Transfer time", f"{transfer.transfer_time_seconds:.2f} s")
    if probe.encoding:
        table.add_row("Encoding", f"{probe.encoding} ({probe.encoding_confidence:.0%} confidence)")
    if probe.decompression and probe.decompression.expansion_ratio:
        table.add_row(
            "Decompressed",
            f"{human_size(len(probe.decompression.data))} "
            f"({probe.decompression.expansion_ratio:.1f}x expansion)",
        )
    console.print(table)

    if not probe.succeeded:
        console.print()
        console.print(
            Panel(
                f"{probe.error}\n\n[dim]Failure category: "
                f"{probe.failure_category.value if probe.failure_category else 'unknown'}[/dim]",
                title="Probe failed",
                border_style="red",
            )
        )
        return

    header = probe.header
    console.print()
    console.print(
        f"[bold]Detected header[/bold] "
        f"[dim](row {header.header_row_index}, {header.delimiter_label}-separated, "
        f"{header.confidence:.0%} confidence)[/dim]"
    )
    if header.preamble_lines:
        console.print(
            f"[dim]{len(header.preamble_lines)} preamble line(s) skipped before it.[/dim]"
        )
    console.print("  " + "  ".join(header.raw_header))

    if probe.mapping is not None:
        render_mapping(probe.mapping)


def render_mapping(mapping: Any) -> None:
    """Print the raw-to-canonical column mapping, in header order."""
    console.print()
    table = Table(title="Canonical column mapping", header_style="bold cyan", box=None)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Column")
    table.add_column("Canonical concept")
    table.add_column("PRS")
    table.add_column("Method")
    table.add_column("Conf.", justify="right")

    for index, column in enumerate(mapping.columns):
        resolved = column.canonical_name != "unknown"
        table.add_row(
            str(index),
            column.raw_name,
            Text(column.canonical_name, style="" if resolved else "dim italic"),
            column.prs_tool_symbol or "",
            column.mapping_method,
            f"{column.confidence:.2f}" if column.confidence else "—",
            style=None if resolved else "dim",
        )
    console.print(table)

    if mapping.unresolved:
        console.print(
            f"[dim]{len(mapping.unresolved)} column(s) left as [italic]unknown[/italic] rather "
            "than forced onto a concept: "
            + ", ".join(c.raw_name for c in mapping.unresolved[:12])
            + ("..." if len(mapping.unresolved) > 12 else "")
            + "[/dim]"
        )


# ----------------------------------------------------------------------
# assess
# ----------------------------------------------------------------------


def render_assessment(result: AssessmentResult, *, show_mapping: bool = False) -> None:
    """Print a full PRS-readiness assessment."""
    console.print()
    study = result.study
    if study is not None:
        console.print(f"[bold]Study:[/bold] {study.study_accession}")
        console.print(f"[bold]Trait:[/bold] {study.reported_trait or 'unknown'}")
        console.print(f"[bold]Source:[/bold] GWAS Catalog ({study.api_source})")
    else:
        console.print(f"[bold]Target:[/bold] {result.target}")

    api = result.api_assessment
    if api is not None:
        console.print(f"[bold]API assessment:[/bold] {_api_summary(api)}")
        if api.ssf_metadata is not None:
            meta = api.ssf_metadata
            console.print(
                f"[bold]Declared format:[/bold] {meta.file_type or 'unknown'} "
                f"[dim](genome build {meta.genome_assembly or 'unknown'}, "
                f"harmonised={meta.is_harmonised})[/dim]"
            )

    resolved = result.resolved_file
    if resolved is not None:
        console.print(
            f"[bold]Remote file:[/bold] {resolved.name} "
            f"[dim]({'harmonised' if resolved.is_harmonised else 'raw'})[/dim]"
        )
        console.print(f"[bold]Remote size:[/bold] {human_size(resolved.size_bytes)}")

    if result.probe is not None:
        transfer = result.probe.transfer
        console.print(f"[bold]Bytes inspected:[/bold] {human_size(transfer.received_bytes)}")
    elif api is not None and api.sufficient_for_prs_assessment:
        console.print(
            "[bold]Bytes inspected:[/bold] [green]0 — raw file probing was unnecessary[/green]"
        )

    if result.error and result.readiness is None:
        console.print()
        console.print(
            Panel(
                f"{result.error}\n\n[dim]Failure category: "
                f"{result.failure_category.value if result.failure_category else 'unknown'}[/dim]",
                title="Assessment failed",
                border_style="red",
            )
        )
        return

    if result.probe is not None and result.probe.header is not None:
        console.print()
        console.print("[bold]Detected header:[/bold]")
        console.print("  " + "  ".join(result.probe.header.raw_header))

    if result.readiness is not None:
        render_readiness(result.readiness)

    if show_mapping and result.probe is not None and result.probe.mapping is not None:
        render_mapping(result.probe.mapping)

    for note in result.notes:
        console.print(f"[dim]Note: {note}[/dim]")


def render_readiness(readiness: ReadinessAssessment) -> None:
    """Print the verdict with its supporting requirement checks."""
    style = _VERDICT_STYLE[readiness.verdict]
    console.print()
    console.print(f"[bold]PRS readiness:[/bold] [{style}]{readiness.verdict.value}[/{style}]")
    console.print(f"[dim]Evidence: {readiness.evidence_source}[/dim]")

    console.print()
    console.print("[bold]Required fields:[/bold]")
    for requirement in readiness.required:
        console.print(_requirement_line(requirement))

    if readiness.recommended:
        console.print()
        console.print("[bold]Recommended:[/bold]")
        for requirement in readiness.recommended:
            console.print(_requirement_line(requirement))

    if readiness.warnings:
        console.print()
        console.print("[bold yellow]Warnings:[/bold yellow]")
        for warning in readiness.warnings:
            console.print(f"  [yellow]![/yellow] {warning}")

    console.print()
    console.print("[bold]Decision:[/bold]")
    console.print(f"  {readiness.decision}")

    for note in readiness.notes:
        console.print(f"[dim]  {note}[/dim]")


def _requirement_line(requirement: Any) -> str:
    style = _STATUS_STYLE[requirement.status]
    symbol = requirement.status.symbol
    line = f"  [{style}]{symbol}[/{style}] {requirement.label}"
    if requirement.satisfied_by:
        line += f" [dim]<- {', '.join(requirement.satisfied_by)}[/dim]"
    elif requirement.status is RequirementStatus.MISSING and requirement.note:
        line += f" [dim]({requirement.note})[/dim]"
    return line


def _api_summary(api: Any) -> str:
    """One-line description of what the structured route achieved."""
    if api.sufficient_for_prs_assessment:
        return "[green]sufficient[/green] — no data bytes needed"
    if api.availability is ApiAvailability.DEPRECATED:
        return "[yellow]insufficient[/yellow] [dim](the Summary Statistics API is withdrawn)[/dim]"
    if api.availability is ApiAvailability.NOT_QUERIED:
        return "[dim]not queried[/dim]"
    if api.available:
        return "[yellow]insufficient[/yellow] [dim](metadata present but the column set is not guaranteed)[/dim]"
    return f"[yellow]insufficient[/yellow] [dim]({api.availability.value})[/dim]"


# ----------------------------------------------------------------------
# run / pilot comparison
# ----------------------------------------------------------------------


def render_candidate_table(
    results: list[AssessmentResult], *, title: str = "Candidate studies"
) -> None:
    """Ranked, transparent comparison table.

    Deliberately not a single opaque "best study" score: every column that
    contributed to the ordering is shown, so the reader can re-sort on the
    criterion they care about.
    """
    console.print()
    table = Table(title=title, header_style="bold cyan", box=None)
    table.add_column("GCST", style="bold")
    table.add_column("Trait", max_width=26, overflow="ellipsis")
    table.add_column("SSF status")
    table.add_column("API suff.", justify="center")
    table.add_column("File size", justify="right")
    table.add_column("Probed", justify="right")
    table.add_column("PRS", justify="center")
    table.add_column("N", justify="right")

    for result in results:
        study = result.study
        api = result.api_assessment
        ssf = api.ssf_metadata if api else None
        verdict = result.readiness.verdict if result.readiness else ReadinessVerdict.UNKNOWN
        probed = (
            human_size(result.probe.transfer.received_bytes) if result.probe is not None else "0 B"
        )
        table.add_row(
            study.study_accession if study else result.target[:18],
            (study.reported_trait if study else None) or "unknown",
            (ssf.ssf_status if ssf else "unknown"),
            _boolean(api.sufficient_for_prs_assessment if api else None),
            human_size(result.resolved_file.size_bytes if result.resolved_file else None),
            probed,
            Text(verdict.value, style=_VERDICT_STYLE[verdict]),
            _count(study.samples.total if study else None),
        )
    console.print(table)


def render_pilot_table(rows: list[dict[str, Any]]) -> None:
    """The comparison table that becomes the basis of the scientific evaluation."""
    console.print()
    table = Table(title="GWASPoker pilot comparison", header_style="bold cyan", box=None)
    for column in (
        "GCST",
        "Trait",
        "SSF/API status",
        "API avail.",
        "API suff.",
        "API bytes",
        "Raw file size",
        "Probe bytes",
        "Header",
        "PRS ready",
        "Full DL required",
        "GWASLab",
    ):
        table.add_column(column, overflow="fold")

    for row in rows:
        table.add_row(
            str(row.get("study_accession") or "—"),
            str(row.get("trait") or "unknown")[:24],
            str(row.get("ssf_status") or "unknown"),
            _boolean(row.get("api_available")),
            _boolean(row.get("api_sufficient")),
            human_size(row.get("api_bytes")),
            human_size(row.get("full_file_size")),
            human_size(row.get("probe_bytes")),
            _boolean(row.get("header_detected")),
            Text(str(row.get("predicted_prs_ready") or "UNKNOWN")),
            _boolean(row.get("full_download_required")),
            (
                _boolean(row.get("gwaslab_success"))
                if row.get("gwaslab_success") is not None
                else Text("not run", style="dim")
            ),
        )
    console.print(table)


def render_failures(failures: list[dict[str, Any]]) -> None:
    """Summarise classified failures at the end of a run."""
    if not failures:
        return
    console.print()
    table = Table(title="Failures", header_style="bold red", box=None)
    table.add_column("Stage")
    table.add_column("Category")
    table.add_column("Target", max_width=30, overflow="ellipsis")
    table.add_column("Message", max_width=60, overflow="ellipsis")
    for failure in failures:
        table.add_row(
            failure.get("stage", ""),
            failure.get("failure_category", ""),
            failure.get("study") or failure.get("url") or "—",
            failure.get("message", ""),
        )
    console.print(table)
