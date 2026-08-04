# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Config handler interfaces for manifest generation."""

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
    """Shared inputs needed by concrete config handlers during generation."""

    output_dir: Path
    repo_root: Path
    charts_dir: Path
    verbose: bool = False
    images: dict[str, str] | None = None
    cache_stats: ChartCacheStats | None = None


class ConfigHandler[ConfigT: ManifestConfig](ABC):
    """Base class for config-type-specific manifest generation.

    Parameterized with the config dataclass the handler owns, so ``validate``
    and ``generate`` receive that type directly:

        class SimpleConfigHandler(ConfigHandler[SimpleConfig]):
            ...
    """

    #: Whether generate() for one config is independent of this handler's
    #: others. Set True to let the orchestrator render them concurrently.
    parallel_safe: bool = False

    #: Upper bound on concurrent generate() calls when parallel_safe is set.
    max_workers: int = 8

    @abstractmethod
    def top_level_config_name(self) -> str:
        """Return the top-level TOML key this handler owns."""

    @abstractmethod
    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        """Parse this handler's raw TOML subtree."""

    @abstractmethod
    def iter_configs(self) -> Iterable[ConfigT]:
        """Yield the parsed configs this handler is responsible for."""

    def resolve(self, helmfile: "Helmfile | None") -> None:
        """Resolve references after all handlers have parsed their configs."""

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
