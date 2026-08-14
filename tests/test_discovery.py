# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for config block discovery."""

import logging
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from manifest_builder.discovery import (
    PLUGINS_PACKAGE,
    _quoted_list,
    discover_blocks,
)
from manifest_builder.k8s import declare_scopes, declared_scopes, is_cluster_scoped

# A plugin defining one config block, mirroring what a config repo would ship.
GREETING_PLUGIN = '''
"""A [[greeting]] config block."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.output import write_documents


@dataclass
class GreetingConfig:
    name: str
    namespace: str
    message: str = "hello"


class GreetingBlock(ConfigBlock[GreetingConfig]):
    def __init__(self, configs: Sequence[GreetingConfig] | None = None) -> None:
        self.configs = list(configs or [])

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
        assert isinstance(data, list)
        for item in data:
            self.configs.append(
                GreetingConfig(
                    name=item["name"],
                    namespace=item.get("namespace", item["name"]),
                    message=item.get("message", "hello"),
                )
            )

    def iter_configs(self) -> list[GreetingConfig]:
        return self.configs

    def validate(self, config: GreetingConfig, repo_root: Path) -> None:
        pass

    def generate(
        self, config: GreetingConfig, context: GenerationContext
    ) -> set[Path]:
        doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config.name},
            "data": {"message": config.message},
        }
        return write_documents([doc], context.output_dir, config.namespace, config.name)
'''

BUILTIN_KEYS = ["copy"]

# A plugin declaring resource scopes from a TOML file beside it, which is how a
# config repo names a kind without a release of this package.
SCOPES_PLUGIN = '''
"""Resource scopes for kinds no rendered CRD describes."""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from manifest_builder.k8s import ScopeProvider

SCOPES_FILE = Path(__file__).parent / "resource-scopes.toml"


class TomlScopes(ScopeProvider):
    def scopes(self) -> Mapping[tuple[str, str], str]:
        data = tomllib.loads(SCOPES_FILE.read_text())
        return {
            (group, kind): scope
            for group, kinds in data.items()
            for kind, scope in kinds.items()
        }
'''


@pytest.fixture(autouse=True)
def _clear_plugin_imports():
    """Keep plugin modules and declared scopes from leaking between tests."""
    yield
    for name in [
        name
        for name in sys.modules
        if name == PLUGINS_PACKAGE or name.startswith(f"{PLUGINS_PACKAGE}.")
    ]:
        del sys.modules[name]
    declare_scopes({})


def _write_plugin(config_dir: Path, name: str, source: str) -> Path:
    plugins = config_dir / "plugins"
    plugins.mkdir(exist_ok=True)
    path = plugins / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    return path


def test_discovers_builtin_blocks() -> None:
    keys = [block.top_level_config_name() for block in discover_blocks()]
    assert keys == BUILTIN_KEYS


def test_discovery_is_sorted_by_config_key() -> None:
    """Ordering must not depend on filesystem or import order."""
    keys = [block.top_level_config_name() for block in discover_blocks()]
    assert keys == sorted(keys)


def test_config_dir_without_plugins_yields_only_builtins(tmp_path: Path) -> None:
    keys = [block.top_level_config_name() for block in discover_blocks(tmp_path)]
    assert keys == BUILTIN_KEYS


def test_discovers_a_plugin_from_the_config_dir(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)

    blocks = discover_blocks(tmp_path)
    keys = [block.top_level_config_name() for block in blocks]

    assert keys == sorted([*BUILTIN_KEYS, "greeting"])


def test_plugin_modules_do_not_shadow_installed_modules(tmp_path: Path) -> None:
    """A plugin named after a stdlib module must not become 'import json'."""
    _write_plugin(tmp_path, "json", GREETING_PLUGIN)

    discover_blocks(tmp_path)

    import json

    assert json.dumps({"a": 1}) == '{"a": 1}'
    assert not hasattr(json, "GreetingBlock")


def test_plugin_can_import_a_sibling_module(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "shared", "MESSAGE = 'from sibling'\n")
    _write_plugin(
        tmp_path,
        "greeting",
        GREETING_PLUGIN.replace(
            'message: str = "hello"',
            "message: str = MESSAGE",
        ).replace(
            '"""A [[greeting]] config block."""',
            '"""A [[greeting]] config block."""\n\nfrom .shared import MESSAGE',
        ),
    )

    blocks = {h.top_level_config_name(): h for h in discover_blocks(tmp_path)}

    assert "greeting" in blocks


