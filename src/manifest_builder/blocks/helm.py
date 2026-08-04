# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Helm chart config block."""

import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import pystache
from pystache.common import MissingTags

from manifest_builder.config import (
    TemplateValue,
    parse_variables,
    validate_known_fields,
)
from manifest_builder.handlers import ConfigHandler, GenerationContext
from manifest_builder.helm import ChartCacheStats, pull_chart, run_helm_template
from manifest_builder.helmfile import Helmfile
from manifest_builder.k8s import (
    CLUSTER_SCOPED_KINDS,
    config_checksum,
    inject_custom_token_projection,
)
from manifest_builder.output import (
    dump_all_yaml,
    load_all_yaml,
    write_documents,
    write_manifests,
)

logger = logging.getLogger(__name__)

# Concurrent renders share the chart cache, whose download path is not atomic.
_HELM_PULL_LOCK = Lock()


@dataclass
class ChartConfig:
    """Configuration for a single Helm chart."""

    name: str
    namespace: str
    chart: str | None  # None when using a helmfile release reference
    repo: str | None
    version: str | None
    values: list[Path]
    release: str | None  # helmfile release name; None for direct chart entries
    variables: dict[str, TemplateValue] = field(default_factory=dict)
    extra_resources: Path | None = (
        None  # directory with additional YAML resources to include
    )
    init: Path | None = None  # optional shell script to inject as initContainer
    config: dict[str, Path] | None = None  # ConfigMap key -> resolved local path
    name_override: str | None = None  # optional release name passed to helm template
    custom_token_audiences: list[str] | None = None


def validate_chart_config(config: ChartConfig, repo_root: Path) -> None:
    """Validate a Helm chart configuration."""
    for values_path in config.values:
        if not values_path.exists():
            raise ValueError(
                f"Values file not found for chart '{config.name}': {values_path}"
            )

    for config_key, local_path in (config.config or {}).items():
        if not local_path.exists():
            raise ValueError(
                f"Config file not found for chart '{config.name}': {local_path} "
                f"(mapped from {config_key})"
            )

    if config.extra_resources is not None:
        if not config.extra_resources.exists():
            raise ValueError(
                f"Extra resources directory not found for '{config.name}': {config.extra_resources}"
            )
        if not config.extra_resources.is_dir():
            raise ValueError(
                f"Extra resources path is not a directory for '{config.name}': {config.extra_resources}"
            )

    if config.chart is not None and (
        config.chart.startswith("./") or config.chart.startswith("/")
    ):
        chart_path = repo_root / config.chart
        if not chart_path.exists():
            raise ValueError(
                f"Local chart path not found for '{config.name}': {config.chart}"
            )

    if config.init is not None and not config.init.exists():
        raise ValueError(f"init script not found for '{config.name}': {config.init}")


