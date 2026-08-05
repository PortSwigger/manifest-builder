# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Shared test helpers."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dulwich import porcelain
from dulwich.repo import Repo

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.config import TemplateValue, parse_variables
from manifest_builder.output import load_all_yaml, write_documents


def init_test_repo(path: Path) -> Repo:
    """Initialize a Dulwich repo with commit signing disabled.

    The user's global git config may set ``commit.gpgsign = true``; that
    invokes gpg during ``porcelain.commit``, which fails in CI / sandboxed
    test environments. Disable it at the per-repo level so tests don't
    depend on the host's gpg setup.
    """
    repo = porcelain.init(path)
    config = repo.get_config()
    config.set((b"commit",), b"gpgsign", b"false")
    config.set((b"tag",), b"gpgsign", b"false")
    config.write_to_path()
    return repo


@dataclass
class ProbeConfig:
    """Config for the probe block: documents to write, and what was parsed."""

    name: str
    namespace: str | None = None
    #: Raw YAML stream this config writes when generated.
    documents: str = ""
    image: str | None = None
    #: A path field, so tests can assert where a relative path in a config
    #: file resolves to.
    source: Path | None = None
    #: Chart cache hits to report, for the run summary the generator logs.
    cache_hits: int = 0
    variables: dict[str, TemplateValue] = field(default_factory=dict)


class ProbeBlock(ConfigBlock[ProbeConfig]):
    """A block that exists to exercise the core, not to render anything real.

    Core behaviour — config loading, target and section merging, namespace
    ownership, the generation summary — used to be tested through the helm and
    simple blocks. Those live with the configuration directories that use them
    now, so the core tests drive this instead: it accepts a ``[[probe]]`` table,
    records what loading handed it, and writes back whatever documents it was
    given.
    """

    def __init__(self, configs: Sequence[ProbeConfig] | None = None) -> None:
        self.configs = list(configs or [])
        #: Arguments the most recent load_config() call received.
        self.load_calls: list[dict[str, Any]] = []
        self.validated: list[str] = []
        self.resolved_with: list[Any] = []

    def top_level_config_name(self) -> str:
        return "probe"

    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        self.load_calls.append(
            {
                "source_file": source_file,
                "default_namespace": default_namespace,
                "default_image": default_image,
            }
        )
        if not isinstance(data, list):
            raise ValueError(f"'probe' must be a list of tables in {source_file}")

        variables = parse_variables(root_config.get("variables"), source_file)
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[probe]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_probe_config(
                    item, source_file, variables, default_namespace, default_image
                )
            )

    def iter_configs(self) -> list[ProbeConfig]:
        return self.configs

    def resolve(self, helmfile: Any) -> None:
        self.resolved_with.append(helmfile)

    def validate(self, config: ProbeConfig, repo_root: Path) -> None:
        self.validated.append(config.name)

    def generate(
        self,
        config: ProbeConfig,
        context: GenerationContext,
    ) -> set[Path]:
        if config.cache_hits and context.cache_stats is not None:
            context.cache_stats.hits += config.cache_hits
        return write_documents(
            load_all_yaml(config.documents),
            context.output_dir,
            config.namespace,
            app_name=config.name,
        )


def _parse_probe_config(
    data: dict,
    source_file: Path,
    variables: dict[str, TemplateValue],
    default_namespace: str | None,
    default_image: str | None,
) -> ProbeConfig:
    """Parse one [[probe]] table."""
    namespace = data.get("namespace", default_namespace)
    source = data.get("source")
    return ProbeConfig(
        name=data.get("name") or namespace or "probe",
        namespace=namespace,
        documents=data.get("documents", ""),
        image=data.get("image", default_image),
        source=source_file.parent / source if source else None,
        variables=variables,
    )
