# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for configuration parsing and validation."""

import re
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import ProbeBlock, ProbeConfig

from manifest_builder.blocks import ConfigBlock
from manifest_builder.blocks.copy import CopyBlock, CopyConfig
from manifest_builder.config import (
    ManifestConfig,
    load_configs,
    load_extra_variables,
    load_images,
    load_owned_namespaces,
    resolve_configs,
)
from manifest_builder.discovery import discover_blocks


def write_toml(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(content))
    return path


def all_configs(
    blocks: Sequence[ConfigBlock],
) -> tuple[ManifestConfig, ...]:
    return tuple(config for block in blocks for config in block.iter_configs())


def only_config(
    blocks: Sequence[ConfigBlock],
) -> ManifestConfig:
    (config,) = all_configs(blocks)
    return config


def config_blocks() -> list[ConfigBlock[Any]]:
    """The blocks a real run finds, plus the probe block.

    Only ``copy`` ships with this package; the probe block stands in for the
    ones a configuration directory brings, so loading behaviour that is not
    specific to any block can still be exercised against two blocks.
    """
    return [*discover_blocks(), ProbeBlock()]


def load_test_configs(
    config_dir: Path,
) -> Sequence[ConfigBlock]:
    return load_configs(config_dir, config_blocks())


def manifest_configs(
    *,
    probes: list[ProbeConfig] | None = None,
    copies: list[CopyConfig] | None = None,
) -> list[ConfigBlock[Any]]:
    return [
        CopyBlock(copies),
        ProbeBlock(probes),
    ]


def test_load_config_unknown_field_reports_correct_table_occurrence(
    tmp_path: Path,
) -> None:
    """Unknown field line numbers should match the table occurrence that failed."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"

        [[copy]]
        namespace = "staging"
        source = "other"
        name = "other"
        value = ["values.yaml"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[copy\]\]: 'value' on line 10",
    ):
        load_test_configs(conf_dir)


def test_extra_variables_are_merged_into_helm_configs(tmp_path: Path) -> None:
    """Variables loaded from --vars-from are merged with config.toml variables."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [variables]
        domain = "example.com"

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    configs = load_configs(
        conf_dir,
        config_blocks(),
        extra_variables={"cluster_name": "prod", "replica_count": 3},
    )
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.variables == {
        "domain": "example.com",
        "cluster_name": "prod",
        "replica_count": 3,
    }


def test_extra_variables_without_config_variables(tmp_path: Path) -> None:
    """extra_variables become the variable set when config.toml has none."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    configs = load_configs(
        conf_dir,
        config_blocks(),
        extra_variables={"cluster_name": "prod"},
    )
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.variables == {"cluster_name": "prod"}


def test_extra_variables_conflict_raises(tmp_path: Path) -> None:
    """Overlapping keys between config.toml and extra_variables raise ValueError."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [variables]
        domain = "example.com"

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(ValueError, match="'domain'"):
        load_configs(
            conf_dir,
            config_blocks(),
            extra_variables={"domain": "other.com"},
        )


def test_load_extra_variables_reads_top_level_keys(tmp_path: Path) -> None:
    vars_file = tmp_path / "vars.toml"
    vars_file.write_text(
        textwrap.dedent(
            """\
            domain = "example.com"
            replica_count = 3
            use_tls = true
            """
        )
    )

    assert load_extra_variables(vars_file) == {
        "domain": "example.com",
        "replica_count": 3,
        "use_tls": True,
    }


def test_load_extra_variables_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Variables file not found"):
        load_extra_variables(tmp_path / "nope.toml")


def test_load_extra_variables_rejects_nested_table(tmp_path: Path) -> None:
    vars_file = tmp_path / "vars.toml"
    vars_file.write_text(
        textwrap.dedent(
            """\
            [nested]
            foo = "bar"
            """
        )
    )

    with pytest.raises(ValueError, match="must be a string, number, or boolean"):
        load_extra_variables(vars_file)


