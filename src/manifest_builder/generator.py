# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Manifest generation orchestration."""

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.config import (
    ManifestConfig,
)
from manifest_builder.helm import ChartCacheStats
from manifest_builder.output import (
    dump_yaml,
)

logger = logging.getLogger(__name__)


def plural(num: int, plural_form: str = "s") -> str:
    return plural_form if num != 1 else ""


class ManifestError(Exception):
    """Raised when manifest generation fails for a specific config."""

    def __init__(self, config_name: str, cause: Exception) -> None:
        self.config_name = config_name
        self.cause = cause
        super().__init__(f"{type(cause).__name__}: {cause}")


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with a formatter for console output.

    Args:
        verbose: If True, set log level to DEBUG; otherwise INFO
    """
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)


@dataclass(frozen=True)
class _GenerationJob:
    # Each job pairs a block with one of its own configs, an invariant
    # established by _collect_generation_jobs().
    block: "ConfigBlock[Any]"
    config: ManifestConfig


def _collect_generation_jobs(
    blocks: "Sequence[ConfigBlock[Any]]",
) -> list[_GenerationJob]:
    """Pair each loaded config with exactly one registered block."""
    all_configs = tuple(config for block in blocks for config in block.iter_configs())
    seen_config_ids: set[int] = set()
    jobs: list[_GenerationJob] = []

    for block in blocks:
        for config in block.iter_configs():
            config_id = id(config)
            if config_id in seen_config_ids:
                raise ValueError(
                    f"Multiple config blocks selected '{config.name}' "
                    f"({config.namespace})"
                )
            seen_config_ids.add(config_id)
            jobs.append(_GenerationJob(block=block, config=config))

    missing = [config for config in all_configs if id(config) not in seen_config_ids]
    if missing:
        details = ", ".join(f"{config.name} ({config.namespace})" for config in missing)
        raise ValueError(f"No config block registered for: {details}")

    return jobs


def generate_manifests(
    blocks: "Sequence[ConfigBlock[Any]]",
    output_dir: Path,
    repo_root: Path,
    *,
    images: dict[str, str] | None = None,
    charts_dir: Path | None = None,
    verbose: bool = False,
    owned_namespaces: set[str] | None = None,
    managed_namespaces: set[str] | None = None,
    cleanup: bool = True,
) -> set[Path]:
    """
    Generate manifests for all configured apps.

    Args:
        blocks: Loaded config blocks
        output_dir: Directory to write generated manifests
        repo_root: Repository root for resolving relative paths
        images: Dict mapping image variable names to image references for template rendering
        charts_dir: Directory for caching pulled charts (default: repo_root/.charts)
        verbose: If True, log detailed output
        owned_namespaces: Namespaces owned by other services/pipelines. Files
            in these namespace directories are not cleaned up, and generation
            fails if any output would land in one of them.
        managed_namespaces: If set, cleanup and automatic Namespace creation are
            limited to these namespace directories.
        cleanup: If True, remove stale YAML files after generation.

    Returns:
        Set of paths that were written

    Raises:
        ValueError: If configuration validation fails
        RuntimeError: If manifest generation fails
    """
    if not blocks:
        logger.info("No configs configured")
        return set()

    if charts_dir is None:
        charts_dir = Path.home() / ".cache" / "manifest-builder"

    owned_namespaces = owned_namespaces or set()
    managed_namespaces = managed_namespaces or None
    jobs = _collect_generation_jobs(blocks)
    if not jobs:
        logger.info("No configs configured")
        return set()

    # Validate all of the configs first
    for job in jobs:
        config = job.config
        job.block.validate(config, repo_root)
        if config.namespace in owned_namespaces:
            raise ValueError(
                f"Config '{config.name}' targets namespace '{config.namespace}' "
                f"which is owned by another service (listed in owners/)"
            )

    # Generate manifests
    # Map the output paths to the config name that generated them
    written_paths: dict[Path, str] = {}
    cache_stats = ChartCacheStats()
    context = GenerationContext(
        output_dir=output_dir,
        repo_root=repo_root,
        charts_dir=charts_dir,
        verbose=verbose,
        images=images,
        cache_stats=cache_stats,
    )

    def generate_job(job: _GenerationJob) -> set[Path]:
        config = job.config
        try:
            logger.info(f"Generating manifest for {config.name} ({config.namespace})")
            return job.block.generate(config, context)
        except ManifestError:
            raise
        except Exception as e:
            logger.error(f"✗ {config.name} ({config.namespace})")
            raise ManifestError(config.name, e) from e

    def record_paths(job: _GenerationJob, paths: set[Path]) -> None:
        config = job.config
        try:
            # Check for conflicts with the previously written files
            conflicts = {p: written_paths[p] for p in paths if p in written_paths}
            if conflicts:
                conflict_details = []
                for path, previous_config in sorted(conflicts.items()):
                    conflict_details.append(
                        f"{_display_output_path(path, output_dir)} "
                        f"(generated by {previous_config})"
                    )
                conflict_list = "\n  ".join(conflict_details)
                raise ValueError(
                    f"Configuration conflict: {config.name} generates files that are already "
                    f"generated by another config:\n  {conflict_list}"
                )

            # Record which config generated each file
            for path in paths:
                written_paths[path] = config.name

            count = len(paths)
            logger.info(
                f"✓ {config.name} ({config.namespace}) -> {count} file{plural(count)}"
            )
        except ManifestError:
            raise
        except Exception as e:
            logger.error(f"✗ {config.name} ({config.namespace})")
            raise ManifestError(config.name, e) from e

    grouped_jobs: list[tuple[ConfigBlock[Any], list[_GenerationJob]]] = []
    for job in jobs:
        if not grouped_jobs or grouped_jobs[-1][0] is not job.block:
            grouped_jobs.append((job.block, []))
        grouped_jobs[-1][1].append(job)

    for block, block_jobs in grouped_jobs:
        # Blocks opt in to concurrent rendering; the rest stay sequential.
        if block.parallel_safe and len(block_jobs) > 1:
            worker_count = min(block.max_workers, len(block_jobs))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                generated_paths = executor.map(generate_job, block_jobs)
                for job, paths in zip(block_jobs, generated_paths, strict=True):
                    record_paths(job, paths)
        else:
            for job in block_jobs:
                record_paths(job, generate_job(job))

    # Catch any output that landed in an owned namespace via metadata override
    if owned_namespaces:
        intrusions = sorted(
            (path, source)
            for path, source in written_paths.items()
            if _path_namespace(path, output_dir) in owned_namespaces
        )
        if intrusions:
            details = "\n  ".join(
                f"{path} (from {source})" for path, source in intrusions
            )
            raise ValueError(
                "Generated output would land in a namespace owned by another "
                f"service:\n  {details}"
            )

    # Create Namespace objects for any namespace that lacks one
    namespace_paths = _ensure_namespaces(
        output_dir, written_paths, owned_namespaces, managed_namespaces
    )
    written_paths.update(namespace_paths)

    if cleanup:
        _cleanup_stale_files(
            output_dir, written_paths, owned_namespaces, managed_namespaces
        )

    if cache_stats.hits or cache_stats.misses:
        logger.info(
            f"Chart cache: {cache_stats.hits} hit{plural(cache_stats.hits)}, "
            f"{cache_stats.misses} miss{plural(cache_stats.misses, 'es')}"
        )

    total = len(written_paths)
    summary = f"Done! Generated {total} manifest{plural(total)}"
    removed = (
        _count_removed_files(
            output_dir, written_paths, owned_namespaces, managed_namespaces
        )
        if cleanup
        else 0
    )
    if removed:
        summary += f", removed {removed} stale file{plural(removed)}"
    logger.info(summary)

    return set(written_paths.keys())


def _path_namespace(path: Path, output_dir: Path) -> str | None:
    """Return the top-level namespace directory for ``path`` under ``output_dir``."""
    try:
        rel_parts = path.relative_to(output_dir).parts
    except ValueError:
        return None
    return rel_parts[0] if rel_parts else None


def _display_output_path(path: Path, output_dir: Path) -> str:
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return str(path)


def _ensure_namespaces(
    output_dir: Path,
    written_paths: dict[Path, str],
    owned_namespaces: set[str] | None = None,
    managed_namespaces: set[str] | None = None,
) -> dict[Path, str]:
    """Create Namespace objects for namespace directories that lack one.

    For each subdirectory of output_dir (excluding cluster/), checks whether a
    Namespace resource already exists for that namespace. If not, writes a minimal
    Namespace manifest to <output_dir>/<namespace>/namespace-<namespace>.yaml.

    Args:
        output_dir: Base output directory
        written_paths: Paths written so far in this run
        owned_namespaces: Namespaces owned by other services; skipped here
        managed_namespaces: If set, only these namespaces get Namespace objects

    Returns:
        Dict of newly created namespace paths mapped to the source label
    """
    if not output_dir.exists():
        return {}

    owned = owned_namespaces or set()
    candidate_namespaces = sorted(
        namespace
        for namespace in {_path_namespace(path, output_dir) for path in written_paths}
        if namespace is not None
    )
    new_paths: dict[Path, str] = {}
    for ns_name in candidate_namespaces:
        if (
            ns_name == "cluster"
            or ns_name == "kube-system"
            or ns_name == "owners"
            or ns_name.startswith(".")
            or ns_name in owned
            or (managed_namespaces is not None and ns_name not in managed_namespaces)
        ):
            continue

        ns_dir = output_dir / ns_name
        ns_filename = f"namespace-{ns_name}.yaml"

        # Skip if a Namespace was already written for this namespace
        if ns_dir / ns_filename in written_paths:
            continue
        if output_dir / "cluster" / ns_filename in written_paths:
            continue

        doc = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns_name},
        }
        out_path = ns_dir / ns_filename
        with open(out_path, "w") as f:
            dump_yaml(doc, f)

        logger.debug(f"Created Namespace {ns_name}")
        new_paths[out_path] = "__namespaces__"

    return new_paths


def _cleanup_stale_files(
    output_dir: Path,
    written_paths: dict[Path, str],
    owned_namespaces: set[str] | None = None,
    managed_namespaces: set[str] | None = None,
) -> None:
    """Remove stale files and empty directories from previous runs.

    Args:
        output_dir: Directory to clean
        written_paths: Set of paths that were written in this run
        owned_namespaces: Namespaces owned by other services; their files
            and directories are left untouched.
        managed_namespaces: If set, only these namespace directories are cleaned.
    """
    if not output_dir.exists():
        return

    owned = owned_namespaces or set()
    for existing in output_dir.rglob("*.yaml"):
        namespace = _path_namespace(existing, output_dir)
        if namespace is not None and namespace.startswith("."):
            continue
        if namespace in owned:
            continue
        if managed_namespaces is not None and namespace not in managed_namespaces:
            continue
        if existing not in written_paths:
            existing.unlink()
            logger.debug(
                "Deleted stale manifest during generation cleanup: %s",
                existing.relative_to(output_dir),
            )

    # Remove any empty directories, except those owned by other services
    for directory in sorted(output_dir.rglob("*"), reverse=True):
        if not directory.is_dir():
            continue
        namespace = _path_namespace(directory, output_dir)
        if namespace is not None and namespace.startswith("."):
            continue
        if namespace in owned:
            continue
        if managed_namespaces is not None and namespace not in managed_namespaces:
            continue
        if not any(directory.iterdir()):
            directory.rmdir()


def _count_removed_files(
    output_dir: Path,
    written_paths: dict[Path, str],
    owned_namespaces: set[str] | None = None,
    managed_namespaces: set[str] | None = None,
) -> int:
    """Count the number of stale files that were removed.

    Args:
        output_dir: Directory to check
        written_paths: Set of paths that were written in this run
        owned_namespaces: Namespaces owned by other services; not counted.
        managed_namespaces: If set, only these namespace directories are counted.

    Returns:
        Number of removed files
    """
    if not output_dir.exists():
        return 0

    owned = owned_namespaces or set()
    removed = 0
    for existing in output_dir.rglob("*.yaml"):
        namespace = _path_namespace(existing, output_dir)
        if namespace is not None and namespace.startswith("."):
            continue
        if namespace in owned:
            continue
        if managed_namespaces is not None and namespace not in managed_namespaces:
            continue
        if existing not in written_paths:
            removed += 1
    return removed
