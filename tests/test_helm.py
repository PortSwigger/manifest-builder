# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for helm command execution."""

import logging
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from manifest_builder.helm import ChartCacheStats, pull_chart, run_helm_template


def _fake_helm_pull(
    name: str, barrier: threading.Barrier | None = None
) -> Callable[..., MagicMock]:
    """Stand in for `helm pull --untar`, unpacking a chart called name.

    Like helm, it refuses to unpack over an existing directory. Given a barrier,
    every pull waits there first, so concurrent pulls overlap.
    """

    def run(cmd: list[str], **kwargs: object) -> MagicMock:
        if barrier is not None:
            barrier.wait(timeout=10)
        chart_dir = Path(cmd[cmd.index("--untardir") + 1]) / name
        try:
            chart_dir.mkdir()
        except FileExistsError:
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="Error: failed to untar: a file or directory with the name "
                f"{chart_dir} already exists",
            ) from None
        (chart_dir / "Chart.yaml").write_text(f"name: {name}\n")
        return MagicMock(returncode=0, stdout="", stderr="")

    return run


def test_pull_chart_uses_cached_chart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """pull_chart should skip download if chart already exists."""
    chart_dir = tmp_path / "myapp"
    chart_dir.mkdir()
    cache_stats = ChartCacheStats()

    caplog.set_level(logging.DEBUG, logger="manifest_builder.helm")
    result = pull_chart(
        "myapp", tmp_path, repo="https://charts.example.com", cache_stats=cache_stats
    )

    assert result == chart_dir
    assert "Chart cache hit: myapp" in caplog.text
    assert cache_stats.hits == 1
    assert cache_stats.misses == 0
    cache_records = [
        record for record in caplog.records if record.message.startswith("Chart cache")
    ]
    assert [record.levelno for record in cache_records] == [logging.DEBUG]