# ---------------------------------------------------------------------------


def test_load_configs_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_test_configs(tmp_path / "nonexistent")


def test_load_configs_missing_config_toml(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        load_test_configs(conf)


def test_load_configs_invalid_toml_names_the_file(tmp_path: Path) -> None:
    """A syntax error in the top-level config names the offending file."""
    conf = tmp_path / "conf"
    conf.mkdir()
    config_file = conf / "config.toml"
    # Unterminated string on the value.
    config_file.write_text('[variables]\nfoo = "unterminated\n')

    with pytest.raises(
        ValueError, match=rf"Failed to parse TOML file {re.escape(str(config_file))}"
    ):
        load_test_configs(conf)


def test_load_extra_variables_invalid_toml_names_the_file(tmp_path: Path) -> None:
    """A syntax error in a --vars-from file names the offending file."""
    vars_file = tmp_path / "vars.toml"
    vars_file.write_text('foo = "unterminated\n')

    with pytest.raises(
        ValueError, match=rf"Failed to parse TOML file {re.escape(str(vars_file))}"
    ):
        load_extra_variables(vars_file)


def test_load_configs_accepts_manifest_builder_toml(tmp_path: Path) -> None:
    """The top-level config file can be named manifest-builder.toml."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "manifest-builder.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    config = only_config(load_test_configs(conf))

    assert isinstance(config, CopyConfig)
    assert config.name == "myapp"


def test_load_configs_prefers_config_toml(tmp_path: Path) -> None:
    """Existing config.toml behavior is preserved when both names are present."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "from-config-toml"
        """,
    )
    write_toml(
        conf,
        "manifest-builder.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "from-manifest-builder-toml"
        """,
    )

    config = only_config(load_test_configs(conf))

    assert isinstance(config, CopyConfig)
    assert config.name == "from-config-toml"


def test_load_configs_no_recognized_tables(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [metadata]
        description = "this file has no recognized config tables"
        """,
    )
    with pytest.raises(
        ValueError,
        match="Unknown top-level field: 'metadata' on line 1",
    ):
        load_test_configs(conf)


def test_load_configs_no_registered_block_tables(tmp_path: Path) -> None:
    """Config loading fails when none of the registered block lists is present."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[probe]]
        namespace = "default"
        name = "myapp"
        """,
    )
    with pytest.raises(
        ValueError,
        match="Unknown top-level field: 'probe' on line 1",
    ):
        load_configs(conf, [CopyBlock()])


def test_load_configs_without_blocks_raises(tmp_path: Path) -> None:
    """Config loading needs at least one block to define known config lists."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )
    with pytest.raises(ValueError, match="No config blocks registered"):
        load_configs(conf, [])


def test_load_configs_uses_only_config_toml(tmp_path: Path) -> None:
    """Sibling TOML files are reserved for other semantics and not parsed here."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[copy]]
        namespace = "ns-a"
        source = "a"
        name = "app-a"
        """,
    )
    write_toml(
        conf,
        "other.toml",
        """\
        [[copy]]
        namespace = "ns-b"
        source = "b"
        name = "app-b"
        """,
    )

    configs = load_test_configs(conf)
    names = {c.name for c in all_configs(configs)}
    assert names == {"app-a"}


def test_load_configs_mixed_blocks(tmp_path: Path) -> None:
    """One config file may declare tables owned by different blocks."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "my-copy-app"

        [[probe]]
        name = "my-probe-app"
        namespace = "apps"
        image = "example.com/my-app:1.0"
        """,
    )

    configs = load_test_configs(conf)
    parsed = all_configs(configs)
    assert len(parsed) == 2
    assert sum(isinstance(config, CopyConfig) for config in parsed) == 1
    assert sum(isinstance(config, ProbeConfig) for config in parsed) == 1


# ---------------------------------------------------------------------------
# What loading hands to a block
# ---------------------------------------------------------------------------


