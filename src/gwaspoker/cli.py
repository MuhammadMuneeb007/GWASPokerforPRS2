"""The ``gwaspoker`` command-line interface.

Eight commands, built on Typer:

``search``     find studies for a phenotype
``probe``      inspect a remote file's header without downloading it
``assess``     structured-first PRS readiness verdict
``scan``       inspect a local file, a URL or an accession
``download``   fetch the complete file, verified
``extract``    decompress and normalize a downloaded file
``run``        search, assess and rank in one pass
``benchmark``  score predictions against curated ground truth

Every command supports ``-v`` / ``-vv`` / ``--quiet``. Commands that produce
results support ``--format`` and ``--output``, and the two are independent:
``--format`` chooses the representation, ``--output`` only chooses the
destination. Without ``--output`` a non-table format is written to stdout so it
can be piped.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from gwaspoker import __version__
from gwaspoker.config import (
    PROBE_SIZE_LADDER,
    GWASPokerConfig,
    configure_logging,
    load_config,
    set_config,
)
from gwaspoker.failures import FAILURES, GWASPokerError

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="gwaspoker",
    help=(
        "API-aware pre-download triage of GWAS summary statistics for PRS workflows.\n\n"
        "GWASPoker uses structured GWAS Catalog information first. Remote file probing "
        "is performed only when file-level inspection is required or explicitly requested."
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Options shared by several commands, defined once.
_VERBOSE = typer.Option(0, "-v", "--verbose", count=True, help="-v for INFO, -vv for DEBUG.")
_QUIET = typer.Option(False, "--quiet", "-q", help="Only report errors.")
_CONFIG = typer.Option(None, "--config", help="Path to a gwaspoker.toml or .yaml config file.")
_FORMAT = typer.Option("table", "--format", "-f", help="table, csv, json or html.")
_OUTPUT = typer.Option(None, "--output", "-o", help="Write results to this path.")
_PROVENANCE = typer.Option(None, "--provenance", help="Write a provenance JSON file.")
_FAILURE_LOG = typer.Option(None, "--failure-log", help="Append classified failures as JSON Lines.")
_PROBE_BYTES = typer.Option(
    None,
    "--probe-bytes",
    help=f"Bytes to inspect. Suggested: {', '.join(str(s) for s in PROBE_SIZE_LADDER)}.",
)
_HARMONISED = typer.Option(
    None, "--harmonised", help="auto (default), yes or no -- prefer the harmonised file."
)

#: Reported-trait substrings excluded from `search` by default.
#:
#: Gene-based burden results aggregate variants to a gene and are not
#: variant-level summary statistics, so they cannot produce PRS weights at all.
#: The Catalog labels their files `file_type: non-GWAS-SSF`. They are dropped by
#: default because they can never answer the question `search` is asked, but the
#: count dropped is always printed, and --no-default-excludes restores them.
DEFAULT_EXCLUDES: tuple[str, ...] = ("Gene-based burden",)


def _setup(
    verbose: int,
    quiet: bool,
    config_path: Optional[Path],
    **overrides,
) -> GWASPokerConfig:
    """Configure logging and build the effective configuration."""
    configure_logging(verbose, quiet)
    config = load_config(config_path, **{k: v for k, v in overrides.items() if v is not None})
    set_config(config)
    return config


def _validate_choice(value: Optional[str], allowed: tuple[str, ...], name: str) -> Optional[str]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered not in allowed:
        raise typer.BadParameter(f"{name} must be one of {', '.join(allowed)}", param_hint=name)
    return lowered


def _finish(failure_log: Optional[Path], *, show: bool = True) -> None:
    """Report and optionally persist the failures collected during the run."""
    if not FAILURES:
        return
    if show:
        from gwaspoker.reporting.console import console, render_failures

        render_failures(FAILURES.to_list())
        console.print(
            f"[dim]{len(FAILURES)} non-fatal failure(s). " "Re-run with -v for detail.[/dim]"
        )
    if failure_log is not None:
        FAILURES.write_jsonl(failure_log)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"gwaspoker {__version__}")
        raise typer.Exit


@app.callback()
def _main_callback(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version."
    ),
) -> None:
    """GWASPoker: decide before you download."""


# ======================================================================
# search
# ======================================================================


@app.command()
def search(
    trait: str = typer.Option(..., "--trait", "-t", help="Phenotype or trait to search for."),
    population: Optional[str] = typer.Option(
        None, "--population", "-p", help="Ancestry filter, e.g. European, East Asian."
    ),
    limit: int = typer.Option(25, "--limit", "-n", help="Maximum studies to return."),
    summary_stats_only: bool = typer.Option(
        False, "--sumstats-only", help="Only studies with full summary statistics."
    ),
    check_files: bool = typer.Option(
        True,
        "--check-files/--no-check-files",
        help="Look up File / SSF Meta / Harmonised / GWAS-SSF / PRS / Probe for "
        "each result. Two or three requests per study; --no-check-files leaves "
        "those columns as '?' and returns immediately.",
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        "-w",
        min=1,
        max=16,
        help="Threads for the file-availability stage (default 6). All workers "
        "share one process-wide rate limiter, so this overlaps latency rather "
        "than raising the request rate.",
    ),
    exclude: list[str] = typer.Option(
        None,
        "--exclude",
        help="Drop studies whose reported trait contains this text "
        "(case-insensitive, repeatable).",
    ),
    default_excludes: bool = typer.Option(
        True,
        "--default-excludes/--no-default-excludes",
        help=f"Also exclude {', '.join(repr(p) for p in DEFAULT_EXCLUDES)}, which "
        "are not variant-level summary statistics and cannot yield PRS weights.",
    ),
    llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Allow the ELECTRA fallback for unresolved sample counts."
    ),
    show_provenance: bool = typer.Option(
        False, "--show-provenance", help="Print where each sample count came from."
    ),
    output_format: str = _FORMAT,
    output: Optional[Path] = _OUTPUT,
    provenance: Optional[Path] = _PROVENANCE,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Find GWAS Catalog studies for a phenotype.

    Trait resolution goes through the Catalog's own ontology index, so results
    follow EFO annotation rather than string similarity against a downloaded
    spreadsheet.
    """
    config = _setup(
        verbose, quiet, config_path, enable_llm_fallback=llm or None, max_workers=workers
    )
    output_format = (
        _validate_choice(output_format, ("table", "csv", "json", "html"), "--format") or "table"
    )
    exclude = list(exclude or []) + (list(DEFAULT_EXCLUDES) if default_excludes else [])

    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    from gwaspoker.catalog.discovery import DiscoveryService
    from gwaspoker.provenance import build_provenance
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import csv as report_csv
    from gwaspoker.reporting import html as report_html
    from gwaspoker.reporting import json as report_json

    console = report_console.console
    patterns = list(exclude)
    started = time.perf_counter()

    with DiscoveryService(config, enable_llm=llm) as service:
        # --- Stage 1: metadata only. Fast, and finishes with a known count. ---
        try:
            with console.status("[dim]Resolving trait and retrieving studies...[/dim]") as status:
                status.update("[dim]Resolving trait...[/dim]")
                results = service.search(
                    trait,
                    population=population,
                    limit=limit,
                    summary_stats_only=summary_stats_only,
                    exclude=patterns,
                )
        except GWASPokerError as exc:
            console.print(f"[red]Search failed:[/red] {exc}")
            raise typer.Exit(1) from exc

        if not quiet:
            console.print(f"[dim]Retrieved {len(results)} study/studies.[/dim]")
            if service.last_excluded:
                console.print(
                    f"[dim]Excluded {service.last_excluded} matching "
                    f"{', '.join(repr(p) for p in patterns)} "
                    "(pass --no-default-excludes to keep them).[/dim]"
                )

        # --- Stage 2: file availability. Expensive, so parallel and measured. ---
        if check_files and results:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
                disable=quiet,
                transient=True,
            ) as bar:
                task = bar.add_task("Checking published files", total=len(results))

                def on_progress(done: int, total: int, _accession: str) -> None:
                    # Workers finish out of order, so a per-study label would
                    # flicker; the count and ETA are the useful signals.
                    bar.update(task, completed=done, total=total)

                service.check_files(results, workers=workers, progress=on_progress)

        record = build_provenance(
            config,
            command=f"gwaspoker search --trait {trait!r}",
            catalog_metadata=service.catalog.api_metadata(),
        )
        excluded = service.last_excluded

    elapsed = time.perf_counter() - started
    record.add_operation(
        "search",
        {
            "trait": trait,
            "population": population,
            "limit": limit,
            "results": len(results),
            "excluded": excluded,
            "exclude_patterns": patterns,
            "checked_files": check_files,
            "workers": workers or config.max_workers,
            "elapsed_seconds": round(elapsed, 2),
        },
    )
    record.failures = FAILURES.to_list()

    # `--format` selects the representation; `--output` only selects the
    # destination. Without --output a non-table format goes to stdout, which is
    # what makes `gwaspoker search --format json | jq` work.
    if output is None and output_format in ("json", "csv", "html"):
        _emit_to_stdout(output_format, results, record, trait, report_csv, report_html, report_json)
    else:
        report_console.render_search_results(results, trait=trait, population=population)
        if not quiet:
            console.print(f"[dim]Completed in {elapsed:.1f} s.[/dim]")
        if show_provenance and results:
            report_console.render_sample_provenance(results)

    if output is not None:
        if output_format == "csv":
            report_csv.write_search_csv(results, output)
        elif output_format == "json":
            report_json.write_json(
                report_json.search_payload(results), output, provenance=record, kind="search"
            )
        elif output_format == "html":
            report_html.write_report(
                output,
                title=f"GWASPoker search: {trait}",
                search_results=results,
                provenance=record,
            )
        else:
            report_csv.write_search_csv(results, output)
        report_console.console.print(f"[green]Wrote[/green] {output}")

    if provenance is not None:
        record.write(provenance)
        report_console.console.print(f"[green]Wrote provenance[/green] {provenance}")

    _finish(failure_log)


