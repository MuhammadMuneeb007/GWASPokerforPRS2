"""Configuration: defaults, file, environment variables, CLI overrides.

Precedence, lowest to highest:

1. the defaults on :class:`GWASPokerConfig`;
2. a config file -- ``--config PATH``, else ``./gwaspoker.toml``,
   ``./gwaspoker.yaml``, else ``~/.config/gwaspoker/config.toml``;
3. ``GWASPOKER_*`` environment variables;
4. explicit CLI options.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Probe sizes offered by the CLI and by the benchmark's probe-size experiment.
PROBE_SIZE_LADDER: tuple[int, ...] = (65_536, 131_072, 262_144, 524_288, 1_048_576)

#: Default bounded probe size. 256 KB is a *starting point*, not a validated
#: optimum -- ``gwaspoker benchmark --probe-size-sweep`` exists precisely to
#: determine how many bytes header detection actually needs.
DEFAULT_PROBE_BYTES = 262_144

_ENV_PREFIX = "GWASPOKER_"


@dataclass(frozen=True)
class GWASPokerConfig:
    """Runtime configuration for one GWASPoker invocation."""

    # --- Networking -----------------------------------------------------
    request_timeout: float = 60.0
    connect_timeout: float = 15.0
    max_retries: int = 3
    retry_backoff: float = 0.5
    user_agent: str = "gwaspoker/2.0.0 (+https://github.com/muneebsiddique/gwaspoker)"
    #: Requests started per second, across the whole process.
    #:
    #: The GWAS Catalog documents 15 queries/second for REST API v2. That figure
    #: is NOT documented for ftp.ebi.ac.uk, which is where the file-availability
    #: checks actually go, so this default is deliberately conservative and is
    #: not derived from the v2 limit.
    max_requests_per_second: float = 8.0

    #: Threads used for the parallel file-availability checks in `search`.
    #: Every worker still passes through the shared rate limiter above, so this
    #: controls concurrency, not throughput.
    max_workers: int = 6

    # --- API endpoints ---------------------------------------------------
    rest_api_v2_base: str = "https://www.ebi.ac.uk/gwas/rest/api/v2"
    rest_api_v1_base: str = "https://www.ebi.ac.uk/gwas/rest/api"
    solr_search_base: str = "https://www.ebi.ac.uk/gwas/api/search"
    #: Withdrawn upstream (HTTP 410). Retained so its status can be *measured*
    #: rather than assumed -- see docs/API_SOURCES.md.
    sumstats_api_base: str = "https://www.ebi.ac.uk/gwas/summary-statistics/api"
    ftp_base: str = "https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"
    prefer_api_version: str = "auto"  # auto | v2 | v1

    # --- Discovery -------------------------------------------------------
    api_page_size: int = 20
    default_search_limit: int = 25

    # --- Probing ---------------------------------------------------------
    probe_bytes: int = DEFAULT_PROBE_BYTES
    max_header_scan_lines: int = 200
    prefer_harmonised: str = "auto"  # auto | yes | no

    # --- Sample-size extraction -------------------------------------------
    enable_llm_fallback: bool = False
    llm_model: str = "ahotrod/electra_large_discriminator_squad2_512"
    llm_device: str = "auto"

    # --- Download ---------------------------------------------------------
    download_dir: Path = field(default_factory=lambda: Path.cwd() / "downloads")
    verify_checksum: bool = True
    download_chunk_bytes: int = 1_048_576
    allow_resume: bool = True

    # --- Reporting ---------------------------------------------------------
    provenance_path: Optional[Path] = None
    failure_log_path: Optional[Path] = None

    def with_overrides(self, **overrides: Any) -> GWASPokerConfig:
        """Return a copy with non-``None`` overrides applied."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(clean) - {f.name for f in fields(self)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        return replace(self, **clean)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out


_CONFIG_FILENAMES = ("gwaspoker.toml", "gwaspoker.yaml", "gwaspoker.yml")


def _coerce(field_name: str, raw: Any, current: Any) -> Any:
    """Coerce a string from a file or environment into the field's type."""
    if isinstance(current, Path) or field_name.endswith(("_dir", "_path")):
        return Path(str(raw)).expanduser()
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(str(raw).replace("_", ""))
    if isinstance(current, float):
        return float(raw)
    return str(raw)


def _load_config_file(path: Path) -> dict[str, Any]:
    """Read a TOML or YAML config file into a flat mapping."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                logger.warning(
                    "Cannot read %s: TOML support needs Python 3.11+ or the 'tomli' "
                    "package. Use a .yaml config file instead.",
                    path,
                )
                return {}
        data = tomllib.loads(text)
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: top level is not a mapping.", path)
        return {}
    # Accept either a flat mapping or a [gwaspoker] / [tool.gwaspoker] table.
    if "gwaspoker" in data and isinstance(data["gwaspoker"], dict):
        data = data["gwaspoker"]
    elif isinstance(data.get("tool"), dict) and isinstance(data["tool"].get("gwaspoker"), dict):
        data = data["tool"]["gwaspoker"]
    return data


def _discover_config_file() -> Optional[Path]:
    for name in _CONFIG_FILENAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    home = Path.home() / ".config" / "gwaspoker"
    for name in _CONFIG_FILENAMES:
        candidate = home / name
        if candidate.is_file():
            return candidate
    return None


def load_config(
    config_path: Optional[Path] = None,
    *,
    env: Optional[dict[str, str]] = None,
    **overrides: Any,
) -> GWASPokerConfig:
    """Build a configuration from defaults, file, environment and overrides."""
    config = GWASPokerConfig()
    known = {f.name: getattr(config, f.name) for f in fields(config)}

    path = Path(config_path) if config_path else _discover_config_file()
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        file_values: dict[str, Any] = {}
        for key, raw in _load_config_file(path).items():
            if key not in known:
                logger.warning("Ignoring unknown key %r in %s", key, path)
                continue
            file_values[key] = _coerce(key, raw, known[key])
        if file_values:
            config = config.with_overrides(**file_values)
            logger.debug("Loaded %d setting(s) from %s", len(file_values), path)

    environ = os.environ if env is None else env
    env_values: dict[str, Any] = {}
    for key, current in known.items():
        env_name = _ENV_PREFIX + key.upper()
        if env_name in environ:
            env_values[key] = _coerce(key, environ[env_name], current)
    if env_values:
        config = config.with_overrides(**env_values)
        logger.debug("Applied %d setting(s) from environment", len(env_values))

    return config.with_overrides(**overrides)


_ACTIVE: Optional[GWASPokerConfig] = None


def set_config(config: GWASPokerConfig) -> None:
    """Install the configuration for the current process."""
    global _ACTIVE
    _ACTIVE = config


def get_config() -> GWASPokerConfig:
    """Return the active configuration, loading defaults on first use."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_config()
    return _ACTIVE


def configure_logging(verbosity: int = 0, quiet: bool = False) -> None:
    """Set up logging. ``-v`` gives INFO, ``-vv`` DEBUG, ``--quiet`` ERROR only."""
    if quiet:
        level = logging.ERROR
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    from rich.logging import RichHandler

    root = logging.getLogger("gwaspoker")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = RichHandler(rich_tracebacks=False, show_path=level <= logging.DEBUG, show_time=False)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False

    # Never let urllib3 connection chatter into default output.
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))
