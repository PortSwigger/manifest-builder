# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Helm command execution for generating manifests."""

import logging
import subprocess
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ChartCacheStats:
    """Track chart cache behavior during one manifest generation run."""

    hits: int = 0
    misses: int = 0


def get_helm_version() -> str:
    """
    Get the installed helm version.

    Returns:
        Helm version string (e.g., "v3.12.0")

    Raises:
        RuntimeError: If helm is not available or version check fails
    """
    try:
        result = subprocess.run(
            ["helm", "version", "--short"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"helm version check failed: {result.stderr}")
        return result.stdout.strip()
    except FileNotFoundError as e:
        raise RuntimeError(
            "helm is not installed or not available in PATH. "
            "Please install helm: https://helm.sh/docs/intro/install/"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("helm version check timed out") from e


@cache
def check_helm_available() -> bool:
    """
    Check if helm is installed and available.

    Returns:
        True if helm is available, False otherwise
    """
    try:
        subprocess.run(
            ["helm", "version", "--short"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def pull_chart(
    chart: str,
    dest: Path,
    repo: str | None = None,
    version: str | None = None,
    cache_stats: ChartCacheStats | None = None,
) -> Path:
    """
    Pull a chart from a repository and untar it to a local directory.

    Skips the pull if the destination already exists.

    Args:
        chart: Chart name or OCI URL (e.g., "mychart" or "oci://registry.com/mychart")
        dest: Directory to untar the chart into
        repo: Optional repository URL (for HTTP/HTTPS repos)
        version: Optional chart version
        cache_stats: Optional cache hit/miss counter to update

    Returns:
        Path to the untarred chart directory

    Raises:
        RuntimeError: If helm pull fails
    """
    # Determine actual chart name for the directory
    if chart.startswith("oci://"):
        actual_chart_name = chart.rstrip("/").split("/")[-1]
    else:
        actual_chart_name = chart

    chart_dir = dest / actual_chart_name

    if chart_dir.exists():
        if cache_stats is not None:
            cache_stats.hits += 1
        logger.debug(f"Chart cache hit: {chart} -> {chart_dir}")
        return chart_dir

    dest.mkdir(parents=True, exist_ok=True)

    version_str = f" (version {version})" if version else ""
    source_str = f" from {repo}" if repo else ""
    if cache_stats is not None:
        cache_stats.misses += 1
    logger.debug(f"Chart cache miss: {chart} -> {chart_dir}")
    logger.info(f"Downloading chart {chart}{source_str}{version_str}")

    cmd = ["helm", "pull"]
    cmd.append(chart)

    if repo:
        cmd.extend(["--repo", repo])

    cmd.extend(["--untar", "--untardir", str(dest)])

    if version:
        cmd.extend(["--version", version])

    logger.debug(f"Executing: {' '.join(cmd)}")

    try:
        start = time.perf_counter()
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        elapsed = time.perf_counter() - start
        logger.info(f"Downloaded chart {chart} in {elapsed:.2f}s")
        logger.debug(f"Successfully unpacked chart to {chart_dir}")
    except subprocess.CalledProcessError as e:
        cmd_str = " ".join(cmd)
        raise RuntimeError(
            f"helm pull failed for {chart}:\n  Command: {cmd_str}\n  Error: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        cmd_str = " ".join(cmd)
        raise RuntimeError(
            f"helm pull timed out for {chart}:\n  Command: {cmd_str}"
        ) from e

    return chart_dir


def run_helm_template(
    release_name: str,
    chart: str,
    namespace: str,
    values_files: list[Path],
    version: str | None = None,
) -> str:
    """
    Execute helm template command and return the generated manifests.

    Args:
        release_name: Name of the Helm release
        chart: Chart reference (repo/chart, local path, or OCI URL)
        namespace: Kubernetes namespace
        values_files: List of values files to apply
        version: Optional chart version (ignored for local paths)

    Returns:
        Generated YAML manifests as a string

    Raises:
        RuntimeError: If helm is not available or the command fails
    """
    if not check_helm_available():
        raise RuntimeError(
            "helm is not installed or not available in PATH. "
            "Please install helm: https://helm.sh/docs/intro/install/"
        )

    cmd = [
        "helm",
        "template",
        release_name,
        chart,
        "--namespace",
        namespace,
        "--include-crds",
    ]

    for values_file in values_files:
        cmd.extend(["-f", str(values_file)])

    if version:
        cmd.extend(["--version", version])

    logger.debug(f"Executing: {' '.join(cmd)}")

    try:
        start = time.perf_counter()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        elapsed = time.perf_counter() - start
        logger.debug(f"helm template for {release_name} completed in {elapsed:.2f}s")
        return result.stdout
    except subprocess.CalledProcessError as e:
        cmd_str = " ".join(cmd)
        raise RuntimeError(
            f"helm template failed for {release_name}:\n  Command: {cmd_str}\n  Error: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        cmd_str = " ".join(cmd)
        raise RuntimeError(
            f"helm template timed out for {release_name}:\n  Command: {cmd_str}"
        ) from e
