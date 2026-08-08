# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Manifest Builder - Generate Kubernetes manifests from configuration input."""

from collections.abc import Mapping
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from manifest_builder.config import TemplateValue
from manifest_builder.discovery import ExternalPlugins
from manifest_builder.result import GenerationResult, KubernetesObjectRef

try:
    __version__ = str(import_module("manifest_builder._version").__version__)
except ModuleNotFoundError:
    try:
        __version__ = version("manifest-builder")
    except PackageNotFoundError:
        __version__ = "0.0.0"


def generate(
    config: Path,
    output: Path,
    repo_root: Path | None = None,
    verbose: bool = False,
    create_commit: bool = False,
    allow_dirty_config: bool = False,
    namespace: str | None = None,
    image: str | None = None,
    *,
    vars_from: Path | None = None,
    vars: Mapping[str, TemplateValue] | None = None,
    target: str | None = None,
    plugins: ExternalPlugins | None = None,
) -> GenerationResult:
    """Generate manifests from ``config`` into ``output``.

    ``target`` names the target to generate, for a ``version = 2`` config
    directory. ``plugins`` supplies config blocks from outside the config
    directory. See :func:`manifest_builder.api.generate` for the full argument
    documentation.
    """
    # Keep this wrapper lazy: api imports __version__ from this module.
    from manifest_builder.api import generate as api_generate

    return api_generate(
        config,
        output,
        repo_root,
        verbose,
        create_commit,
        allow_dirty_config,
        vars_from=vars_from,
        namespace=namespace,
        image=image,
        vars=vars,
        target=target,
        plugins=plugins,
    )


__all__ = [
    "ExternalPlugins",
    "GenerationResult",
    "KubernetesObjectRef",
    "__version__",
    "generate",
]
