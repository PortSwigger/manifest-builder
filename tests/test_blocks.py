# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for the config block interface."""

from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Lock
from time import sleep
from typing import Any, cast

import pytest
import yaml

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.generator import generate_manifests
from manifest_builder.output import write_documents


@dataclass
class GreetingConfig:
    """A config type declared outside manifest_builder.config."""

    name: str
    namespace: str
    message: str = "hello"


class GreetingBlock(ConfigBlock[GreetingConfig]):
    """A block for a config type the library does not know about."""

    def __init__(self, configs: list[GreetingConfig] | None = None) -> None:
        self.configs = list(configs or [])
        self.validated: list[str] = []

    def top_level_config_name(self) -> str:
        return "greeting"

    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        if not isinstance(data, list):
            raise ValueError(f"'greeting' must be a list of tables in {source_file}")
        for item in data:
            entry = cast(dict[str, Any], item)
            self.configs.append(
                GreetingConfig(
                    name=entry["name"],
                    namespace=entry.get("namespace", default_namespace or "default"),
                    message=entry.get("message", "hello"),
                )
            )

    def iter_configs(self) -> list[GreetingConfig]:
        return self.configs

    def validate(self, config: GreetingConfig, repo_root: Path) -> None:
        self.validated.append(config.name)

    def generate(self, config: GreetingConfig, context: GenerationContext) -> set[Path]:
        doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config.name},
            "data": {"message": config.message},
        }
        return write_documents([doc], context.output_dir, config.namespace, config.name)


def test_generate_manifests_accepts_an_out_of_tree_config_type(tmp_path: Path) -> None:
    """A block may bring its own config dataclass, unknown to config.py."""
    block = GreetingBlock(
        [GreetingConfig(name="greeter", namespace="demo", message="hi there")]
    )

    written = generate_manifests([block], tmp_path / "out", repo_root=tmp_path)

    manifest = tmp_path / "out" / "demo" / "configmap-greeter.yaml"
    assert manifest in written
    assert yaml.safe_load(manifest.read_text())["data"]["message"] == "hi there"
    assert block.validated == ["greeter"]


def test_generate_manifests_reports_out_of_tree_validation_failure(
    tmp_path: Path,
) -> None:
    """Validation raised by a custom block stops generation."""

    class RejectingBlock(GreetingBlock):
        def validate(self, config: GreetingConfig, repo_root: Path) -> None:
            raise ValueError(f"{config.name} is not welcome")

    block = RejectingBlock([GreetingConfig(name="greeter", namespace="demo")])

    with pytest.raises(ValueError, match="greeter is not welcome"):
        generate_manifests([block], tmp_path / "out", repo_root=tmp_path)


class _ConcurrencyProbe(GreetingBlock):
    """Records how many generate() calls were ever in flight at once."""

    def __init__(self, configs: list[GreetingConfig], expected_overlap: int) -> None:
        super().__init__(configs)
        self._barrier = Barrier(expected_overlap)
        self._lock = Lock()
        self.active = 0
        self.max_active = 0

    def generate(self, config: GreetingConfig, context: GenerationContext) -> set[Path]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            # Times out unless expected_overlap calls arrive together.
            self._barrier.wait(timeout=2)
        except BrokenBarrierError:
            pass
        sleep(0.01)
        with self._lock:
            self.active -= 1
        return super().generate(config, context)


def test_parallel_safe_block_generates_concurrently(tmp_path: Path) -> None:
    """A block opting into parallel_safe has its configs rendered together."""
    configs = [
        GreetingConfig(name=name, namespace="demo") for name in ("first", "second")
    ]
    block = _ConcurrencyProbe(configs, expected_overlap=2)
    block.parallel_safe = True

    generate_manifests([block], tmp_path / "out", repo_root=tmp_path)

    assert block.max_active == 2


def test_blocks_are_sequential_by_default(tmp_path: Path) -> None:
    """Without parallel_safe, generate() is called one config at a time."""
    configs = [
        GreetingConfig(name=name, namespace="demo") for name in ("first", "second")
    ]
    # expected_overlap=1 so the barrier never blocks; sequencing is what is
    # under test, and a concurrent run would still be caught by max_active.
    block = _ConcurrencyProbe(configs, expected_overlap=1)

    assert block.parallel_safe is False
    generate_manifests([block], tmp_path / "out", repo_root=tmp_path)

    assert block.max_active == 1