class HelmConfigHandler(ConfigHandler[ChartConfig]):
    """Generate manifests for Helm chart configs."""

    # Each chart renders into its own temporary directory, so renders may
    # overlap. Chart cache downloads are serialized by _HELM_PULL_LOCK.
    parallel_safe = True

    def __init__(self, configs: Sequence[ChartConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "helm"

    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        del default_image
        if not isinstance(data, list):
            raise ValueError(f"'helm' must be a list of tables in {source_file}")

        variables = parse_variables(root_config.get("variables"), source_file)
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[helm]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_chart_config(
                    item, source_file, variables, index, default_namespace
                )
            )

    def iter_configs(self) -> Sequence[ChartConfig]:
        return self.configs

    def resolve(self, helmfile: Helmfile | None) -> None:
        if not any(config.release for config in self.configs):
            return

        if helmfile is None:
            names = [config.name for config in self.configs if config.release]
            raise ValueError(
                f"Charts {names} reference helmfile releases but no releases.yaml was found"
            )

        release_data = _get_helmfile_data(helmfile)
        repo_by_name = release_data[0]
        release_by_name = release_data[1]

        resolved: list[ChartConfig] = []
        for config in self.configs:
            if config.release is None:
                resolved.append(config)
                continue

            release_name = config.release
            if release_name not in release_by_name:
                raise ValueError(f"Release '{release_name}' not found in releases.yaml")

            hf_release = release_by_name[release_name]

            parts = hf_release.chart.split("/", 1)
            if len(parts) != 2:
                raise ValueError(
                    f"helmfile release '{release_name}' chart '{hf_release.chart}' "
                    "must be in 'reponame/chartname' format"
                )
            repo_name, chart_name = parts

            if repo_name not in repo_by_name:
                raise ValueError(
                    f"Repository '{repo_name}' referenced by release '{release_name}' "
                    "not found in releases.yaml repositories"
                )

            repo_url = repo_by_name[repo_name]

            if repo_url.startswith("oci://"):
                base_url = repo_url.rstrip("/")
                resolved_chart = f"{base_url}/{chart_name}"
                resolved_repo = None
            else:
                resolved_chart = chart_name
                resolved_repo = repo_url

            resolved.append(
                ChartConfig(
                    name=config.name,
                    namespace=config.namespace,
                    chart=resolved_chart,
                    repo=resolved_repo,
                    version=hf_release.version,
                    values=config.values,
                    variables=config.variables,
                    release=config.release,
                    extra_resources=config.extra_resources,
                    init=config.init,
                    config=config.config,
                    name_override=config.name_override,
                    custom_token_audiences=config.custom_token_audiences,
                )
            )

        self.configs = resolved

    def validate(self, config: ChartConfig, repo_root: Path) -> None:
        validate_chart_config(config, repo_root)

    def generate(
        self,
        config: ChartConfig,
        context: GenerationContext,
    ) -> set[Path]:
        return _generate_helm_manifests(
            config,
            context.output_dir,
            context.charts_dir,
            context.verbose,
            images=context.images,
            cache_stats=context.cache_stats,
        )


def _parse_chart_config(
    data: dict,
    source_file: Path,
    variables: dict[str, TemplateValue],
    table_index: int = 0,
    default_namespace: str | None = None,
) -> ChartConfig:
    """Parse a single Helm chart configuration from TOML data."""
    validate_known_fields(
        "[[helm]]",
        data,
        {
            "name",
            "namespace",
            "chart",
            "release",
            "repo",
            "version",
            "values",
            "config",
            "extra-resources",
            "init",
            "name-override",
            "custom-token-audience",
            "custom-token-audiences",
        },
        source_file,
        table_index,
    )

    has_release = "release" in data
    has_chart = "chart" in data

    if has_release and has_chart:
        raise ValueError(f"Cannot specify both 'release' and 'chart' in {source_file}")
    if not has_release and not has_chart:
        raise ValueError(f"Must specify either 'release' or 'chart' in {source_file}")
    if "namespace" not in data and default_namespace is None:
        raise ValueError(f"Missing required field 'namespace' in {source_file}")

    config_dir = source_file.parent
    namespace = data.get("namespace", default_namespace)
    raw_values = data.get("values", [])
    if isinstance(raw_values, str):
        raw_values = [raw_values]
    values = [config_dir / v for v in raw_values]

    extra_resources = None
    if "extra-resources" in data:
        extra_resources = config_dir / data["extra-resources"]

    init = None
    if "init" in data:
        init = config_dir / data["init"]

    config_files = _parse_chart_config_files(data.get("config"), source_file)
    custom_token_audiences = _parse_custom_token_audiences(data, source_file)

    if has_release:
        return ChartConfig(
            name=data["release"],
            namespace=namespace,
            chart=None,
            repo=None,
            version=None,
            values=values,
            variables=variables.copy(),
            release=data["release"],
            extra_resources=extra_resources,
            init=init,
            config=config_files,
            name_override=data.get("name-override"),
            custom_token_audiences=custom_token_audiences,
        )

    if "name" not in data:
        raise ValueError(f"Missing required field 'name' in {source_file}")
    return ChartConfig(
        name=data["name"],
        namespace=namespace,
        chart=data["chart"],
        repo=data.get("repo"),
        version=data.get("version"),
        values=values,
        variables=variables.copy(),
        release=None,
        extra_resources=extra_resources,
        init=init,
        config=config_files,
        name_override=data.get("name-override"),
        custom_token_audiences=custom_token_audiences,
    )


def _parse_custom_token_audiences(data: dict, source_file: Path) -> list[str] | None:
    """Normalize singular and plural custom token audience fields."""
    custom_token_audience = data.get("custom-token-audience")
    custom_token_audiences = data.get("custom-token-audiences")

    if custom_token_audience is not None and custom_token_audiences is not None:
        raise ValueError(
            f"Cannot specify both 'custom-token-audience' and "
            f"'custom-token-audiences' in {source_file}"
        )

    if custom_token_audience is not None:
        if not isinstance(custom_token_audience, str):
            raise ValueError(
                f"'custom-token-audience' must be a string in {source_file}"
            )
        return [custom_token_audience]

    if custom_token_audiences is not None and (
        not isinstance(custom_token_audiences, list)
        or not all(isinstance(audience, str) for audience in custom_token_audiences)
    ):
        raise ValueError(
            f"'custom-token-audiences' must be a list of strings in {source_file}"
        )

    return custom_token_audiences


def _parse_chart_config_files(
    data: object, source_file: Path
) -> dict[str, Path] | None:
    """Parse Helm ConfigMap file mappings as config-key -> local file path."""
    if data is None:
        return None

    if not isinstance(data, dict):
        raise ValueError(f"'helm.config' must be a table in {source_file}")

    config_dir = source_file.parent
    config_files: dict[str, Path] = {}
    for config_key, local_path in data.items():
        if not isinstance(config_key, str) or not isinstance(local_path, str):
            raise ValueError(
                f"'helm.config' entries must map strings to strings in {source_file}"
            )
        config_files[config_key] = config_dir / local_path

    return config_files


def _get_helmfile_data(helmfile: Helmfile) -> tuple[dict[str, str], dict[str, Any]]:
    repositories = helmfile.repositories
    releases = helmfile.releases
    repo_by_name = {
        repo.name: (f"oci://{repo.url}" if repo.oci else repo.url)
        for repo in repositories
    }
    release_by_name = {release.name: release for release in releases}
    return repo_by_name, release_by_name


def _generate_helm_manifests(
    config: ChartConfig,
    output_dir: Path,
    charts_dir: Path,
    verbose: bool = False,
    images: dict[str, str] | None = None,
    cache_stats: ChartCacheStats | None = None,
) -> set[Path]:
    """Generate manifests from a Helm chart.

    Args:
        config: Helm chart configuration
        output_dir: Directory to write generated manifests
        charts_dir: Directory for caching pulled charts
        verbose: If True, log detailed output
        cache_stats: Optional chart cache hit/miss counter to update

    Returns:
        Set of paths written
    """
    logger.debug(f"Chart: {config.chart}")
    if config.repo:
        logger.debug(f"Repo: {config.repo}")
    if config.version:
        logger.debug(f"Version: {config.version}")
    if config.values:
        logger.debug(f"Values: {', '.join(str(v) for v in config.values)}")

    if config.chart is None:
        raise ValueError(
            f"Chart '{config.name}' has no resolved chart reference; "
            "ensure resolve_configs() was called before generate_manifests()"
        )

    # Pull the chart from the repo if configured (traditional or OCI)
    if config.repo or (config.chart and config.chart.startswith("oci://")):
        version_suffix = f"-{config.version}" if config.version else ""

        # Create a filesystem-safe directory name for the cache
        if config.chart.startswith("oci://"):
            chart_slug = config.chart.replace("oci://", "").replace("/", "_")
        else:
            chart_slug = config.chart

        pull_dest = charts_dir / f"{chart_slug}{version_suffix}"

        with _HELM_PULL_LOCK:
            chart_path = str(
                pull_chart(
                    chart=config.chart,
                    dest=pull_dest,
                    repo=config.repo,
                    version=config.version,
                    cache_stats=cache_stats,
                )
            )
    else:
        chart_path = config.chart

    values_context: dict[str, TemplateValue | str] = {
        **(images or {}),
        **config.variables,
    }
    helm_release_name = config.name_override or config.name
    with tempfile.TemporaryDirectory(prefix="manifest-builder-values-") as temp_dir:
        values_paths = _render_values_files(
            config.values, Path(temp_dir), values_context
        )
        manifest_content = run_helm_template(
            release_name=helm_release_name,
            chart=chart_path,
            namespace=config.namespace,
            values_files=values_paths,
        )

    if config.custom_token_audiences:
        docs = load_all_yaml(manifest_content)
        deployments = [doc for doc in docs if doc.get("kind") == "Deployment"]
        if len(deployments) != 1:
            raise ValueError(
                f"Custom token audience injection for Helm chart '{config.name}' "
                f"requires exactly one Deployment, found {len(deployments)}"
            )
        inject_custom_token_projection(deployments[0], config.custom_token_audiences)
        manifest_content = dump_all_yaml(docs)

    # Inject init container if configured
    if config.init:
        docs = load_all_yaml(manifest_content)
        deployments = [d for d in docs if d.get("kind") == "Deployment"]
        if len(deployments) != 1:
            raise ValueError(
                f"init requires exactly one Deployment in chart '{config.name}', "
                f"found {len(deployments)}"
            )
        alpine_image = (images or {}).get("alpine_image")
        if not alpine_image:
            raise ValueError(
                f"init requires 'alpine_image' to be defined in images.toml "
                f"for '{config.name}'"
            )
        script = config.init.read_text()
        deployment = deployments[0]
        pod_spec = (
            deployment.setdefault("spec", {})
            .setdefault("template", {})
            .setdefault("spec", {})
        )
        # Collect unique volumeMounts from all containers
        seen = set()
        volume_mounts = []
        for container in pod_spec.get("containers", []):
            for vm in container.get("volumeMounts", []):
                key = (vm.get("name"), vm.get("mountPath"))
                if key not in seen:
                    seen.add(key)
                    volume_mounts.append(vm)
        init_container: dict = {
            "name": config.init.stem,
            "image": alpine_image,
            "command": ["/bin/sh", "-c", script],
        }
        if volume_mounts:
            init_container["volumeMounts"] = volume_mounts
        pod_spec["initContainers"] = [init_container]
        # Re-serialize; write_manifests will re-parse
        manifest_content = dump_all_yaml(docs)

    configmap = None
    if config.config:
        configmap = _make_helm_configmap(
            helm_release_name, config.namespace, config.config
        )
        checksum = config_checksum([configmap])
        docs = load_all_yaml(manifest_content)
        for doc in docs:
            if doc.get("kind") in {"Deployment", "StatefulSet"}:
                doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                    "metadata", {}
                ).setdefault("annotations", {})["checksum/config"] = checksum

        manifest_content = dump_all_yaml(docs)

    paths = write_manifests(manifest_content, output_dir, config.namespace, config.name)

    if configmap:
        paths.update(
            write_documents([configmap], output_dir, config.namespace, config.name)
        )

    # Handle extra resources if configured
    if config.extra_resources:
        renderer = pystache.Renderer(missing_tags=MissingTags.strict)
        extra_docs: list[dict] = []
        for yaml_file in sorted(config.extra_resources.glob("*.yaml")):
            rendered = renderer.render(yaml_file.read_text(), values_context)
            for doc in load_all_yaml(rendered):
                # Add namespace to namespaced resources without one
                kind = doc.get("kind")
                if (
                    kind
                    and kind not in CLUSTER_SCOPED_KINDS
                    and "namespace" not in doc.get("metadata", {})
                ):
                    doc.setdefault("metadata", {})["namespace"] = config.namespace
                extra_docs.append(doc)
        if extra_docs:
            extra_paths = write_documents(
                extra_docs, output_dir, config.namespace, config.name
            )
            paths.update(extra_paths)
            logger.debug(f"Copied {len(extra_docs)} extra resources")

    return paths


def _make_helm_configmap(
    name: str,
    namespace: str,
    config_files: dict[str, Path],
) -> dict:
    """Build a Helm companion ConfigMap from config-key -> local file mappings."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-config",
            "namespace": namespace,
        },
        "data": {
            config_key: local_path.read_text()
            for config_key, local_path in sorted(config_files.items())
        },
    }


def _render_values_files(
    values_paths: list[Path],
    temp_dir: Path,
    context: dict[str, TemplateValue | str],
) -> list[Path]:
    """Render values files as Mustache templates into temporary files."""
    if not values_paths:
        return []

    renderer = pystache.Renderer(missing_tags=MissingTags.strict)
    rendered_paths: list[Path] = []
    for index, values_path in enumerate(values_paths):
        rendered_path = temp_dir / f"{index:02d}-{values_path.name}"
        rendered_path.write_text(renderer.render(values_path.read_text(), context))
        rendered_paths.append(rendered_path)

    return rendered_paths