def test_discovers_a_plugin_from_an_external_directory(tmp_path: Path) -> None:
    """Config from a repo without its plugins can be given them separately."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    external = tmp_path / "elsewhere" / "plugins"
    external.mkdir(parents=True)
    (external / "greeting.py").write_text(textwrap.dedent(GREETING_PLUGIN))

    keys = [h.top_level_config_name() for h in discover_blocks(config_dir, external)]

    assert keys == sorted([*BUILTIN_KEYS, "greeting"])


def test_external_plugins_are_added_to_the_config_dir_plugins(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_plugin(config_dir, "greeting", GREETING_PLUGIN)
    external = tmp_path / "elsewhere" / "plugins"
    external.mkdir(parents=True)
    (external / "farewell.py").write_text(
        textwrap.dedent(
            GREETING_PLUGIN.replace("greeting", "farewell").replace(
                "Greeting", "Farewell"
            )
        )
    )

    keys = [h.top_level_config_name() for h in discover_blocks(config_dir, external)]

    assert keys == sorted([*BUILTIN_KEYS, "greeting", "farewell"])


def test_the_same_plugin_module_name_in_both_directories_is_rejected(
    tmp_path: Path,
) -> None:
    """One directory silently shadowing the other would be hard to spot."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_plugin(config_dir, "greeting", GREETING_PLUGIN)
    external = tmp_path / "elsewhere" / "plugins"
    external.mkdir(parents=True)
    (external / "greeting.py").write_text(textwrap.dedent(GREETING_PLUGIN))

    with pytest.raises(ValueError, match="Plugin module 'greeting' is present in both"):
        discover_blocks(config_dir, external)


def test_a_missing_external_plugins_directory_is_an_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with pytest.raises(ValueError, match="Plugins directory does not exist"):
        discover_blocks(config_dir, tmp_path / "nowhere")


def test_external_plugins_without_a_config_dir(tmp_path: Path) -> None:
    external = tmp_path / "plugins"
    external.mkdir()
    (external / "greeting.py").write_text(textwrap.dedent(GREETING_PLUGIN))

    keys = [h.top_level_config_name() for h in discover_blocks(None, external)]

    assert keys == sorted([*BUILTIN_KEYS, "greeting"])


def test_underscored_and_hidden_files_are_skipped(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "_helpers", "raise AssertionError('must not import')\n")
    (tmp_path / "plugins" / "notes.txt").write_text("not python\n")

    keys = [block.top_level_config_name() for block in discover_blocks(tmp_path)]

    assert keys == BUILTIN_KEYS


@pytest.mark.parametrize("name", ["test_greeting", "greeting_test", "conftest"])
def test_test_modules_beside_plugins_are_not_imported(
    tmp_path: Path, name: str
) -> None:
    """A plugin dir may keep its own tests; discovery must leave them alone."""
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    _write_plugin(tmp_path, name, "raise AssertionError('must not import')\n")

    keys = [block.top_level_config_name() for block in discover_blocks(tmp_path)]

    assert keys == sorted([*BUILTIN_KEYS, "greeting"])


@pytest.mark.parametrize("dirname", ["tests", "test"])
def test_a_tests_package_is_not_imported_as_a_plugin(
    tmp_path: Path, dirname: str
) -> None:
    """A tests directory is skipped even when it is an importable package."""
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    tests_dir = tmp_path / "plugins" / dirname
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("raise AssertionError('must not import')\n")

    keys = [block.top_level_config_name() for block in discover_blocks(tmp_path)]

    assert keys == sorted([*BUILTIN_KEYS, "greeting"])


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], ""),
        (["a"], '"a"'),
        (["a", "b"], '"a" and "b"'),
        (["a", "b", "c"], '"a", "b", and "c"'),
    ],
)
def test_quoted_list_reads_as_english(names: list[str], expected: str) -> None:
    assert _quoted_list(names) == expected


def test_logs_a_summary_of_detected_plugins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    caplog.set_level(logging.INFO, logger="manifest_builder.discovery")

    discover_blocks(tmp_path)

    assert 'Found 1 plugin, now handling config block type "greeting"' in caplog.text


