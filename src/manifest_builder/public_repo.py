# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Public ECR repository manifest generation from bundled Mustache templates."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pystache
import yaml
from pystache.common import MissingTags

from manifest_builder.config import (
    ManifestConfig,
    PublicRepoConfig,
    TemplateValue,
    parse_variables,
    validate_known_fields,
)
from manifest_builder.handlers import ConfigHandler, GenerationContext
from manifest_builder.k8s import CLUSTER_SCOPED_KINDS
from manifest_builder.output import write_documents


class PublicRepoConfigHandler(ConfigHandler):
    """Generate manifests for public-repo configs."""

    def __init__(self, configs: Sequence[PublicRepoConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "public-repo"

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
            raise ValueError(f"'public-repo' must be a list of tables in {source_file}")

        variables = parse_variables(root_config.get("variables"), source_file)
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[public-repo]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_public_repo_config(
                    item, source_file, variables, index, default_namespace
                )
            )

    def iter_configs(self) -> list[PublicRepoConfig]:
        return self.configs

    def validate(self, config: ManifestConfig, repo_root: Path) -> None:
        if not isinstance(config, PublicRepoConfig):
            raise TypeError(
                f"PublicRepoConfigHandler cannot process {type(config).__name__}"
            )

    def generate(
        self,
        config: ManifestConfig,
        context: GenerationContext,
    ) -> set[Path]:
        if not isinstance(config, PublicRepoConfig):
            raise TypeError(
                f"PublicRepoConfigHandler cannot process {type(config).__name__}"
            )
        return generate_public_repo(config, context.output_dir, images=context.images)


def _parse_public_repo_config(
    data: dict,
    source_file: Path,
    variables: dict[str, TemplateValue],
    table_index: int = 0,
    default_namespace: str | None = None,
) -> PublicRepoConfig:
    """Parse a public-repo configuration from TOML data."""
    validate_known_fields(
        "[[public-repo]]",
        data,
        {"name", "enable-charts"},
        source_file,
        table_index,
    )

    if "name" not in data:
        raise ValueError(f"Missing required field 'name' in {source_file}")
    name = data["name"]
    if not isinstance(name, str):
        raise ValueError(f"'name' must be a string in {source_file}")

    enable_charts = data.get("enable-charts", False)
    if not isinstance(enable_charts, bool):
        raise ValueError(f"'enable-charts' must be a boolean in {source_file}")

    return PublicRepoConfig(
        name=name,
        namespace=default_namespace or name,
        enable_charts=enable_charts,
        variables=variables.copy(),
    )


def generate_public_repo(
    config: PublicRepoConfig,
    output_dir: Path,
    images: dict[str, str] | None = None,
    _templates_override: Path | None = None,  # for testing only
) -> set[Path]:
    """Generate manifests granting GitHub Actions publish access to public ECR.

    Creates an ecr-public Repository for the images built from
    https://github.com/portswigger/<name>, along with an IAM Role and
    RolePolicy that let the repository's GitHub Actions workflows push to it
    via OIDC from the main branch or any tag. With enable_charts, a
    charts/<name> repository is also created and covered by the policy.
    """
    if _templates_override is not None:
        templates_dir = _templates_override
    else:
        from importlib.resources import files as get_package_files

        templates_dir = Path(
            str(get_package_files("manifest_builder") / "templates" / "public-repo")
        )

    context: dict[str, Any] = {
        **(images or {}),
        **config.variables,
        "name": config.name,
        "enable_charts": config.enable_charts,
    }

    renderer = pystache.Renderer(missing_tags=MissingTags.strict)
    docs: list[dict] = []
    for template_file in sorted(templates_dir.glob("*.yaml")):
        if template_file.name.startswith("_"):
            continue

        rendered = renderer.render(template_file.read_text(), context)
        for doc in yaml.safe_load_all(rendered):
            if doc:
                docs.append(doc)

    for doc in docs:
        kind = doc.get("kind")
        if kind and kind not in CLUSTER_SCOPED_KINDS:
            doc.setdefault("metadata", {})["namespace"] = config.namespace

    return write_documents(docs, output_dir, config.namespace, config.name)