def test_load_configs_passes_the_default_namespace_to_blocks(tmp_path: Path) -> None:
    """Namespace-owner mode lets an entry omit its namespace."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(conf, "config.toml", '[[probe]]\nname = "myapp"\n')
    probe = ProbeBlock()

    load_configs(conf, [probe], default_namespace="idcat")

    (call,) = probe.load_calls
    assert call["default_namespace"] == "idcat"
    assert only_config([probe]).namespace == "idcat"


def test_load_configs_passes_the_default_image_to_blocks(tmp_path: Path) -> None:
    """An image override reaches the blocks that support one."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(conf, "config.toml", '[[probe]]\nname = "myapp"\n')
    probe = ProbeBlock()

    load_configs(conf, [probe], default_image="example.com/myapp:1.0")

    (call,) = probe.load_calls
    assert call["default_image"] == "example.com/myapp:1.0"
    config = only_config([probe])
    assert isinstance(config, ProbeConfig)
    assert config.image == "example.com/myapp:1.0"


def test_resolve_configs_offers_the_helmfile_to_every_block() -> None:
    """Every block gets a chance to resolve references once loading is done."""
    probe = ProbeBlock()
    blocks: list[ConfigBlock[Any]] = [CopyBlock(), probe]

    resolved = resolve_configs(blocks, None)

    assert resolved is blocks
    assert probe.resolved_with == [None]


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


def test_load_copy_config_basic(tmp_path: Path) -> None:
    """Basic [[copy]] entry with namespace and source is parsed correctly."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
""",
    )

    configs = load_test_configs(tmp_path)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.namespace == "acme-dns"
    assert config.source == tmp_path / "manifests"
    assert config.config is None


def test_load_copy_config_unknown_field_raises(tmp_path: Path) -> None:
    """Unknown copy fields should fail before generation."""
    (tmp_path / "manifests").mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
sources = "other-manifests"
""",
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[copy\]\]: 'sources' on line 4",
    ):
        load_test_configs(tmp_path)


def test_load_copy_config_name_defaults_to_namespace(tmp_path: Path) -> None:
    """When name is omitted, it defaults to the namespace value."""
    (tmp_path / "manifests").mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.name == "acme-dns"


def test_load_copy_config_explicit_name(tmp_path: Path) -> None:
    """An explicit name field overrides the namespace default."""
    (tmp_path / "manifests").mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
name = "my-app"
namespace = "acme-dns"
source = "manifests"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.name == "my-app"


def test_load_copy_config_missing_namespace_and_name(tmp_path: Path) -> None:
    """Missing both namespace and name raises ValueError."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
source = "manifests"
""",
    )
    with pytest.raises(ValueError, match="must set either 'name' or 'namespace'"):
        load_test_configs(tmp_path)


def test_load_copy_config_namespace_optional_when_name_set(tmp_path: Path) -> None:
    """Namespace is optional at parse time when name is provided."""
    (tmp_path / "manifests").mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
name = "cluster-stuff"
source = "manifests"
""",
    )
    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.name == "cluster-stuff"
    assert config.namespace is None


def test_load_copy_config_missing_source(tmp_path: Path) -> None:
    """Missing source field raises ValueError."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
""",
    )
    with pytest.raises(ValueError, match="Missing required field 'source'"):
        load_test_configs(tmp_path)


def test_load_copy_config_source_resolved_relative_to_toml(
    tmp_path: Path,
) -> None:
    """source path is resolved relative to the TOML file's directory."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "manifests").mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
""",
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.source == conf_dir / "manifests"


def test_load_copy_config_with_config_dict(tmp_path: Path) -> None:
    """[copy.config] inline table is parsed into a dict of resolved paths."""
    (tmp_path / "manifests").mkdir()
    (tmp_path / "app.cfg").write_text("key=value")
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
[copy.config]
"/config/app.cfg" = "app.cfg"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.config is not None
    assert config.config["/config/app.cfg"] == tmp_path / "app.cfg"