def test_pull_chart_oci_uses_cached_chart(tmp_path: Path) -> None:
    """pull_chart should skip download if OCI chart already exists using extracted chart name."""
    # For OCI repos, the chart dir is named after the last component of the OCI URL
    chart_dir = tmp_path / "gateway-helm"
    chart_dir.mkdir()

    result = pull_chart("oci://docker.io/envoyproxy/gateway-helm", tmp_path)

    assert result == chart_dir


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_http_repository(
    mock_run: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """pull_chart should use --repo for HTTP/HTTPS repositories."""
    mock_run.side_effect = _fake_helm_pull("cert-manager")
    cache_stats = ChartCacheStats()

    caplog.set_level(logging.DEBUG, logger="manifest_builder.helm")
    pull_chart(
        "cert-manager",
        tmp_path,
        repo="https://charts.jetstack.io",
        version="v1.19.4",
        cache_stats=cache_stats,
    )

    # Verify the command structure
    call_args = mock_run.call_args
    cmd = call_args[0][0]

    assert "helm" in cmd
    assert "pull" in cmd
    assert "cert-manager" in cmd
    assert "--repo" in cmd
    assert "https://charts.jetstack.io" in cmd
    assert "--version" in cmd
    assert "v1.19.4" in cmd
    assert "Chart cache miss: cert-manager" in caplog.text
    assert cache_stats.hits == 0
    assert cache_stats.misses == 1
    miss_records = [
        record
        for record in caplog.records
        if record.message.startswith("Chart cache miss:")
    ]
    assert [record.levelno for record in miss_records] == [logging.DEBUG]


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_oci_repository(mock_run: MagicMock, tmp_path: Path) -> None:
    """pull_chart should use OCI URL directly without --repo for OCI registries."""
    mock_run.side_effect = _fake_helm_pull("gateway-helm")

    pull_chart("oci://docker.io/envoyproxy/gateway-helm", tmp_path, version="v1.3.3")

    # Verify the command structure
    call_args = mock_run.call_args
    cmd = call_args[0][0]

    assert "helm" in cmd
    assert "pull" in cmd
    # For OCI, the repo URL is used directly (chart name not appended)
    assert "oci://docker.io/envoyproxy/gateway-helm" in cmd
    # --repo should NOT be used for OCI
    assert "--repo" not in cmd
    assert "--version" in cmd
    assert "v1.3.3" in cmd


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_oci_without_version(mock_run: MagicMock, tmp_path: Path) -> None:
    """pull_chart should work for OCI registries without a version."""
    mock_run.side_effect = _fake_helm_pull("gateway-helm")

    pull_chart("oci://docker.io/envoyproxy/gateway-helm", tmp_path)

    # Verify the command structure
    call_args = mock_run.call_args
    cmd = call_args[0][0]

    assert "helm" in cmd
    assert "pull" in cmd
    # For OCI, the repo URL is used directly
    assert "oci://docker.io/envoyproxy/gateway-helm" in cmd
    assert "--repo" not in cmd
    assert "--version" not in cmd


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_unpacks_into_staging_directory(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """helm should unpack beside the cache entry, which then appears complete."""
    mock_run.side_effect = _fake_helm_pull("cert-manager")

    result = pull_chart("cert-manager", tmp_path, repo="https://charts.jetstack.io")

    cmd = mock_run.call_args[0][0]
    untardir = Path(cmd[cmd.index("--untardir") + 1])
    assert untardir.parent == tmp_path
    assert result == tmp_path / "cert-manager"
    assert (result / "Chart.yaml").is_file()
    assert [path.name for path in tmp_path.iterdir()] == ["cert-manager"]


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_concurrent_pulls_of_uncached_chart(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """Pulls of the same uncached chart that overlap should all get the chart."""
    mock_run.side_effect = _fake_helm_pull("alloy", threading.Barrier(2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                pull_chart,
                "alloy",
                tmp_path,
                repo="https://grafana.github.io/helm-charts",
            )
            for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert results == [tmp_path / "alloy"] * 2
    assert (tmp_path / "alloy" / "Chart.yaml").is_file()
    assert [path.name for path in tmp_path.iterdir()] == ["alloy"]


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_failed_pull_leaves_nothing_cached(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """A pull that fails part way should leave nothing for later pulls to reuse."""

    def fail_part_way(cmd: list[str], **kwargs: object) -> MagicMock:
        (Path(cmd[cmd.index("--untardir") + 1]) / "alloy").mkdir()
        raise subprocess.CalledProcessError(1, cmd, stderr="Error: unexpected EOF")

    mock_run.side_effect = fail_part_way
    with pytest.raises(RuntimeError, match="helm pull failed for alloy"):
        pull_chart("alloy", tmp_path)
    assert list(tmp_path.iterdir()) == []

    mock_run.side_effect = _fake_helm_pull("alloy")
    cache_stats = ChartCacheStats()
    result = pull_chart("alloy", tmp_path, cache_stats=cache_stats)

    assert cache_stats.misses == 1
    assert (result / "Chart.yaml").is_file()


@patch("manifest_builder.helm.subprocess.run")
def test_pull_chart_rejects_pull_without_chart(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """A pull that leaves no chart under the expected name should fail uncached."""

    def unpack_other_name(cmd: list[str], **kwargs: object) -> MagicMock:
        untardir = Path(cmd[cmd.index("--untardir") + 1])
        # helm creates the directory named after the chart reference whatever
        # the archive's own top-level directory is called.
        (untardir / "gateway-helm").mkdir()
        (untardir / "gateway").mkdir()
        (untardir / "gateway" / "Chart.yaml").write_text("name: gateway\n")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = unpack_other_name
    with pytest.raises(RuntimeError, match=r"unpacked no gateway-helm/Chart\.yaml"):
        pull_chart("oci://registry.example.com/charts/gateway-helm", tmp_path)
    assert list(tmp_path.iterdir()) == []


@patch("manifest_builder.helm.check_helm_available", return_value=True)
@patch("manifest_builder.helm.subprocess.run")
def test_run_helm_template_includes_crds(
    mock_run: MagicMock,
    mock_check_helm_available: MagicMock,
) -> None:
    """helm template should ask Helm to include chart CRDs."""
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    run_helm_template(
        release_name="my-release",
        chart="my-chart",
        namespace="default",
        values_files=[],
    )

    call_args = mock_run.call_args
    cmd = call_args[0][0]

    assert "--include-crds" in cmd
    mock_check_helm_available.assert_called_once_with()