def _emit_to_stdout(
    output_format: str,
    results,
    record,
    trait: str,
    report_csv,
    report_html,
    report_json,
) -> None:
    """Write a non-table format to stdout when no --output was given.

    ``--format`` chooses the representation; ``--output`` chooses only the
    destination. The two are independent. Previously a missing ``--output``
    forced the table regardless of ``--format``, so ``--format json`` silently
    printed a table and could not be piped into ``jq``.
    """
    if output_format == "json":
        document = {
            "report_type": "search",
            "provenance": record.to_dict(),
            "results": report_json.search_payload(results),
        }
        sys.stdout.write(report_json.dumps(document) + "\n")
    elif output_format == "csv":
        sys.stdout.write(report_csv.render_search_csv(results))
    else:  # html
        sys.stdout.write(
            report_html.render_report(
                title=f"GWASPoker search: {trait}",
                search_results=results,
                provenance=record,
            )
        )


# ======================================================================
# probe
# ======================================================================


@app.command()
def probe(
    target: str = typer.Argument(..., help="A GCST accession or an http(s) URL."),
    probe_bytes: Optional[int] = _PROBE_BYTES,
    harmonised: Optional[str] = _HARMONISED,
    output_format: str = _FORMAT,
    output: Optional[Path] = _OUTPUT,
    provenance: Optional[Path] = _PROVENANCE,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Inspect a remote file's header without downloading it.

    At most ``--probe-bytes`` are transferred, using an HTTP Range request where
    the server supports one and a locally bounded stream where it does not.
    """
    harmonised = _validate_choice(harmonised, ("auto", "yes", "no"), "--harmonised")
    output_format = _validate_choice(output_format, ("table", "csv", "json"), "--format") or "table"
    config = _setup(
        verbose, quiet, config_path, probe_bytes=probe_bytes, prefer_harmonised=harmonised
    )

    from gwaspoker.catalog.discovery import DiscoveryService
    from gwaspoker.provenance import build_provenance
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import json as report_json

    with DiscoveryService(config) as service:
        try:
            result, study, resolved = service.probe(
                target, harmonised=harmonised, probe_bytes=probe_bytes
            )
        except GWASPokerError as exc:
            report_console.console.print(f"[red]Probe failed:[/red] {exc}")
            report_console.console.print(f"[dim]Failure category: {exc.category.value}[/dim]")
            _finish(failure_log)
            raise typer.Exit(1) from exc

    report_console.render_probe(result, resolved=resolved, study=study)

    record = build_provenance(config, command=f"gwaspoker probe {target}")
    record.add_operation("probe", report_json.probe_payload(result, study=study, resolved=resolved))
    record.failures = FAILURES.to_list()

    if output is not None:
        payload = report_json.probe_payload(result, study=study, resolved=resolved)
        if output_format == "csv" and result.mapping is not None:
            from gwaspoker.reporting import csv as report_csv

            report_csv.write_mapping_csv(result.mapping, output, source=target)
        else:
            report_json.write_json(payload, output, provenance=record, kind="probe")
        report_console.console.print(f"[green]Wrote[/green] {output}")
    if provenance is not None:
        record.write(provenance)

    _finish(failure_log)
    if not result.succeeded:
        raise typer.Exit(1)


# ======================================================================
# assess
# ======================================================================


@app.command()
def assess(
    targets: list[str] = typer.Argument(..., help="One or more GCST accessions or URLs."),
    target_workflow: str = typer.Option(
        "prs", "--target", help="Downstream workflow. Currently: prs."
    ),
    force_probe: bool = typer.Option(
        False,
        "--force-probe",
        help="Probe the file even when the structured metadata was sufficient. "
        "Needed to compare the two routes in a benchmark.",
    ),
    no_api: bool = typer.Option(
        False, "--no-api", help="Skip the structured route and go straight to the probe."
    ),
    probe_bytes: Optional[int] = _PROBE_BYTES,
    harmonised: Optional[str] = _HARMONISED,
    show_mapping: bool = typer.Option(False, "--show-mapping", help="Print the column mapping."),
    output_format: str = _FORMAT,
    output: Optional[Path] = _OUTPUT,
    provenance: Optional[Path] = _PROVENANCE,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Decide whether a study's summary statistics are usable for PRS.

    Structured metadata is consulted first. When a file declares GWAS-SSF
    conformance the verdict follows from the standard and no data bytes move at
    all; otherwise the file's header is probed.
    """
    harmonised = _validate_choice(harmonised, ("auto", "yes", "no"), "--harmonised")
    workflow = _validate_choice(target_workflow, ("prs",), "--target") or "prs"
    config = _setup(
        verbose, quiet, config_path, probe_bytes=probe_bytes, prefer_harmonised=harmonised
    )
    output_format = (
        _validate_choice(output_format, ("table", "csv", "json", "html"), "--format") or "table"
    )

    from gwaspoker.catalog.discovery import DiscoveryService
    from gwaspoker.provenance import assessment_provenance, build_provenance
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import csv as report_csv
    from gwaspoker.reporting import html as report_html
    from gwaspoker.reporting import json as report_json

    results = []
    with DiscoveryService(config) as service:
        for target in targets:
            result = service.assess(
                target,
                prs_target=workflow,
                harmonised=harmonised,
                force_probe=force_probe,
                probe_bytes=probe_bytes,
                skip_api=no_api,
            )
            results.append(result)
            if output_format == "table" or output is None:
                report_console.render_assessment(result, show_mapping=show_mapping)
        catalog_metadata = service.catalog.api_metadata()

    if len(results) > 1 and (output_format == "table" or output is None):
        report_console.render_candidate_table(results, title="Assessment summary")

    record = build_provenance(
        config,
        command=f"gwaspoker assess {' '.join(targets)}",
        catalog_metadata=catalog_metadata,
    )
    for result in results:
        record.add_operation("assess", assessment_provenance(result))
    record.failures = FAILURES.to_list()

    if output is not None:
        if output_format == "csv":
            report_csv.write_assessment_csv(results, output)
        elif output_format == "html":
            report_html.write_report(
                output, title="GWASPoker PRS assessment", assessments=results, provenance=record
            )
        else:
            report_json.write_json(
                report_json.assessment_payload(results),
                output,
                provenance=record,
                kind="assessment",
            )
        report_console.console.print(f"[green]Wrote[/green] {output}")
    if provenance is not None:
        record.write(provenance)
        report_console.console.print(f"[green]Wrote provenance[/green] {provenance}")

    _finish(failure_log)
    if all(r.readiness is None for r in results):
        raise typer.Exit(1)


# ======================================================================
# scan
# ======================================================================


@app.command()
def scan(
    target: str = typer.Argument(
        ..., help="A local file path, an http(s) URL or a GCST accession."
    ),
    probe_bytes: Optional[int] = _PROBE_BYTES,
    target_workflow: str = typer.Option("prs", "--target", help="Downstream workflow."),
    emit_code: bool = typer.Option(
        False, "--emit-code", help="Print a pandas snippet that renames columns to PRS symbols."
    ),
    output_format: str = _FORMAT,
    output: Optional[Path] = _OUTPUT,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Report format, compression, encoding, delimiter, header and PRS fields.

    A local file needs no network access at all.
    """
    config = _setup(verbose, quiet, config_path, probe_bytes=probe_bytes)
    output_format = _validate_choice(output_format, ("table", "csv", "json"), "--format") or "table"

    from gwaspoker.inputs import InputResolutionError, resolve_input
    from gwaspoker.probe.remote import RemoteProber
    from gwaspoker.readiness.prs import assess_from_mapping
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import csv as report_csv
    from gwaspoker.reporting import json as report_json

    study = None
    resolved = None

    try:
        resolved_input = resolve_input(target, allow_local=True)
    except InputResolutionError as exc:
        report_console.console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if resolved_input.normalisation_note:
        report_console.console.print(f"[dim]{resolved_input.normalisation_note}[/dim]")

    if resolved_input.is_local_file:
        prober = RemoteProber(config)
        result = prober.probe_local(resolved_input.path, probe_bytes=probe_bytes)
    else:
        from gwaspoker.catalog.discovery import DiscoveryService

        with DiscoveryService(config) as service:
            try:
                result, study, resolved = service.probe(target, probe_bytes=probe_bytes)
            except GWASPokerError as exc:
                report_console.console.print(f"[red]Scan failed:[/red] {exc}")
                raise typer.Exit(1) from exc

    report_console.console.print(f"[dim]Input type: {resolved_input.input_type.label}[/dim]")

    report_console.render_probe(result, resolved=resolved, study=study)

    readiness = None
    if result.mapping is not None:
        readiness = assess_from_mapping(
            result.mapping,
            target=_validate_choice(target_workflow, ("prs",), "--target") or "prs",
            evidence_source=("local_file" if resolved_input.is_local_file else "file_probe"),
            header=result.header.raw_header if result.header else (),
            validation=result.value_validation,
        )
        report_console.render_readiness(readiness)

    if emit_code and result.mapping is not None:
        report_console.console.print()
        report_console.console.print("[bold]Column mapping code:[/bold]")
        report_console.console.print(_emit_pandas_code(result, target))

    if output is not None:
        if output_format == "csv":
            report_csv.write_mapping_csv(result.mapping, output, source=target)
        else:
            payload = report_json.probe_payload(result, study=study, resolved=resolved)
            payload["readiness"] = readiness.to_dict() if readiness else None
            payload["input"] = resolved_input.to_dict()
            report_json.write_json(payload, output, kind="scan")
        report_console.console.print(f"[green]Wrote[/green] {output}")

    _finish(failure_log)
    if not result.succeeded:
        raise typer.Exit(1)


def _emit_pandas_code(result, source: str) -> str:
    """Generate the rename snippet locally and deterministically.

    v1 shipped this to HuggingChat, which required an account, sent study
    metadata to a third party, and returned prose that had to be parsed out of a
    chat response.
    """
    renames = {
        column.raw_name: column.prs_tool_symbol
        for column in result.mapping.columns
        if column.canonical_name != "unknown" and column.prs_tool_symbol
    }
    delimiter = result.header.delimiter if result.header else "\t"
    skip = result.header.header_row_index if result.header else 0
    unmapped = [c.raw_name for c in result.mapping.unresolved]

    lines = [
        "import pandas as pd",
        "",
        "df = pd.read_csv(",
        f"    {source!r},",
        f"    sep={delimiter!r},",
        f"    skiprows={skip},",
        f"    encoding={result.encoding or 'utf-8'!r},",
        ")",
        "",
        "df = df.rename(columns={",
    ]
    lines.extend(f"    {raw!r}: {symbol!r}," for raw, symbol in renames.items())
    lines.append("})")
    if unmapped:
        lines.append("")
        lines.append("# Columns GWASPoker could not map, left untouched for you to check:")
        lines.extend(f"#   {name}" for name in unmapped)
    return "\n".join(lines)


# ======================================================================
# download
# ======================================================================


@app.command()
def download(
    target: str = typer.Argument(..., help="A GCST accession or an http(s) URL."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-d", help="Directory to download into."
    ),
    harmonised: Optional[str] = _HARMONISED,
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing file."),
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Skip MD5 verification against the published checksum."
    ),
    gwaslab: bool = typer.Option(
        False, "--gwaslab", help="Hand the downloaded file to GWASLab afterwards."
    ),
    output: Optional[Path] = _OUTPUT,
    provenance: Optional[Path] = _PROVENANCE,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Download the complete summary-statistics file, with checksum verification."""
    harmonised = _validate_choice(harmonised, ("auto", "yes", "no"), "--harmonised")
    config = _setup(
        verbose,
        quiet,
        config_path,
        download_dir=output_dir,
        prefer_harmonised=harmonised,
        verify_checksum=False if no_verify else None,
    )

    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    from gwaspoker.download.downloader import SummaryStatisticsDownloader
    from gwaspoker.download.resolver import SummaryStatisticsResolver
    from gwaspoker.http import HttpClient
    from gwaspoker.processing.formats import human_size
    from gwaspoker.provenance import build_provenance
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import json as report_json

    destination = Path(output_dir) if output_dir else config.download_dir
    http = HttpClient(config)
    resolver = SummaryStatisticsResolver(config, http)
    downloader = SummaryStatisticsDownloader(config, http)
    resolved = None

    from gwaspoker.inputs import InputResolutionError, resolve_input

    try:
        try:
            resolved_input = resolve_input(target)
        except InputResolutionError as exc:
            report_console.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        if resolved_input.normalisation_note:
            report_console.console.print(f"[dim]{resolved_input.normalisation_note}[/dim]")
        report_console.console.print(f"[dim]Input type: {resolved_input.input_type.label}[/dim]")

        if resolved_input.is_direct_url:
            url, name, expected = resolved_input.url, None, None
        else:
            from gwaspoker.catalog.rest_api import GwasCatalogClient

            catalog = GwasCatalogClient(config, http)
            study = catalog.get_study(resolved_input.accession)
            resolved = resolver.resolve(
                resolved_input.accession,
                harmonised=harmonised or config.prefer_harmonised,
                location_hint=study.summary_statistics_location,
            )
            url, name = resolved.url, resolved.name
            expected = None
            if resolved.checksum_url and not no_verify:
                expected = resolver.fetch_expected_md5(resolved.checksum_url, resolved.name)
            report_console.console.print(f"[bold]Selected:[/bold] {resolved.name}")
            report_console.console.print(f"[dim]{resolved.selection_reason}[/dim]")
            report_console.console.print(f"[bold]Size:[/bold] {human_size(resolved.size_bytes)}")

        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=report_console.console,
            disable=quiet,
        ) as progress_bar:
            task = progress_bar.add_task(f"Downloading {name or 'file'}", total=None)

            def on_progress(written: int, total: Optional[int]) -> None:
                progress_bar.update(task, completed=written, total=total)

            result = downloader.download(
                url,
                destination,
                filename=name,
                expected_md5=expected,
                overwrite=overwrite,
                progress=on_progress,
            )

        if resolved is not None:
            result.sidecar_paths = downloader.download_sidecars(
                resolved, destination, overwrite=overwrite
            )
    finally:
        http.close()

    if result.skipped:
        report_console.console.print(
            f"[yellow]{result.notes[0] if result.notes else 'Skipped.'}[/yellow]"
        )
    if result.error:
        report_console.console.print(f"[red]Download failed:[/red] {result.error}")
        _finish(failure_log)
        raise typer.Exit(1)

    report_console.console.print(
        f"[green]Downloaded[/green] {result.path} "
        f"({human_size(result.bytes_downloaded or result.total_bytes)}"
        + (f", {result.throughput_mb_s:.1f} MB/s" if result.throughput_mb_s else "")
        + ")"
    )
    if result.checksum_verified is True:
        report_console.console.print("[green]MD5 verified[/green] against the published checksum.")
    elif result.checksum_expected is None:
        report_console.console.print(
            "[dim]No published checksum was available for this file; integrity unverified.[/dim]"
        )
    for sidecar in result.sidecar_paths:
        report_console.console.print(f"[dim]Also saved {sidecar.name}[/dim]")

    gwaslab_result = None
    if gwaslab and result.path is not None:
        gwaslab_result = _run_gwaslab(result.path, report_console.console)

    record = build_provenance(config, command=f"gwaspoker download {target}")
    record.add_operation(
        "download",
        {
            "input": resolved_input.to_dict(),
            "download": result.to_dict(),
            "resolved_file": resolved.to_dict() if resolved else None,
            "gwaslab": gwaslab_result.to_dict() if gwaslab_result else None,
        },
    )
    record.failures = FAILURES.to_list()
    if output is not None:
        report_json.write_json(record.operations[-1], output, provenance=record, kind="download")
    if provenance is not None:
        record.write(provenance)

    _finish(failure_log)


def _run_gwaslab(path: Path, console, mapping=None):
    """Hand a downloaded file to GWASLab, reporting availability honestly."""
    from gwaspoker.integrations.gwaslab import run_gwaslab

    console.print()
    console.print("[bold]GWASLab[/bold]")
    result = run_gwaslab(path, mapping=mapping)
    if not result.available:
        console.print(f"[yellow]{result.message}[/yellow]")
    elif result.succeeded:
        console.print(f"[green]{result.message}[/green]")
    else:
        console.print(f"[red]GWASLab failed:[/red] {result.error}")
    return result


# ======================================================================
# extract
# ======================================================================


@app.command()
def extract(
    path: Path = typer.Argument(..., help="A downloaded summary-statistics file."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path."),
    delimiter: str = typer.Option("\t", "--delimiter", help="Output delimiter."),
    max_rows: Optional[int] = typer.Option(None, "--max-rows", help="Read at most this many rows."),
    rename: bool = typer.Option(False, "--rename", help="Rename columns to canonical concepts."),
    rename_symbols: bool = typer.Option(
        False, "--rename-symbols", help="Rename columns to PRS tool symbols (CHR, BP, A1, ...)."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    report_path: Optional[Path] = typer.Option(
        None, "--report", help="Write the transformation report as JSON."
    ),
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Decompress and normalize a downloaded file into a clean table.

    Only declared transformations are applied, and every one is reported.
    GWASPoker never rewrites data values to make a parser succeed.
    """
    _setup(verbose, quiet, config_path)

    from gwaspoker.processing.extract import Extractor
    from gwaspoker.processing.formats import human_size
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import json as report_json

    extractor = Extractor()
    result = extractor.extract(
        path,
        output_path=output,
        output_delimiter=delimiter,
        max_rows=max_rows,
        rename_columns=rename,
        rename_to_symbols=rename_symbols,
        overwrite=overwrite,
    )

    console = report_console.console
    if result.error:
        console.print(f"[red]Extraction failed:[/red] {result.error}")
        if result.failure_category:
            console.print(f"[dim]Failure category: {result.failure_category.value}[/dim]")
        _finish(failure_log)
        raise typer.Exit(1)

    console.print(f"[green]Wrote[/green] {result.output_path}")
    console.print(
        f"[dim]{result.rows_written:,} rows, {len(result.columns)} columns, "
        f"{human_size(result.output_path.stat().st_size)}[/dim]"
    )
    console.print(
        f"[dim]Source: {result.compression.value} compression, "
        f"{result.encoding} encoding, delimiter {result.delimiter!r}, "
        f"header at row {result.header_row_index}.[/dim]"
    )

    console.print()
    console.print("[bold]Transformations applied:[/bold]")
    if not result.report.transformations:
        console.print("  [dim]none beyond delimiter interpretation[/dim]")
    for transformation in result.report.transformations:
        console.print(f"  • {transformation.name}: {transformation.description}")
        if transformation.rows_affected:
            console.print(f"    [dim]{transformation.rows_affected:,} cell(s) affected[/dim]")

    if verbose and result.report.declined:
        console.print()
        console.print("[bold]Transformations deliberately not applied:[/bold]")
        for reason in result.report.declined:
            console.print(f"  [dim]• {reason}[/dim]")

    if result.mapping is not None:
        report_console.render_mapping(result.mapping)

    if report_path is not None:
        report_json.write_json(result.to_dict(), report_path, kind="extract")
        console.print(f"[green]Wrote report[/green] {report_path}")

    _finish(failure_log)


# ======================================================================
# run
# ======================================================================


@app.command()
def run(
    trait: str = typer.Option(..., "--trait", "-t", help="Phenotype to search for."),
    population: Optional[str] = typer.Option(None, "--population", "-p", help="Ancestry filter."),
    target_workflow: str = typer.Option("prs", "--target", help="Downstream workflow."),
    limit: int = typer.Option(10, "--limit", "-n", help="Studies to assess."),
    force_probe: bool = typer.Option(
        False, "--force-probe", help="Probe even when the API sufficed."
    ),
    do_download: bool = typer.Option(
        False, "--download", help="Download the top-ranked study. Files can be many gigabytes."
    ),
    gwaslab: bool = typer.Option(False, "--gwaslab", help="Run GWASLab on any downloaded file."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-d", help="Download directory."
    ),
    probe_bytes: Optional[int] = _PROBE_BYTES,
    harmonised: Optional[str] = _HARMONISED,
    output_format: str = _FORMAT,
    output: Optional[Path] = _OUTPUT,
    provenance: Optional[Path] = _PROVENANCE,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Search, assess and rank candidate studies end to end.

    Nothing is downloaded unless ``--download`` is given.
    """
    harmonised = _validate_choice(harmonised, ("auto", "yes", "no"), "--harmonised")
    workflow = _validate_choice(target_workflow, ("prs",), "--target") or "prs"
    config = _setup(
        verbose, quiet, config_path, probe_bytes=probe_bytes, prefer_harmonised=harmonised
    )
    output_format = (
        _validate_choice(output_format, ("table", "csv", "json", "html"), "--format") or "table"
    )

    from gwaspoker.catalog.discovery import DiscoveryService
    from gwaspoker.provenance import assessment_provenance, build_provenance
    from gwaspoker.readiness.models import ReadinessVerdict
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import csv as report_csv
    from gwaspoker.reporting import html as report_html
    from gwaspoker.reporting import json as report_json

    console = report_console.console

    with DiscoveryService(config) as service:
        search_results = service.search(
            trait, population=population, limit=limit, summary_stats_only=True
        )
        report_console.render_search_results(search_results, trait=trait, population=population)

        if not search_results:
            _finish(failure_log)
            raise typer.Exit(1)

        console.print()
        console.print(f"[bold]Assessing {len(search_results)} candidate study/studies...[/bold]")

        assessments = []
        for result in search_results:
            accession = result.study.study_accession
            console.print(f"[dim]  {accession}...[/dim]")
            assessments.append(
                service.assess(
                    accession,
                    prs_target=workflow,
                    harmonised=harmonised,
                    force_probe=force_probe,
                    probe_bytes=probe_bytes,
                )
            )
        catalog_metadata = service.catalog.api_metadata()

    ranked = _rank(assessments)
    report_console.render_candidate_table(ranked, title=f"Ranked candidates for {trait}")
    console.print(
        "[dim]Ranked by: PRS verdict, then whether the structured route sufficed, then "
        "sample size, then publication year. Every ranking input is a column above.[/dim]"
    )

    total_bytes = sum(a.bytes_transferred for a in ranked)
    total_size = sum(a.resolved_file.size_bytes or 0 for a in ranked if a.resolved_file is not None)
    if total_size:
        from gwaspoker.processing.formats import human_size

        console.print(
            f"[dim]Transferred {human_size(total_bytes)} to triage "
            f"{human_size(total_size)} of published summary statistics "
            f"({(1 - total_bytes / total_size) * 100:.4f}% avoided).[/dim]"
        )

    if do_download and ranked:
        best = ranked[0]
        if best.readiness and best.readiness.verdict is ReadinessVerdict.READY:
            console.print()
            console.print(f"[bold]Downloading {best.study.study_accession}[/bold]")
            _download_assessment(best, output_dir or config.download_dir, config, gwaslab, console)
        else:
            console.print(
                "[yellow]Top-ranked study is not READY; skipping the download. "
                "Pass an explicit accession to `gwaspoker download` to override.[/yellow]"
            )

    record = build_provenance(
        config,
        command=f"gwaspoker run --trait {trait!r}",
        catalog_metadata=catalog_metadata,
    )
    for assessment in ranked:
        record.add_operation("assess", assessment_provenance(assessment))
    record.failures = FAILURES.to_list()

    if output is not None:
        if output_format == "csv":
            report_csv.write_assessment_csv(ranked, output)
        elif output_format == "html":
            report_html.write_report(
                output,
                title=f"GWASPoker report: {trait}",
                search_results=search_results,
                assessments=ranked,
                provenance=record,
            )
        else:
            report_json.write_json(
                report_json.assessment_payload(ranked), output, provenance=record, kind="run"
            )
        console.print(f"[green]Wrote[/green] {output}")
    if provenance is not None:
        record.write(provenance)

    _finish(failure_log)


def _rank(assessments: list) -> list:
    """Rank candidates on transparent, individually visible criteria."""
    from gwaspoker.readiness.models import ReadinessVerdict

    order = {
        ReadinessVerdict.READY: 3,
        ReadinessVerdict.PARTIAL: 2,
        ReadinessVerdict.NOT_READY: 1,
        ReadinessVerdict.UNKNOWN: 0,
    }
    return sorted(
        assessments,
        key=lambda a: (
            order[a.readiness.verdict] if a.readiness else 0,
            1 if (a.api_assessment and a.api_assessment.sufficient_for_prs_assessment) else 0,
            (a.study.samples.implied_total() or 0) if a.study else 0,
            (a.study.study_year or 0) if a.study else 0,
        ),
        reverse=True,
    )


def _download_assessment(assessment, destination: Path, config, gwaslab: bool, console) -> None:
    """Download the file behind an assessment that has already been resolved."""
    from gwaspoker.download.downloader import SummaryStatisticsDownloader
    from gwaspoker.download.resolver import SummaryStatisticsResolver
    from gwaspoker.http import HttpClient
    from gwaspoker.processing.formats import human_size

    resolved = assessment.resolved_file
    if resolved is None:
        console.print("[red]No file was resolved for this study.[/red]")
        return

    http = HttpClient(config)
    try:
        resolver = SummaryStatisticsResolver(config, http)
        downloader = SummaryStatisticsDownloader(config, http)
        expected = resolved.expected_md5
        if expected is None and resolved.checksum_url:
            expected = resolver.fetch_expected_md5(resolved.checksum_url, resolved.name)
        result = downloader.download(
            resolved.url, destination, filename=resolved.name, expected_md5=expected
        )
        result.sidecar_paths = downloader.download_sidecars(resolved, destination)
    finally:
        http.close()

    if result.error:
        console.print(f"[red]Download failed:[/red] {result.error}")
        return
    console.print(
        f"[green]Downloaded[/green] {result.path} ({human_size(result.bytes_downloaded)})"
    )
    if gwaslab and result.path is not None:
        mapping = assessment.probe.mapping if assessment.probe else None
        _run_gwaslab(result.path, console, mapping=mapping)


# ======================================================================
# benchmark
# ======================================================================


@app.command()
def benchmark(
    manifest_path: Path = typer.Argument(..., help="Benchmark manifest CSV."),
    run_predictions_flag: bool = typer.Option(
        False, "--run", help="Run GWASPoker to fill the prediction columns first."
    ),
    probe_size_sweep: bool = typer.Option(
        False,
        "--probe-size-sweep",
        help="Probe each study at 64 KB, 128 KB, 256 KB, 512 KB and 1 MB.",
    ),
    stratify: Optional[str] = typer.Option(
        None,
        "--stratify",
        help="Comma-separated: ssf_status, api_coverage, file_format, compression, source.",
    ),
    probe_bytes: Optional[int] = _PROBE_BYTES,
    harmonised: Optional[str] = _HARMONISED,
    update_manifest: Optional[Path] = typer.Option(
        None, "--update-manifest", help="Write the manifest back with predictions filled in."
    ),
    output: Optional[Path] = _OUTPUT,
    failure_log: Optional[Path] = _FAILURE_LOG,
    config_path: Optional[Path] = _CONFIG,
    verbose: int = _VERBOSE,
    quiet: bool = _QUIET,
) -> None:
    """Score GWASPoker's predictions against externally curated ground truth.

    Ground-truth columns are never written by GWASPoker. Scoring a parser
    against labels it produced itself measures nothing, and the evaluator warns
    when a manifest looks like that has happened.
    """
    harmonised = _validate_choice(harmonised, ("auto", "yes", "no"), "--harmonised")
    config = _setup(
        verbose, quiet, config_path, probe_bytes=probe_bytes, prefer_harmonised=harmonised
    )

    from gwaspoker.benchmark.evaluate import (
        evaluate_manifest,
        probe_size_experiment,
        run_predictions,
        summarize_probe_sizes,
    )
    from gwaspoker.benchmark.manifest import read_manifest, write_manifest
    from gwaspoker.reporting import console as report_console
    from gwaspoker.reporting import json as report_json

    console = report_console.console

    try:
        rows = read_manifest(manifest_path)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"Read [bold]{len(rows)}[/bold] manifest row(s) from {manifest_path}")

    if run_predictions_flag:
        console.print("Running GWASPoker over the manifest...")

        def on_progress(index: int, total: int, target: str) -> None:
            console.print(f"[dim]  [{index}/{total}] {target}[/dim]")

        rows = run_predictions(
            rows,
            config=config,
            probe_bytes=probe_bytes,
            harmonised=harmonised or "auto",
            progress=on_progress,
        )
        # Writing back is opt-in. The manifest holds hand-curated ground truth
        # -- the one thing in this project GWASPoker must never write -- and
        # overwriting the input by default puts hours of manual curation one
        # stray `--run` away from being rewritten in place. Predictions stay in
        # memory for scoring unless a destination is named.
        if update_manifest is not None:
            write_manifest(rows, update_manifest)
            console.print(f"[green]Wrote predictions to[/green] {update_manifest}")
        else:
            console.print(
                "[dim]Predictions were not written back; "
                "pass --update-manifest PATH to save them.[/dim]"
            )

    sweep_summary = None
    if probe_size_sweep:
        targets = [row.accession or str(row.get("remote_file_url")) for row in rows]
        targets = [t for t in targets if t and t != "None"]
        console.print(
            f"Probe-size sweep over {len(targets)} target(s) at "
            f"{', '.join(str(s) for s in PROBE_SIZE_LADDER)} bytes..."
        )

        def on_probe(target: str, size: int) -> None:
            console.print(f"[dim]  {target} at {size:,} bytes[/dim]")

        outcomes = probe_size_experiment(
            targets, config=config, harmonised=harmonised or "auto", progress=on_probe
        )
        sweep_summary = summarize_probe_sizes(outcomes)
        _render_probe_sweep(console, sweep_summary)

    strata = tuple(s.strip() for s in stratify.split(",")) if stratify else ()
    report = evaluate_manifest(rows, stratify_by=strata)

    _render_evaluation(console, report)

    if output is not None:
        payload = report.to_dict()
        if sweep_summary is not None:
            payload["probe_size_experiment"] = sweep_summary
        report_json.write_json(payload, output, kind="benchmark")
        console.print(f"[green]Wrote[/green] {output}")

    _finish(failure_log)


def _render_evaluation(console, report) -> None:
    from rich.table import Table

    if report.validation_problems:
        console.print()
        for problem in report.validation_problems:
            console.print(f"[yellow]! {problem}[/yellow]")

    console.print()
    console.print(
        f"[bold]{report.rows_with_ground_truth}[/bold] of {report.rows_total} row(s) "
        "carry externally curated ground truth."
    )

    if report.rows_with_ground_truth:
        table = Table(title="Header detection", header_style="bold cyan", box=None)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_column("n", justify="right")
        for name, key in (
            ("Header row accuracy", "header_row_accuracy"),
            ("Exact ordered header match", "exact_ordered_header_match"),
            ("Header detected", "header_detected_rate"),
        ):
            entry = report.header_detection.get(key, {})
            table.add_row(name, _fmt_rate(entry.get("rate")), str(entry.get("n", 0)))
        console.print(table)

        column = report.column_level
        if column.get("files_compared"):
            table = Table(title="Column-level agreement", header_style="bold cyan", box=None)
            table.add_column("Metric")
            table.add_column("Value", justify="right")
            for name, key in (("Precision", "precision"), ("Recall", "recall"), ("F1", "f1")):
                table.add_row(name, _fmt_rate(column.get(key)))
            console.print(table)

        mapping = report.canonical_mapping
        if mapping.get("columns_compared"):
            console.print(
                f"\nCanonical mapping accuracy: "
                f"[bold]{_fmt_rate(mapping.get('accuracy'))}[/bold] "
                f"over {mapping['columns_compared']} column(s); "
                f"{_fmt_rate(mapping.get('unknown_rate'))} predicted as unknown."
            )

        strict = report.prs_readiness.get("strict_ready_positive", {})
        if strict.get("n"):
            table = Table(
                title="PRS readiness (READY = positive)", header_style="bold cyan", box=None
            )
            table.add_column("Metric")
            table.add_column("Value", justify="right")
            for name, key in (
                ("Sensitivity / recall", "sensitivity"),
                ("Specificity", "specificity"),
                ("Precision", "precision"),
                ("F1", "f1"),
                ("Accuracy", "accuracy"),
                ("False positive rate", "false_positive_rate"),
                ("False negative rate", "false_negative_rate"),
            ):
                table.add_row(name, _fmt_rate(strict.get(key)))
            table.add_row("n", str(strict["n"]))
            console.print(table)

    transfer = report.transfer
    if transfer.get("files"):
        console.print()
        console.print("[bold]Transfer[/bold]")
        console.print(
            f"  {transfer['total_bytes_transferred']:,} bytes transferred to triage "
            f"{transfer['total_full_file_bytes']:,} bytes of published data."
        )
        reduction = transfer.get("percentage_transfer_reduction")
        if reduction is not None:
            console.print(f"  Transfer reduction: [bold]{reduction:.4f}%[/bold]")
        if transfer.get("median_probe_latency_seconds") is not None:
            console.print(
                f"  Median probe latency: {transfer['median_probe_latency_seconds']:.2f} s"
            )

    coverage = report.coverage
    console.print()
    console.print("[bold]Coverage[/bold]")
    console.print(
        f"  API coverage: {_fmt_rate(coverage['api_coverage'].get('rate'))} "
        f"(n={coverage['api_coverage'].get('n', 0)})"
    )
    console.print(
        f"  API sufficiency: {_fmt_rate(coverage['api_sufficiency_rate'].get('rate'))} "
        f"(n={coverage['api_sufficiency_rate'].get('n', 0)})"
    )
    console.print(
        f"  Failure rate: {_fmt_rate(coverage['failure_rate'].get('rate'))} "
        f"(n={coverage['failure_rate'].get('n', 0)})"
    )

    if report.failures:
        console.print()
        console.print("[bold]Failure categories[/bold]")
        for category, count in report.failures.items():
            console.print(f"  {category}: {count}")

    for key, buckets in report.strata.items():
        console.print()
        console.print(f"[bold]Stratified by {key}[/bold]")
        for name, summary in buckets.items():
            exact = summary["header_detection"]["exact_ordered_header_match"]
            console.print(
                f"  {name}: n={summary['n']}, labelled={summary['n_with_ground_truth']}, "
                f"exact header match={_fmt_rate(exact.get('rate'))}"
            )


def _render_probe_sweep(console, summary: dict) -> None:
    from rich.table import Table

    console.print()
    table = Table(title="Probe-size experiment", header_style="bold cyan", box=None)
    table.add_column("Probe size", justify="right")
    table.add_column("Header detected", justify="right")
    table.add_column("Agrees with largest probe", justify="right")
    table.add_column("Mean bytes received", justify="right")

    for size in summary["sizes"]:
        detection = summary["detection_rate_by_size"].get(str(size), {})
        agreement = summary["agreement_with_largest_probe_by_size"].get(str(size), {})
        table.add_row(
            f"{size:,}",
            f"{_fmt_rate(detection.get('rate'))} ({detection.get('hits', 0)}/{detection.get('n', 0)})",
            f"{_fmt_rate(agreement.get('rate'))} ({agreement.get('hits', 0)}/{agreement.get('n', 0)})",
            f"{summary['mean_bytes_by_size'].get(str(size), 0):,.0f}",
        )
    console.print(table)
    console.print(f"[dim]{summary['note']}[/dim]")


def _fmt_rate(value: Optional[float]) -> str:
    """Format a rate, distinguishing 'undefined' from 'zero'."""
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def main() -> None:
    """Console-script entry point.

    Commands catch :class:`GWASPokerError` where they can add context ("Scan
    failed: ..."). This is the backstop for the paths that cannot: a study with
    no summary-statistics directory, an unreadable archive, a withdrawn
    endpoint. Those are *classified, expected* outcomes, and printing a Python
    traceback for one misrepresents an answer as a crash -- and buries the
    failure category under thirty lines of frames.

    A genuinely unexpected exception still gets its traceback, because that is
    a bug and the frames are the point.
    """
    from gwaspoker.failures import GWASPokerError

    try:
        app()
    except KeyboardInterrupt:
        typer.echo("\nInterrupted.", err=True)
        sys.exit(130)
    except GWASPokerError as exc:
        from gwaspoker.reporting import console as report_console

        category = exc.category.value if exc.category else "unknown"
        report_console.console.print(f"[red]Error:[/red] {exc}")
        report_console.console.print(f"[dim]Failure category: {category}[/dim]")
        sys.exit(1)


if __name__ == "__main__":
    main()