def test_load_copy_config_with_config_array_of_tables(tmp_path: Path) -> None:
    """[[copy.config]] array-of-tables syntax is merged into a single dict."""
    (tmp_path / "manifests").mkdir()
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[copy]]
namespace = "acme-dns"
source = "manifests"
[[copy.config]]
"/config/app.cfg" = "app.cfg"
[[copy.config]]
"/config/other.cfg" = "other.cfg"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.config is not None
    assert set(config.config.keys()) == {"/config/app.cfg", "/config/other.cfg"}
    assert config.config["/config/app.cfg"] == tmp_path / "app.cfg"
    assert config.config["/config/other.cfg"] == tmp_path / "other.cfg"


def test_validate_copy_config_missing_source_dir(tmp_path: Path) -> None:
    """Validation fails if source directory does not exist."""
    config = CopyConfig(
        name="acme-dns",
        namespace="acme-dns",
        source=tmp_path / "nonexistent",
    )
    with pytest.raises(ValueError, match="source directory not found"):
        CopyBlock().validate(config, tmp_path)


def test_validate_copy_config_source_not_a_directory(tmp_path: Path) -> None:
    """Validation fails if source path is a file, not a directory."""
    not_a_dir = tmp_path / "file.yaml"
    not_a_dir.write_text("content")
    config = CopyConfig(
        name="acme-dns",
        namespace="acme-dns",
        source=not_a_dir,
    )
    with pytest.raises(ValueError, match="source path is not a directory"):
        CopyBlock().validate(config, tmp_path)


def test_validate_copy_config_missing_config_file(tmp_path: Path) -> None:
    """Validation fails if a referenced config file does not exist."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    config = CopyConfig(
        name="acme-dns",
        namespace="acme-dns",
        source=manifests_dir,
        config={"/config/app.cfg": tmp_path / "nonexistent.cfg"},
    )
    with pytest.raises(ValueError, match="Config file not found"):
        CopyBlock().validate(config, tmp_path)


# ---------------------------------------------------------------------------


def test_load_images_returns_image_dict(tmp_path: Path) -> None:
    """load_images should return dict mapping variable names to image references."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "images.toml").write_text(
        textwrap.dedent(
            """\
            [git]
            repo = "alpine/git"
            version = "2.47.2"

            [hugo]
            repo = "floryn90/hugo"
            version = "0.155.3-alpine"

            [static-web-server]
            repo = "ghcr.io/static-web-server/static-web-server"
            version = "2.36.1"
            """
        )
    )

    images = load_images(conf_dir)

    assert images == {
        "git_image": "alpine/git:2.47.2",
        "git_version": "2.47.2",
        "hugo_image": "floryn90/hugo:0.155.3-alpine",
        "hugo_version": "0.155.3-alpine",
        "static_web_server_image": "ghcr.io/static-web-server/static-web-server:2.36.1",
        "static_web_server_version": "2.36.1",
    }


def test_load_images_returns_empty_dict_when_missing(tmp_path: Path) -> None:
    """load_images should return an empty dict when images.toml is missing."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()

    assert load_images(conf_dir) == {}


def test_load_images_converts_hyphens_to_underscores(tmp_path: Path) -> None:
    """load_images should convert hyphenated keys to underscored variable names."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "images.toml").write_text(
        textwrap.dedent(
            """\
            [my-custom-image]
            repo = "example.com/my-image"
            version = "1.0.0"
            """
        )
    )

    images = load_images(conf_dir)

    assert "my_custom_image_image" in images
    assert images["my_custom_image_image"] == "example.com/my-image:1.0.0"
    assert "my_custom_image_version" in images
    assert images["my_custom_image_version"] == "1.0.0"