def test_plugin_summary_lists_every_block_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for key in ("greeting", "farewell", "welcome"):
        _write_plugin(
            tmp_path,
            key,
            GREETING_PLUGIN.replace("greeting", key).replace(
                "Greeting", key.capitalize()
            ),
        )
    caplog.set_level(logging.INFO, logger="manifest_builder.discovery")

    discover_blocks(tmp_path)

    assert (
        "Found 3 plugins, now handling config block types "
        '"farewell", "greeting", and "welcome"' in caplog.text
    )


def test_no_plugin_summary_without_plugins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A config dir with no plugins should not log about them at all."""
    caplog.set_level(logging.INFO, logger="manifest_builder.discovery")

    discover_blocks(tmp_path)

    assert "plugin" not in caplog.text


def test_broken_plugin_reports_its_path(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "broken", "import nonexistent_module_xyz\n")

    with pytest.raises(ValueError, match="Failed to load plugin 'broken'"):
        discover_blocks(tmp_path)


def test_plugin_claiming_a_builtin_key_is_rejected(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path, "clash", GREETING_PLUGIN.replace('return "greeting"', 'return "copy"')
    )

    with pytest.raises(ValueError, match="both claim the top-level key 'copy'"):
        discover_blocks(tmp_path)


def test_switching_config_dir_reloads_plugins(tmp_path: Path) -> None:
    """Two config dirs in one process must not see each other's plugins."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_plugin(first, "greeting", GREETING_PLUGIN)
    _write_plugin(
        second,
        "farewell",
        GREETING_PLUGIN.replace("greeting", "farewell").replace("Greeting", "Farewell"),
    )

    first_keys = [h.top_level_config_name() for h in discover_blocks(first)]
    second_keys = [h.top_level_config_name() for h in discover_blocks(second)]

    assert "greeting" in first_keys and "farewell" not in first_keys
    assert "farewell" in second_keys and "greeting" not in second_keys


def test_replaced_plugin_module_is_reloaded(tmp_path: Path) -> None:
    """A checkout at the same path must not serve the previous plugin's code."""
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    first = discover_blocks(tmp_path)
    assert "greeting" in [h.top_level_config_name() for h in first]

    # Same path, new content, as a fresh checkout over the top would leave it.
    _write_plugin(
        tmp_path,
        "greeting",
        GREETING_PLUGIN.replace('return "greeting"', 'return "hail"'),
    )
    second = discover_blocks(tmp_path)

    keys = [h.top_level_config_name() for h in second]
    assert "hail" in keys
    assert "greeting" not in keys


def test_plugin_added_after_a_previous_scan_is_found(tmp_path: Path) -> None:
    """Import-system directory caches must not hide a newly added plugin."""
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    discover_blocks(tmp_path)

    _write_plugin(
        tmp_path,
        "farewell",
        GREETING_PLUGIN.replace("greeting", "farewell").replace("Greeting", "Farewell"),
    )
    keys = [h.top_level_config_name() for h in discover_blocks(tmp_path)]

    assert "farewell" in keys and "greeting" in keys


def test_plugin_removed_after_a_previous_scan_is_gone(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)
    discover_blocks(tmp_path)

    (tmp_path / "plugins" / "greeting.py").unlink()
    keys = [h.top_level_config_name() for h in discover_blocks(tmp_path)]

    assert keys == BUILTIN_KEYS


def test_plugin_import_writes_no_bytecode_cache(tmp_path: Path) -> None:
    """__pycache__ in the config checkout would both litter and go stale."""
    _write_plugin(tmp_path, "greeting", GREETING_PLUGIN)

    discover_blocks(tmp_path)

    assert not list((tmp_path / "plugins").glob("**/__pycache__"))
    assert not sys.dont_write_bytecode


