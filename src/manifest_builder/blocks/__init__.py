# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""The config block interface, and the bundled block implementations.

Each module in this package owns one top-level TOML key: its config
dataclass, the parser that builds it, its validation, and the
:class:`ConfigBlock` subclass that ties them together. Those modules depend on
the shared toolkit (config, k8s, output) and never on each other.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from manifest_builder.config import ManifestConfig
from manifest_builder.helm import ChartCacheStats

if TYPE_CHECKING:
    from manifest_builder.helmfile import Helmfile


@dataclass(frozen=True)
class GenerationContext:
    """Shared inputs needed by concrete config blocks during generation."""

    output_dir: Path
    repo_root: Path
    charts_dir: Path
    verbose: bool = False
    images: dict[str, str] | None = None
    cache_stats: ChartCacheStats | None = None


class ConfigBlock[ConfigT: ManifestConfig](ABC):
    """Base class for config-type-specific manifest generation.

    Parameterized with the config dataclass the block owns, so ``validate``
    and ``generate`` receive that type directly:

        class SimpleBlock(ConfigBlock[SimpleConfig]):
            ...
    """

    #: Whether generate() for one config is independent of this block's
    #: others. Set True to let the orchestrator render them concurrently.
    parallel_safe: bool = False

    #: Upper bound on concurrent generate() calls when parallel_safe is set.
    max_workers: int = 8

    @abstractmethod
    def top_level_config_name(self) -> str:
        """Return the top-level TOML key this block owns."""

    @abstractmethod
    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        """Parse this block's raw TOML subtree."""

    @abstractmethod
    def iter_configs(self) -> Iterable[ConfigT]:
        """Yield the parsed configs this block is responsible for."""

    def resolve(self, helmfile: "Helmfile | None") -> None:
        """Resolve references after all blocks have parsed their configs."""

    @abstractmethod
    def validate(self, config: ConfigT, repo_root: Path) -> None:
        """Validate a config before any manifests are generated."""

    @abstractmethod
    def generate(
        self,
        config: ConfigT,
        context: GenerationContext,
    ) -> set[Path]:
        """Generate manifests for a config and return paths that were written."""