def test_load_images_raises_on_empty_file(tmp_path: Path) -> None:
    """load_images should raise ValueError if images.toml is empty."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "images.toml").write_text("")

    with pytest.raises(ValueError, match="images.toml is empty"):
        load_images(conf_dir)


def test_load_images_raises_on_invalid_entry(tmp_path: Path) -> None:
    """load_images should raise ValueError if an image entry is missing required fields."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "images.toml").write_text(
        textwrap.dedent(
            """\
            [git]
            repo = "alpine/git"
            """
        )
    )

    with pytest.raises(ValueError, match="must have 'repo' and 'version' fields"):
        load_images(conf_dir)


# ---------------------------------------------------------------------------


def test_load_owned_namespaces_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    """No owners directory means no namespaces are owned by others."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()

    assert load_owned_namespaces(conf_dir) == set()


def test_load_owned_namespaces_owned_string(tmp_path: Path) -> None:
    """An 'owned' string adds one output root to the owned set."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "team-a.toml").write_text('owned = "team-a"\n')

    assert load_owned_namespaces(tmp_path / "conf") == {"team-a"}


def test_load_owned_namespaces_owned_list(tmp_path: Path) -> None:
    """An 'owned' list adds each entry to the owned set."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "platform.toml").write_text('owned = ["monitoring", "logging"]\n')

    assert load_owned_namespaces(tmp_path / "conf") == {"monitoring", "logging"}


def test_load_owned_namespaces_combines_keys_across_files(tmp_path: Path) -> None:
    """Owner files accumulate into a single owned set."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "team-a.toml").write_text('owned = ["team-a", "alpha", "beta"]\n')
    (owners_dir / "team-b.toml").write_text('owned = "team-b"\n')

    assert load_owned_namespaces(tmp_path / "conf") == {
        "team-a",
        "alpha",
        "beta",
        "team-b",
    }


def test_load_owned_namespaces_can_exclude_owner_files(tmp_path: Path) -> None:
    """Callers can ignore owners that are handled by a different mode."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "system.toml").write_text('owned = ["cluster", "team-a"]\n')
    (owners_dir / "team-b.toml").write_text('owned = "team-b"\n')

    assert load_owned_namespaces(
        tmp_path / "conf", exclude_owner_files={"system.toml"}
    ) == {"team-b"}


def test_load_owned_namespaces_rejects_invalid_owned_value(tmp_path: Path) -> None:
    """An 'owned' value that is not a string or list is a configuration error."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "bad.toml").write_text("owned = 42\n")

    with pytest.raises(ValueError, match="'owned' must be a string or list"):
        load_owned_namespaces(tmp_path / "conf")


def test_load_owned_namespaces_ignores_non_toml_files(tmp_path: Path) -> None:
    """Files without a .toml suffix in owners/ are ignored."""
    owners_dir = tmp_path / "conf" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "README.md").write_text("# notes\n")
    (owners_dir / "team-a.toml").write_text('owned = "team-a"\n')

    assert load_owned_namespaces(tmp_path / "conf") == {"team-a"}


# ---------------------------------------------------------------------------


def write_targets_config(conf: Path, content: str) -> Path:
    """Write a version = 2 top-level config file."""
    return write_toml(conf, "config.toml", content)


def write_section(conf: Path, section: str, content: str) -> Path:
    """Write one section directory with its config file."""
    section_dir = conf / section
    section_dir.mkdir(parents=True, exist_ok=True)
    return write_toml(section_dir, "section.toml", content)


def load_target_configs(conf: Path, target: str) -> Sequence[ConfigBlock]:
    return load_configs(conf, config_blocks(), target=target)


