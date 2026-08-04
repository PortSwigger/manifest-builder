# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Copy manifest generation from existing manifests."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pystache
import yaml
from pystache.common import MissingTags

from manifest_builder.config import (
    TemplateValue,
    parse_variables,
    validate_known_fields,
)
from manifest_builder.handlers import ConfigHandler, GenerationContext
from manifest_builder.k8s import (
    CLUSTER_SCOPED_KINDS,
    config_checksum,
    configmap_suffix_from_mount_path,
    make_configmaps,
    make_k8s_name,
)
from manifest_builder.output import write_documents


@dataclass
class CopyConfig:
    """Configuration for an app that copies existing manifests verbatim."""

    name: str
    source: Path  # resolved directory containing manifests to copy
    namespace: str | None = None
    config: dict[str, Path] | None = None  # container path -> resolved local path
    variables: dict[str, TemplateValue] = field(default_factory=dict)


def validate_copy_config(config: CopyConfig) -> None:
    """Validate a copy app configuration."""
    if not config.source.exists():
        raise ValueError(
            f"source directory not found for '{config.name}': {config.source}"
        )
    if not config.source.is_dir():
        raise ValueError(
            f"source path is not a directory for '{config.name}': {config.source}"
        )
    for container_path, local_path in (config.config or {}).items():
        if not local_path.exists():
            raise ValueError(
                f"Config file not found for '{config.name}': {local_path} "
                f"(mapped from {container_path})"
            )


class CopyConfigHandler(ConfigHandler[CopyConfig]):
    """Generate manifests for copy configs."""

    def __init__(self, configs: Sequence[CopyConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "copy"

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
            raise ValueError(f"'copy' must be a list of tables in {source_file}")

        variables = parse_variables(root_config.get("variables"), source_file)
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[copy]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_copy_config(
                    item, source_file, variables, index, default_namespace
                )
            )

    def iter_configs(self) -> list[CopyConfig]:
        return self.configs

    def validate(self, config: CopyConfig, repo_root: Path) -> None:
        validate_copy_config(config)

    def generate(
        self,
        config: CopyConfig,
        context: GenerationContext,
    ) -> set[Path]:
        return generate_copy(config, context.output_dir, images=context.images)


def _parse_copy_config(
    data: dict,
    source_file: Path,
    variables: dict[str, Any],
    table_index: int = 0,
    default_namespace: str | None = None,
) -> CopyConfig:
    """Parse a copy app configuration from TOML data."""
    validate_known_fields(
        "[[copy]]",
        data,
        {"name", "namespace", "source", "config"},
        source_file,
        table_index,
    )

    if "source" not in data:
        raise ValueError(f"Missing required field 'source' in {source_file}")

    config_dir = source_file.parent
    namespace = data.get("namespace", default_namespace)
    name = data.get("name", namespace)
    if name is None:
        raise ValueError(
            f"[[copy]] entry in {source_file} must set either 'name' or 'namespace'"
        )
    source = config_dir / data["source"]

    config_dict = None
    config_data = data.get("config")
    if config_data is not None:
        # Support both [copy.config] (dict) and [[copy.config]] (list of dicts)
        if isinstance(config_data, list):
            merged: dict[str, str] = {}
            for item in config_data:
                merged.update(item)
            config_data = merged
        config_dict = {
            container_path: config_dir / local_path
            for container_path, local_path in config_data.items()
        }

    return CopyConfig(
        name=name,
        namespace=namespace,
        source=source,
        config=config_dict,
        variables=variables.copy(),
    )


def generate_copy(
    config: CopyConfig,
    output_dir: Path,
    images: dict[str, str] | None = None,
) -> set[Path]:
    """Generate manifests for a copy app by copying existing manifests.

    Reads all YAML files from the source directory, injects the configured
    namespace into any namespaced resources that don't already have one, and
    optionally creates a ConfigMap from the files listed in the config table.

    The manifests are processed as Mustache templates, with image references and
    the config's template variables (including those merged from ``--vars-from``)
    available as ``{{variable}}`` substitutions.

    Args:
        config: Copy app configuration
        output_dir: Directory to write generated manifests
        images: Dict mapping image variable names to image references

    Returns:
        Set of paths written
    """
    renderer = pystache.Renderer(missing_tags=MissingTags.strict)
    docs: list[dict] = []
    context: dict[str, Any] = {**(images or {}), **config.variables}

    for yaml_file in sorted(config.source.glob("*.yaml")):
        text = yaml_file.read_text()
        text = renderer.render(text, context)

        for doc in yaml.safe_load_all(text):
            if doc:
                docs.append(doc)

    needs_namespace = bool(config.config) or any(
        doc.get("kind")
        and doc.get("kind") not in CLUSTER_SCOPED_KINDS
        and "namespace" not in doc.get("metadata", {})
        for doc in docs
    )
    if needs_namespace and config.namespace is None:
        raise ValueError(
            f"'namespace' is required for copy config '{config.name}' "
            "because it contains namespaced resources"
        )

    for doc in docs:
        kind = doc.get("kind")
        if (
            kind
            and kind not in CLUSTER_SCOPED_KINDS
            and "namespace" not in doc.get("metadata", {})
        ):
            doc.setdefault("metadata", {})["namespace"] = config.namespace

    if config.config:
        k8s_name = make_k8s_name(config.name)
        configmaps = make_configmaps(k8s_name, config.config)
        checksum = config_checksum(configmaps)
        for cm in configmaps:
            cm.setdefault("metadata", {})["namespace"] = config.namespace
        docs.extend(configmaps)

        mount_groups = {
            str(Path(container_path).parent) for container_path in config.config
        }
        for doc in docs:
            if doc.get("kind") == "Deployment":
                doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                    "metadata", {}
                ).setdefault("annotations", {})["checksum/config"] = checksum
                pod_spec = (
                    doc.setdefault("spec", {})
                    .setdefault("template", {})
                    .setdefault("spec", {})
                )
                for mount_path in sorted(mount_groups):
                    cm_name = (
                        f"{k8s_name}-{configmap_suffix_from_mount_path(mount_path)}"
                    )
                    for container in pod_spec.get("containers", []):
                        container.setdefault("volumeMounts", []).append(
                            {"name": cm_name, "mountPath": mount_path}
                        )
                    pod_spec.setdefault("volumes", []).append(
                        {"name": cm_name, "configMap": {"name": cm_name}}
                    )

    return write_documents(docs, output_dir, config.namespace, config.name)