def test_repeated_generation_picks_up_new_plugin_and_template(tmp_path: Path) -> None:
    """The long-running-process case: regenerate after the config is replaced.

    Both the plugin module and the templates it reads must come from the new
    checkout, not from whatever the process imported on a previous run.
    """
    from manifest_builder import generate

    config_dir = tmp_path / "config"
    templates = config_dir / "plugins" / "templates"
    output = tmp_path / "out"
    config_dir.mkdir()
    output.mkdir()
    (config_dir / "config.toml").write_text('[[greeting]]\nname = "welcomer"\n')

    # A plugin that renders its ConfigMap body from a template file on disk.
    templated_plugin = GREETING_PLUGIN.replace(
        '"data": {"message": config.message},',
        '"data": {"message": (Path(__file__).parent / "templates" / "greeting.txt")'
        ".read_text().strip()},",
    ).replace('return "greeting"', 'return "greeting"  # v1')
    _write_plugin(config_dir, "greeting", templated_plugin)
    templates.mkdir()
    (templates / "greeting.txt").write_text("first checkout\n")

    generate(config=config_dir, output=output, repo_root=tmp_path)
    manifest = output / "welcomer" / "configmap-welcomer.yaml"
    assert yaml.safe_load(manifest.read_text())["data"]["message"] == "first checkout"

    # Simulate relcoord checking out a new config revision over the same path:
    # the template changes, and so does the module's own behaviour.
    (templates / "greeting.txt").write_text("second checkout\n")
    _write_plugin(
        config_dir,
        "greeting",
        templated_plugin.replace(
            '"metadata": {"name": config.name},',
            '"metadata": {"name": config.name + "-v2"},',
        ),
    )

    generate(config=config_dir, output=output, repo_root=tmp_path)

    updated = output / "welcomer" / "configmap-welcomer-v2.yaml"
    assert updated.exists(), "plugin module was not reloaded from the new checkout"
    assert yaml.safe_load(updated.read_text())["data"]["message"] == "second checkout"


def test_concurrent_discovery_from_two_config_dirs(tmp_path: Path) -> None:
    """Parallel loads must not interleave into each other's plugin set."""
    from concurrent.futures import ThreadPoolExecutor

    dirs = []
    for key in ("greeting", "farewell"):
        config_dir = tmp_path / key
        config_dir.mkdir()
        _write_plugin(
            config_dir,
            key,
            GREETING_PLUGIN.replace("greeting", key).replace(
                "Greeting", key.capitalize()
            ),
        )
        dirs.append((key, config_dir))

    def load(item: tuple[str, Path]) -> tuple[str, list[str]]:
        key, config_dir = item
        return key, [h.top_level_config_name() for h in discover_blocks(config_dir)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(load, dirs * 8))

    for key, keys in results:
        assert keys == sorted([*BUILTIN_KEYS, key]), f"{key} saw {keys}"


def test_plugin_block_generates_manifests_end_to_end(tmp_path: Path) -> None:
    """A plugin block loads from config.toml and writes its manifests."""
    from manifest_builder import generate

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_plugin(config_dir, "greeting", GREETING_PLUGIN)
    (config_dir / "config.toml").write_text(
        textwrap.dedent("""
            [[greeting]]
            name = "welcomer"
            message = "hi from a plugin"
        """)
    )
    output = tmp_path / "out"
    output.mkdir()

    generate(config=config_dir, output=output, repo_root=tmp_path)

    manifest = output / "welcomer" / "configmap-welcomer.yaml"
    assert manifest.exists()
    assert yaml.safe_load(manifest.read_text())["data"]["message"] == "hi from a plugin"


# ---------------------------------------------------------------------------
# Plugin-declared resource scopes
# ---------------------------------------------------------------------------


def _write_scopes(config_dir: Path, toml: str) -> None:
    _write_plugin(config_dir, "resource_scopes", SCOPES_PLUGIN)
    (config_dir / "plugins" / "resource-scopes.toml").write_text(textwrap.dedent(toml))


def test_a_plugin_can_declare_a_scope_from_a_toml_file(tmp_path: Path) -> None:
    """The whole point: a kind is named in config, not in this package."""
    _write_scopes(
        tmp_path,
        """\
        ["aws.upbound.io"]
        ProviderConfig = "Cluster"
        """,
    )

    discover_blocks(tmp_path)

    assert declared_scopes() == {("aws.upbound.io", "ProviderConfig"): "Cluster"}
    doc = {"apiVersion": "aws.upbound.io/v1beta1", "kind": "ProviderConfig"}
    assert is_cluster_scoped(doc, {}) is True


def test_declared_scopes_distinguish_api_groups(tmp_path: Path) -> None:
    """The case the bare-kind set cannot express.

    aws.upbound.io/ProviderConfig is cluster-scoped while
    aws.m.upbound.io/ProviderConfig is namespaced, so one kind name has to
    resolve two ways in the same run.
    """
    _write_scopes(
        tmp_path,
        """\
        ["aws.upbound.io"]
        ProviderConfig = "Cluster"

        ["aws.m.upbound.io"]
        ProviderConfig = "Namespaced"
        """,
    )

    discover_blocks(tmp_path)

    legacy = {"apiVersion": "aws.upbound.io/v1beta1", "kind": "ProviderConfig"}
    scoped = {"apiVersion": "aws.m.upbound.io/v1beta1", "kind": "ProviderConfig"}
    assert is_cluster_scoped(legacy, {}) is True
    assert is_cluster_scoped(scoped, {}) is False