def dev_and_prod_config(conf: Path) -> None:
    """A two-target, two-section config directory used by several tests."""
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "platform-dev"
        sections = ["base", "platform"]
        [target.vars]
        cluster_name = "platform-dev"
        vanity_domain = "portswigger.com"

        [[target]]
        name = "platform-prod"
        sections = ["base", "platform"]
        [target.vars]
        cluster_name = "platform-prod"
        vanity_domain = "portswigger.net"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "argo"
        source = "argocd"
        name = "argocd"
        """,
    )
    write_section(
        conf,
        "platform",
        """\
        [[copy]]
        namespace = "idcat"
        source = "idcat"
        name = "idcat"
        """,
    )


def test_targets_load_blocks_from_every_section(tmp_path: Path) -> None:
    """A target's sections each contribute their config blocks."""
    conf = tmp_path / "conf"
    conf.mkdir()
    dev_and_prod_config(conf)

    configs = all_configs(load_target_configs(conf, "platform-dev"))

    assert sorted(config.name for config in configs) == ["argocd", "idcat"]


def test_target_vars_reach_every_section(tmp_path: Path) -> None:
    """The selected target's vars become the variables each section renders with."""
    conf = tmp_path / "conf"
    conf.mkdir()
    dev_and_prod_config(conf)

    configs = all_configs(load_target_configs(conf, "platform-prod"))

    assert configs
    for config in configs:
        assert isinstance(config, CopyConfig)
        assert config.variables == {
            "cluster_name": "platform-prod",
            "vanity_domain": "portswigger.net",
        }


def test_selecting_a_target_ignores_the_other_targets_vars(tmp_path: Path) -> None:
    """Only the selected target's vars are used."""
    conf = tmp_path / "conf"
    conf.mkdir()
    dev_and_prod_config(conf)

    config = next(iter(all_configs(load_target_configs(conf, "platform-dev"))))

    assert isinstance(config, CopyConfig)
    assert config.variables["cluster_name"] == "platform-dev"


def test_target_selects_only_its_own_sections(tmp_path: Path) -> None:
    """A section no selected target names is not loaded."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "small"
        sections = ["base"]

        [[target]]
        name = "large"
        sections = ["base", "extras"]
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "argo"
        source = "argocd"
        name = "argocd"
        """,
    )
    write_section(
        conf,
        "extras",
        """\
        [[copy]]
        namespace = "extra"
        source = "extra"
        name = "extra"
        """,
    )

    small = all_configs(load_target_configs(conf, "small"))
    assert sorted(config.name for config in small) == ["argocd"]

    large = all_configs(load_target_configs(conf, "large"))
    assert sorted(config.name for config in large) == ["argocd", "extra"]


def test_section_paths_resolve_inside_the_section_directory(tmp_path: Path) -> None:
    """Paths a section's blocks reference resolve relative to that section."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["platform"]
        """,
    )
    write_section(
        conf,
        "platform",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    config = only_config(load_target_configs(conf, "dev"))

    assert isinstance(config, CopyConfig)
    assert config.source == conf / "platform" / "myapp"


def test_two_sections_may_name_the_same_relative_path(tmp_path: Path) -> None:
    """Sections are self-contained, so equal relative paths stay distinct."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base", "platform"]
        """,
    )
    for section, name in (("base", "base-app"), ("platform", "platform-app")):
        write_section(
            conf,
            section,
            f"""\
            [[copy]]
            namespace = "default"
            source = "myapp"
            name = "{name}"
            """,
        )

    sources: dict[str, Path] = {}
    for config in all_configs(load_target_configs(conf, "dev")):
        assert isinstance(config, CopyConfig)
        sources[config.name] = config.source

    assert sources == {
        "base-app": conf / "base" / "myapp",
        "platform-app": conf / "platform" / "myapp",
    }


@pytest.mark.parametrize(
    "file_name", ["section.toml", "config.toml", "manifest-builder.toml"]
)
def test_section_config_file_names(tmp_path: Path, file_name: str) -> None:
    """A section holds what a top-level config file used to, under any of its names."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )
    section_dir = conf / "base"
    section_dir.mkdir()
    write_toml(
        section_dir,
        file_name,
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    assert only_config(load_target_configs(conf, "dev")).name == "myapp"


def test_section_toml_preferred_over_config_toml(tmp_path: Path) -> None:
    """section.toml is the canonical name and wins when both are present."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )
    section_dir = conf / "base"
    section_dir.mkdir()
    for file_name, name in (
        ("section.toml", "from-section-toml"),
        ("config.toml", "from-config-toml"),
    ):
        write_toml(
            section_dir,
            file_name,
            f"""\
            [[copy]]
            namespace = "default"
            source = "myapp"
            name = "{name}"
            """,
        )

    assert only_config(load_target_configs(conf, "dev")).name == "from-section-toml"


def test_target_without_vars_loads_with_no_variables(tmp_path: Path) -> None:
    """A target need not declare vars."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    config = only_config(load_target_configs(conf, "dev"))
    assert isinstance(config, CopyConfig)
    assert config.variables == {}


def test_sections_accepts_a_single_string(tmp_path: Path) -> None:
    """A lone section may be given without a list."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = "base"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    assert only_config(load_target_configs(conf, "dev")).name == "myapp"


def test_section_variables_merge_with_target_vars(tmp_path: Path) -> None:
    """A section may add variables of its own alongside the target's vars."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        [target.vars]
        cluster_name = "platform-dev"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [variables]
        domain = "example.com"

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    config = only_config(load_target_configs(conf, "dev"))
    assert isinstance(config, CopyConfig)
    assert config.variables == {
        "cluster_name": "platform-dev",
        "domain": "example.com",
    }


def test_section_variable_conflicting_with_target_var_raises(tmp_path: Path) -> None:
    """A variable defined by both a target and a section is a config error."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        [target.vars]
        cluster_name = "platform-dev"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [variables]
        cluster_name = "something-else"

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(ValueError, match="'cluster_name' defined in both target 'dev'"):
        load_target_configs(conf, "dev")


def test_extra_variables_merge_with_target_vars(tmp_path: Path) -> None:
    """--vars-from variables are merged on top of a target's vars."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        [target.vars]
        cluster_name = "platform-dev"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    configs = load_configs(
        conf,
        config_blocks(),
        extra_variables={"build_id": 42},
        target="dev",
    )

    config = only_config(configs)
    assert isinstance(config, CopyConfig)
    assert config.variables == {"cluster_name": "platform-dev", "build_id": 42}


def test_extra_variable_conflicting_with_target_var_raises(tmp_path: Path) -> None:
    """A --vars-from variable a target also defines is a config error."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        [target.vars]
        cluster_name = "platform-dev"
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(ValueError, match="'cluster_name' defined in both"):
        load_configs(
            conf,
            config_blocks(),
            extra_variables={"cluster_name": "other"},
            target="dev",
        )


def test_target_must_be_selected(tmp_path: Path) -> None:
    """A targets-style config directory needs a target to be chosen."""
    conf = tmp_path / "conf"
    conf.mkdir()
    dev_and_prod_config(conf)

    with pytest.raises(
        ValueError,
        match="declares targets, so one must be selected. "
        "Available targets: 'platform-dev', 'platform-prod'",
    ):
        load_configs(conf, config_blocks())