def test_a_rendered_crd_still_beats_a_declared_scope(tmp_path: Path) -> None:
    """Precedence: what a chart ships wins over what config declares."""
    _write_scopes(
        tmp_path,
        """\
        ["example.invalid"]
        Widget = "Cluster"
        """,
    )
    discover_blocks(tmp_path)

    doc = {"apiVersion": "example.invalid/v1", "kind": "Widget"}
    crd_scopes = {("example.invalid", "Widget"): "Namespaced"}
    assert is_cluster_scoped(doc, crd_scopes) is False


def test_a_declared_scope_beats_the_static_kind_set(tmp_path: Path) -> None:
    """A declared Namespaced scope overrides CLUSTER_SCOPED_KINDS.

    Provider is in the static set for crossplane's sake, but another group's
    Provider may well be namespaced, and config gets to say so.
    """
    _write_scopes(
        tmp_path,
        """\
        ["example.invalid"]
        Provider = "Namespaced"
        """,
    )
    discover_blocks(tmp_path)

    assert (
        is_cluster_scoped({"apiVersion": "example.invalid/v1", "kind": "Provider"}, {})
        is False
    )
    # crossplane's own Provider is untouched.
    assert (
        is_cluster_scoped(
            {"apiVersion": "pkg.crossplane.io/v1", "kind": "Provider"}, {}
        )
        is True
    )


def test_scopes_are_replaced_between_runs(tmp_path: Path) -> None:
    """One config directory's declarations must not leak into the next.

    A long-running process generates from many config directories, so scopes are
    replaced per discovery rather than accumulated.
    """
    first = tmp_path / "first"
    first.mkdir()
    _write_scopes(first, '["a.invalid"]\nWidget = "Cluster"\n')
    discover_blocks(first)
    assert ("a.invalid", "Widget") in declared_scopes()

    second = tmp_path / "second"
    second.mkdir()
    _write_scopes(second, '["b.invalid"]\nGadget = "Cluster"\n')
    discover_blocks(second)

    assert declared_scopes() == {("b.invalid", "Gadget"): "Cluster"}


def test_a_config_dir_without_plugins_clears_declared_scopes(tmp_path: Path) -> None:
    with_plugins = tmp_path / "with"
    with_plugins.mkdir()
    _write_scopes(with_plugins, '["a.invalid"]\nWidget = "Cluster"\n')
    discover_blocks(with_plugins)
    assert declared_scopes()

    bare = tmp_path / "bare"
    bare.mkdir()
    discover_blocks(bare)

    assert declared_scopes() == {}


def test_an_unknown_scope_value_is_rejected(tmp_path: Path) -> None:
    _write_scopes(tmp_path, '["a.invalid"]\nWidget = "Clustered"\n')

    with pytest.raises(ValueError, match="must be one of Cluster, Namespaced"):
        discover_blocks(tmp_path)


def test_scope_providers_that_disagree_are_rejected(tmp_path: Path) -> None:
    """Letting import order decide would make generation unpredictable."""
    _write_scopes(tmp_path, '["a.invalid"]\nWidget = "Cluster"\n')
    _write_plugin(
        tmp_path,
        "other_scopes",
        """
        from collections.abc import Mapping

        from manifest_builder.k8s import ScopeProvider


        class OtherScopes(ScopeProvider):
            def scopes(self) -> Mapping[tuple[str, str], str]:
                return {("a.invalid", "Widget"): "Namespaced"}
        """,
    )

    with pytest.raises(
        ValueError, match="disagree about the scope of a.invalid/Widget"
    ):
        discover_blocks(tmp_path)


def test_a_scope_only_plugin_counts_as_contributing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A plugin declaring only scopes is still a plugin that was loaded."""
    _write_scopes(tmp_path, '["a.invalid"]\nWidget = "Cluster"\n')

    with caplog.at_level(logging.INFO):
        discover_blocks(tmp_path)

    assert "Found 1 plugin" in caplog.text