def test_unknown_target_lists_the_available_ones(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    dev_and_prod_config(conf)

    with pytest.raises(
        ValueError,
        match="Unknown target 'staging'.*"
        "Available targets: 'platform-dev', 'platform-prod'",
    ):
        load_target_configs(conf, "staging")


def test_target_rejected_for_a_blocks_style_config(tmp_path: Path) -> None:
    """The older layout has no targets to choose between."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(
        ValueError,
        match="Cannot select target 'dev'.*declares config blocks directly",
    ):
        load_target_configs(conf, "dev")


def test_version_one_declares_config_blocks_directly(tmp_path: Path) -> None:
    """An explicit version = 1 keeps the blocks-in-config.toml layout."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        version = 1

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    assert only_config(load_test_configs(conf)).name == "myapp"


def test_unsupported_version_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(conf, "config.toml", "version = 3\n")

    with pytest.raises(
        ValueError, match="Unsupported config version 3.*expected 1 or 2"
    ):
        load_test_configs(conf)


def test_non_integer_version_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(conf, "config.toml", 'version = "2"\n')

    with pytest.raises(ValueError, match="Unsupported config version '2'"):
        load_test_configs(conf)


def test_targets_config_rejects_config_blocks(tmp_path: Path) -> None:
    """Config blocks belong in a section, not the targets file."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]

        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(
        ValueError,
        match="Unknown top-level field: 'copy'.*config blocks belong in a "
        "section directory",
    ):
        load_target_configs(conf, "dev")


def test_targets_config_without_targets_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(conf, "version = 2\n")

    with pytest.raises(ValueError, match=r"No \[\[target\]\] entries found"):
        load_target_configs(conf, "dev")


def test_missing_section_directory_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base", "absent"]
        """,
    )
    write_section(
        conf,
        "base",
        """\
        [[copy]]
        namespace = "default"
        source = "myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(
        FileNotFoundError,
        match="Section directory not found for target 'dev'",
    ):
        load_target_configs(conf, "dev")


def test_section_without_a_config_file_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )
    (conf / "base").mkdir()

    with pytest.raises(
        FileNotFoundError,
        match="Configuration file not found.*section.toml.*for section 'base'",
    ):
        load_target_configs(conf, "dev")


def test_section_without_config_blocks_raises(tmp_path: Path) -> None:
    """A section that declares no blocks is a mistake worth reporting."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )
    write_section(conf, "base", '[variables]\ndomain = "example.com"\n')

    with pytest.raises(ValueError, match=r"No .*\[\[copy\]\].* entries found"):
        load_target_configs(conf, "dev")


def test_duplicate_target_names_raise(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]

        [[target]]
        name = "dev"
        sections = ["base"]
        """,
    )

    with pytest.raises(ValueError, match="Duplicate target 'dev'"):
        load_target_configs(conf, "dev")


def test_repeated_section_in_one_target_raises(tmp_path: Path) -> None:
    """Loading a section twice would duplicate every block it declares."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base", "base"]
        """,
    )

    with pytest.raises(ValueError, match="lists section 'base' more than once"):
        load_target_configs(conf, "dev")


@pytest.mark.parametrize("section", ["../escape", "nested/section", ".hidden", ""])
def test_invalid_section_name_raises(tmp_path: Path, section: str) -> None:
    """A section must name a single directory inside the config directory."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        f"""\
        version = 2

        [[target]]
        name = "dev"
        sections = ["{section}"]
        """,
    )

    with pytest.raises(ValueError, match="Invalid section name"):
        load_target_configs(conf, "dev")


def test_target_without_sections_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        """,
    )

    with pytest.raises(ValueError, match="Target 'dev'.*must set 'sections'"):
        load_target_configs(conf, "dev")


def test_target_with_empty_sections_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = []
        """,
    )

    with pytest.raises(ValueError, match="must list at least one section"):
        load_target_configs(conf, "dev")


def test_target_without_name_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        sections = ["base"]
        """,
    )

    with pytest.raises(ValueError, match="must set a non-empty 'name'"):
        load_target_configs(conf, "dev")


def test_target_unknown_field_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        cluster = "oops"
        """,
    )

    with pytest.raises(
        ValueError, match=r"Unknown field in \[\[target\]\]: 'cluster' on line 6"
    ):
        load_target_configs(conf, "dev")


def test_target_sections_must_be_strings(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = [1, 2]
        """,
    )

    with pytest.raises(ValueError, match="'sections' must be a string or list"):
        load_target_configs(conf, "dev")


def test_target_vars_must_be_scalars(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_targets_config(
        conf,
        """\
        version = 2

        [[target]]
        name = "dev"
        sections = ["base"]
        [target.vars]
        nested = [1, 2]
        """,
    )

    with pytest.raises(ValueError, match="must be a string, number, or boolean"):
        load_target_configs(conf, "dev")
