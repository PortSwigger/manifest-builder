# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for configuration parsing and validation."""

import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest

from manifest_builder.handlers import ConfigHandler
from manifest_builder.config import (
    DEFAULT_REPLICA_COUNT,
    ChartConfig,
    CopyConfig,
    ManifestConfig,
    SimpleConfig,
    WebsiteConfig,
    load_configs,
    load_extra_variables,
    load_images,
    load_owned_namespaces,
    resolve_configs,
    validate_config,
)
from manifest_builder.copy import CopyConfigHandler
from manifest_builder.generator import HelmConfigHandler
from manifest_builder.helmfile import Helmfile, HelmfileRelease, HelmfileRepository
from manifest_builder.public_repo import PublicRepoConfigHandler
from manifest_builder.simple import SimpleConfigHandler
from manifest_builder.website import WebsiteConfigHandler


def write_toml(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(content))
    return path


def all_configs(
    handlers: Sequence[ConfigHandler],
) -> tuple[ManifestConfig, ...]:
    return tuple(config for handler in handlers for config in handler.iter_configs())


def only_config(
    handlers: Sequence[ConfigHandler],
) -> ManifestConfig:
    (config,) = all_configs(handlers)
    return config


def config_handlers() -> list[
    HelmConfigHandler
    | WebsiteConfigHandler
    | SimpleConfigHandler
    | CopyConfigHandler
    | PublicRepoConfigHandler
]:
    return [
        HelmConfigHandler(),
        WebsiteConfigHandler(),
        SimpleConfigHandler(),
        CopyConfigHandler(),
        PublicRepoConfigHandler(),
    ]


def load_test_configs(
    config_dir: Path,
) -> Sequence[ConfigHandler]:
    return load_configs(config_dir, config_handlers())


def manifest_configs(
    *,
    helm: list[ChartConfig] | None = None,
    websites: list[WebsiteConfig] | None = None,
    simples: list[SimpleConfig] | None = None,
    copies: list[CopyConfig] | None = None,
) -> list[
    HelmConfigHandler | WebsiteConfigHandler | SimpleConfigHandler | CopyConfigHandler
]:
    return [
        HelmConfigHandler(helm),
        WebsiteConfigHandler(websites),
        SimpleConfigHandler(simples),
        CopyConfigHandler(copies),
    ]


# ---------------------------------------------------------------------------
# Values file path resolution
# ---------------------------------------------------------------------------


def test_values_resolved_relative_to_config_dir(tmp_path: Path) -> None:
    """Values paths must be resolved relative to the TOML file's directory."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        values = ["myapp/values.yaml"]
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
    assert config.values == [conf_dir / "myapp/values.yaml"]


def test_values_resolved_relative_to_custom_config_dir(tmp_path: Path) -> None:
    """Specifying a different -c directory changes where values are resolved."""
    conf_a = tmp_path / "conf-a"
    conf_a.mkdir()
    conf_b = tmp_path / "conf-b"
    conf_b.mkdir()

    for conf_dir in (conf_a, conf_b):
        write_toml(
            conf_dir,
            "config.toml",
            """\
            [[helm]]
            namespace = "default"
            chart = "./charts/myapp"
            name = "myapp"
            values = ["values.yaml"]
            """,
        )

    configs_a = load_test_configs(conf_a)
    configs_b = load_test_configs(conf_b)

    config_a = only_config(configs_a)
    config_b = only_config(configs_b)
    assert isinstance(config_a, ChartConfig)
    assert isinstance(config_b, ChartConfig)
    assert config_a.values == [conf_a / "values.yaml"]
    assert config_b.values == [conf_b / "values.yaml"]
    assert config_a.values != config_b.values


def test_values_empty_when_not_specified(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
    assert config.values == []


def test_load_chart_config_with_name_override(tmp_path: Path) -> None:
    """Helm configs can override the release name passed to Helm."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        release = "myapp"
        name-override = "myapp-rendered"
        """,
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)

    assert isinstance(config, ChartConfig)
    assert config.name == "myapp"
    assert config.release == "myapp"
    assert config.name_override == "myapp-rendered"


def test_load_chart_config_with_config(tmp_path: Path) -> None:
    """Helm config can specify ConfigMap keys with local file paths."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    config_file = conf_dir / "app.conf"
    config_file.write_text("debug=true\n")
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        config = { "app.conf" = "app.conf" }
        """,
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)

    assert isinstance(config, ChartConfig)
    assert config.config == {"app.conf": config_file}


def test_load_chart_config_unknown_field_raises(tmp_path: Path) -> None:
    """Unknown Helm fields should fail before generation."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        value = ["values.yaml"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[helm\]\]: 'value' on line 5",
    ):
        load_test_configs(conf_dir)


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
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"

        [[helm]]
        namespace = "staging"
        chart = "./charts/other"
        name = "other"
        value = ["values.yaml"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[helm\]\]: 'value' on line 10",
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

        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )

    configs = load_configs(
        conf_dir,
        config_handlers(),
        extra_variables={"cluster_name": "prod", "replica_count": 3},
    )
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
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
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )

    configs = load_configs(
        conf_dir,
        config_handlers(),
        extra_variables={"cluster_name": "prod"},
    )
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
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

        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )

    with pytest.raises(ValueError, match="'domain'"):
        load_configs(
            conf_dir,
            config_handlers(),
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


def test_variables_are_loaded_for_helm_configs(tmp_path: Path) -> None:
    """Top-level variables should be attached to helm configs from the same TOML file."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [variables]
        domain = "example.com"
        replica_count = 3
        use_tls = true

        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        values = ["values.yaml"]
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
    assert config.variables == {
        "domain": "example.com",
        "replica_count": 3,
        "use_tls": True,
    }


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


def test_validate_config_missing_values_file(tmp_path: Path) -> None:
    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[tmp_path / "nonexistent.yaml"],
        release=None,
    )
    with pytest.raises(ValueError, match="Values file not found"):
        validate_config(config, tmp_path)


def test_validate_config_existing_values_file(tmp_path: Path) -> None:
    values_file = tmp_path / "values.yaml"
    values_file.write_text("key: value\n")

    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart=None,
        repo=None,
        version=None,
        values=[values_file],
        release="myapp",
    )
    validate_config(config, tmp_path)  # should not raise


def test_validate_config_missing_local_chart(tmp_path: Path) -> None:
    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
    )
    with pytest.raises(ValueError, match="Local chart path not found"):
        validate_config(config, tmp_path)


# ---------------------------------------------------------------------------
# load_configs
# ---------------------------------------------------------------------------


def test_load_configs_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_test_configs(tmp_path / "nonexistent")


def test_load_configs_missing_config_toml(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        load_test_configs(conf)


def test_load_configs_accepts_manifest_builder_toml(tmp_path: Path) -> None:
    """The top-level config file can be named manifest-builder.toml."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "manifest-builder.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )

    config = only_config(load_test_configs(conf))

    assert isinstance(config, ChartConfig)
    assert config.name == "myapp"


def test_load_configs_prefers_config_toml(tmp_path: Path) -> None:
    """Existing config.toml behavior is preserved when both names are present."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "from-config-toml"
        """,
    )
    write_toml(
        conf,
        "manifest-builder.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "from-manifest-builder-toml"
        """,
    )

    config = only_config(load_test_configs(conf))

    assert isinstance(config, ChartConfig)
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


def test_load_configs_no_registered_handler_tables(tmp_path: Path) -> None:
    """Config loading fails when none of the registered handler lists is present."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )
    with pytest.raises(
        ValueError,
        match="Unknown top-level field: 'helm' on line 1",
    ):
        load_configs(conf, [WebsiteConfigHandler()])


def test_load_configs_without_handlers_raises(tmp_path: Path) -> None:
    """Config loading needs at least one handler to define known config lists."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "myapp"
        """,
    )
    with pytest.raises(ValueError, match="No config handlers registered"):
        load_configs(conf, [])


def test_load_simple_config(tmp_path: Path) -> None:
    """Simple config can omit name and use namespace as the generated name."""
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "idcat").mkdir()
    (conf / "idcat" / "myconfig.toml").write_text("[idcat]\nenabled = true\n")
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"

        [[simple.config]]
        "/config/myconfig.toml" = "idcat/myconfig.toml"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.name == "idcat"
    assert config.namespace == "idcat"
    assert config.image == "example.com/idcat:1.0"
    assert config.config == {"/config/myconfig.toml": conf / "idcat" / "myconfig.toml"}


def test_load_simple_config_uses_default_namespace(tmp_path: Path) -> None:
    """Simple config can get its namespace from namespace-owner mode."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        image = "example.com/idcat:1.0"
        """,
    )

    configs = load_configs(conf, config_handlers(), default_namespace="idcat")
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.name == "idcat"
    assert config.namespace == "idcat"


def test_load_simple_config_uses_default_image(tmp_path: Path) -> None:
    """Simple config can get its image from namespace-mode API input."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        """,
    )

    configs = load_configs(
        conf,
        config_handlers(),
        default_namespace="idcat",
        default_image="example.com/idcat:1.0",
    )
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.namespace == "idcat"
    assert config.image == "example.com/idcat:1.0"


def test_load_simple_config_rejects_image_with_default_image(tmp_path: Path) -> None:
    """Config image and API image override are mutually exclusive."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        image = "example.com/idcat:1.0"
        """,
    )

    with pytest.raises(ValueError, match="Cannot specify 'image'.*generate"):
        load_configs(
            conf,
            config_handlers(),
            default_namespace="idcat",
            default_image="example.com/override:1.0",
        )


def test_load_simple_config_with_extra_resources(tmp_path: Path) -> None:
    """Simple config can specify a directory with extra YAML resources."""
    conf = tmp_path / "conf"
    conf.mkdir()
    resources_dir = conf / "resources"
    resources_dir.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        extra-resources = "resources"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.extra_resources == resources_dir


def test_load_simple_config_with_iam_role_and_variables(tmp_path: Path) -> None:
    """Simple iam-role is parsed with the variables used during rendering."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [variables]
        account_id = "123456789012"
        cluster_name = "berries"

        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        iam-role = "arn:aws:iam::{{account_id}}:role/{{cluster_name}}-idcat"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.iam_role == "arn:aws:iam::{{account_id}}:role/{{cluster_name}}-idcat"
    assert config.variables == {
        "account_id": "123456789012",
        "cluster_name": "berries",
    }


def test_load_simple_config_with_k8s_role(tmp_path: Path) -> None:
    """Simple config can specify a Kubernetes Role to bind to its ServiceAccount."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        k8s-role = "idcat-reader"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.k8s_role == "idcat-reader"


def test_load_simple_config_with_service_account_annotations(tmp_path: Path) -> None:
    """Simple config can specify a table of ServiceAccount annotations."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"

        [simple.service-account-annotations]
        "example.com/owner" = "platform"
        "example.com/team" = "berries"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.service_account_annotations == {
        "example.com/owner": "platform",
        "example.com/team": "berries",
    }


def test_load_simple_config_service_account_annotations_must_be_strings(
    tmp_path: Path,
) -> None:
    """Non-string annotation values are rejected."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"

        [simple.service-account-annotations]
        "example.com/replicas" = 3
        """,
    )

    with pytest.raises(
        ValueError, match=r"'service-account-annotations' must be a table"
    ):
        load_test_configs(conf)


def test_load_simple_config_rejects_role_arn_annotation_with_iam_role(
    tmp_path: Path,
) -> None:
    """Setting the role-arn annotation and iam-role together fails clearly."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        iam-role = "arn:aws:iam::123456789012:role/berries-idcat"

        [simple.service-account-annotations]
        "eks.amazonaws.com/role-arn" = "arn:aws:iam::123456789012:role/other"
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"cannot set 'eks\.amazonaws\.com/role-arn' when 'iam-role'",
    ):
        load_test_configs(conf)


def test_load_simple_config_with_arch(tmp_path: Path) -> None:
    """Simple config can declare a node architecture for the Pod nodeSelector."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        arch = "arm64"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.arch == "arm64"


def test_load_simple_config_with_custom_token_audiences(tmp_path: Path) -> None:
    """Simple config can specify custom audiences for projected tokens."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        custom-token-audiences = ["vault", "api"]
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.custom_token_audiences == ["vault", "api"]


def test_load_simple_config_custom_token_audiences_must_be_list(
    tmp_path: Path,
) -> None:
    """Simple custom token audiences must be configured as a string list."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        custom-token-audiences = "vault"
        """,
    )

    with pytest.raises(
        ValueError,
        match="'custom-token-audiences' must be a list of strings",
    ):
        load_test_configs(conf)


def test_load_simple_config_arch_must_be_string(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        arch = 64
        """,
    )

    with pytest.raises(ValueError, match="'arch' must be a string"):
        load_test_configs(conf)


def test_load_simple_config_with_random_secret(tmp_path: Path) -> None:
    """A single random-secret is normalized into a one-element list."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        random-secret = "SESSION_KEY"
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.random_secrets == ["SESSION_KEY"]


def test_load_simple_config_with_random_secrets_list(tmp_path: Path) -> None:
    """A random-secrets list is preserved in order."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        random-secrets = ["API_KEY", "SIGNING_KEY"]
        """,
    )

    configs = load_test_configs(conf)
    config = only_config(configs)
    assert isinstance(config, SimpleConfig)
    assert config.random_secrets == ["API_KEY", "SIGNING_KEY"]


def test_load_simple_config_rejects_both_random_secret_forms(tmp_path: Path) -> None:
    """Specifying both random-secret and random-secrets is an error."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        random-secret = "SESSION_KEY"
        random-secrets = ["API_KEY"]
        """,
    )

    with pytest.raises(
        ValueError,
        match="Cannot specify both 'random-secret' and 'random-secrets'",
    ):
        load_test_configs(conf)


def test_load_simple_config_random_secrets_must_be_list_of_strings(
    tmp_path: Path,
) -> None:
    """random-secrets must be a list of strings."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        random-secrets = "API_KEY"
        """,
    )

    with pytest.raises(
        ValueError,
        match="'random-secrets' must be a list of strings",
    ):
        load_test_configs(conf)


def test_load_simple_config_unknown_field_raises(tmp_path: Path) -> None:
    """Unknown simple fields should fail before generation."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[simple]]
        namespace = "idcat"
        image = "example.com/idcat:1.0"
        iam_role = "typo"
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[simple\]\]: 'iam_role' on line 4",
    ):
        load_test_configs(conf)


def test_load_configs_both_release_and_chart_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        name = "myapp"
        chart = "./charts/myapp"
        release = "myapp"
        """,
    )
    with pytest.raises(ValueError, match="Cannot specify both"):
        load_test_configs(conf)


def test_load_configs_neither_release_nor_chart_raises(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        name = "myapp"
        """,
    )
    with pytest.raises(ValueError, match="Must specify either"):
        load_test_configs(conf)


def test_load_configs_uses_only_config_toml(tmp_path: Path) -> None:
    """Sibling TOML files are reserved for other semantics and not parsed here."""
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "ns-a"
        chart = "./charts/a"
        name = "app-a"
        """,
    )
    write_toml(
        conf,
        "other.toml",
        """\
        [[helm]]
        namespace = "ns-b"
        chart = "./charts/b"
        name = "app-b"
        """,
    )

    configs = load_test_configs(conf)
    names = {c.name for c in all_configs(configs)}
    assert names == {"app-a"}


def test_load_configs_mixed_helms_and_websites(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    write_toml(
        conf,
        "config.toml",
        """\
        [[helm]]
        namespace = "default"
        chart = "./charts/myapp"
        name = "my-helm-app"

        [[website]]
        name = "my-website"
        namespace = "web"
        """,
    )

    configs = load_test_configs(conf)
    assert len(all_configs(configs)) == 2
    parsed = all_configs(configs)
    assert sum(isinstance(config, ChartConfig) for config in parsed) == 1
    assert sum(isinstance(config, WebsiteConfig) for config in parsed) == 1


# ---------------------------------------------------------------------------
# resolve_configs
# ---------------------------------------------------------------------------


def _make_helmfile() -> Helmfile:
    return Helmfile(
        repositories=[
            HelmfileRepository(name="myrepo", url="https://charts.example.com")
        ],
        releases=[
            HelmfileRelease(
                name="myapp",
                chart="myrepo/myapp",
                version="1.2.3",
                namespace="default",
            )
        ],
    )


def test_resolve_configs_fills_in_chart_and_repo(tmp_path: Path) -> None:
    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart=None,
        repo=None,
        version=None,
        values=[],
        release="myapp",
        name_override="myapp-rendered",
    )
    resolved = resolve_configs(manifest_configs(helm=[config]), _make_helmfile())
    assert len(all_configs(resolved)) == 1
    resolved_config = only_config(resolved)
    assert isinstance(resolved_config, ChartConfig)
    assert resolved_config.chart == "myapp"
    assert resolved_config.repo == "https://charts.example.com"
    assert resolved_config.version == "1.2.3"
    assert resolved_config.name_override == "myapp-rendered"


def test_resolve_configs_no_helmfile_raises_when_release_present(
    tmp_path: Path,
) -> None:
    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart=None,
        repo=None,
        version=None,
        values=[],
        release="myapp",
    )
    with pytest.raises(ValueError, match="no releases.yaml was found"):
        resolve_configs(manifest_configs(helm=[config]), None)


def test_resolve_configs_unknown_release_raises() -> None:
    config = ChartConfig(
        name="unknown",
        namespace="default",
        chart=None,
        repo=None,
        version=None,
        values=[],
        release="unknown",
    )
    with pytest.raises(ValueError, match="not found in releases.yaml"):
        resolve_configs(manifest_configs(helm=[config]), _make_helmfile())


def test_resolve_configs_oci_repository() -> None:
    """OCI repositories should be resolved to a full OCI URL chart and no repo."""
    helmfile = Helmfile(
        repositories=[
            HelmfileRepository(
                name="envoyproxy",
                url="docker.io/envoyproxy",
                oci=True,
            )
        ],
        releases=[
            HelmfileRelease(
                name="envoy-gateway",
                chart="envoyproxy/gateway-helm",
                version="v1.7.0",
                namespace="default",
            )
        ],
    )
    config = ChartConfig(
        name="envoy-gateway",
        namespace="default",
        chart=None,
        repo=None,
        version=None,
        values=[],
        release="envoy-gateway",
    )
    resolved = resolve_configs(manifest_configs(helm=[config]), helmfile)
    assert len(all_configs(resolved)) == 1
    resolved_config = only_config(resolved)
    assert isinstance(resolved_config, ChartConfig)
    assert resolved_config.chart == "oci://docker.io/envoyproxy/gateway-helm"
    assert resolved_config.repo is None
    assert resolved_config.version == "v1.7.0"


# ---------------------------------------------------------------------------
# WebsiteConfig parsing
# ---------------------------------------------------------------------------


def test_load_website_config(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.name == "my-website"
    assert config.namespace == "production"


def test_load_website_config_unknown_field_raises(tmp_path: Path) -> None:
    """Unknown website fields should fail before generation."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        external_secret = ["/password"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown field in \[\[website\]\]: 'external_secret' on line 4",
    ):
        load_test_configs(conf_dir)


def test_load_website_config_missing_name_field(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        namespace = "default"
        """,
    )
    with pytest.raises(ValueError, match="Missing required field 'name'"):
        load_test_configs(conf_dir)


def test_load_website_config_with_hugo_repo(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        hugo-repo = "https://github.com/user/repo"
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.hugo_repo == "https://github.com/user/repo"


def test_load_website_config_with_image(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        image = "nginx:latest"
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.image == "nginx:latest"


def test_load_website_config_uses_default_image(tmp_path: Path) -> None:
    """Website config can get its image from namespace-mode API input."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        """,
    )

    configs = load_configs(
        conf_dir,
        config_handlers(),
        default_namespace="production",
        default_image="nginx:latest",
    )
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.namespace == "production"
    assert config.image == "nginx:latest"


def test_load_website_config_rejects_image_with_default_image(
    tmp_path: Path,
) -> None:
    """Config image and API image override are mutually exclusive for websites."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        image = "nginx:latest"
        """,
    )

    with pytest.raises(ValueError, match="Cannot specify 'image'.*generate"):
        load_configs(
            conf_dir,
            config_handlers(),
            default_namespace="production",
            default_image="example.com/override:1.0",
        )


def test_load_website_config_with_args_string(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        args = "--flag=value"
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.args == "--flag=value"


def test_load_website_config_with_args_list(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        args = ["--flag1=value1", "--flag2=value2"]
        """,
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.args == ["--flag1=value1", "--flag2=value2"]


def test_load_website_config_with_env(tmp_path: Path) -> None:
    """Website config can specify container environment variables."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"

        [website.env]
        LOG_LEVEL = "debug"
        PUBLIC_URL = "https://example.com"
        """,
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.env == {
        "LOG_LEVEL": "debug",
        "PUBLIC_URL": "https://example.com",
    }


def test_load_website_config_env_value_must_be_string(tmp_path: Path) -> None:
    """Website env values must be strings."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"

        [website.env]
        DEBUG = true
        """,
    )

    with pytest.raises(ValueError, match="'env' values must be strings"):
        load_test_configs(conf_dir)


def test_load_website_config_hugo_repo_and_image_mutually_exclusive(
    tmp_path: Path,
) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    write_toml(
        conf_dir,
        "config.toml",
        """\
        [[website]]
        name = "my-website"
        namespace = "production"
        hugo-repo = "https://github.com/user/repo"
        image = "nginx:latest"
        """,
    )

    with pytest.raises(ValueError, match="Cannot specify both 'hugo-repo' and 'image'"):
        load_test_configs(conf_dir)


def test_resolve_configs_passes_website_config_through() -> None:
    config = WebsiteConfig(
        name="my-website",
        namespace="default",
    )
    resolved = resolve_configs(manifest_configs(websites=[config]), None)
    assert all_configs(resolved) == (config,)


def test_resolve_configs_passthrough_for_direct_chart() -> None:
    config = ChartConfig(
        name="myapp",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
        extra_resources=None,
    )
    resolved = resolve_configs(manifest_configs(helm=[config]), None)
    assert all_configs(resolved) == (config,)


def test_load_website_config_with_config(tmp_path: Path) -> None:
    """Website config can specify config with local paths."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()

    # Create config files in the conf directory (not with .toml extension to avoid glob)
    config_file = conf_dir / "app.conf"
    config_file.write_text("[app]\nkey = value\n")

    write_toml(
        conf_dir,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
config = { "/config/app.conf" = "app.conf" }
""",
    )

    configs = load_test_configs(conf_dir)
    assert len(all_configs(configs)) == 1
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.config is not None
    assert config.config["/config/app.conf"] == conf_dir / "app.conf"


def test_load_website_config_multiple_config(tmp_path: Path) -> None:
    """Website config can specify multiple config files in different directories."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()

    # Create config files (use .conf and .yaml to avoid .toml glob)
    (conf_dir / "app.conf").write_text("app")
    (conf_dir / "db.yaml").write_text("db")

    write_toml(
        conf_dir,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
config = { "/config/app.conf" = "app.conf", "/etc/db.yaml" = "db.yaml" }
""",
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.config is not None
    assert len(config.config) == 2
    assert config.config["/config/app.conf"] == conf_dir / "app.conf"
    assert config.config["/etc/db.yaml"] == conf_dir / "db.yaml"


def test_load_website_config_with_persistence(tmp_path: Path) -> None:
    """Website config can specify persistent storage by mount path."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
persistence = { "/data" = "1Gi" }
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.persistence == {"/data": "1Gi"}


def test_load_website_config_with_emptydir_path(tmp_path: Path) -> None:
    """Website config can specify an ephemeral writable emptyDir mount path."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
emptydir-path = "/cache"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.emptydir_path == "/cache"


def test_load_website_config_emptydir_path_must_be_absolute(
    tmp_path: Path,
) -> None:
    """Website emptydir-path must be an absolute container path."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
emptydir-path = "cache"
""",
    )

    with pytest.raises(ValueError, match="'emptydir-path' must be an absolute path"):
        load_test_configs(tmp_path)


def test_validate_config_missing_config_file(tmp_path: Path) -> None:
    """Validation should fail if a referenced config file doesn't exist."""
    config = WebsiteConfig(
        name="my-app",
        namespace="default",
        config={"/config/app.toml": tmp_path / "nonexistent.toml"},
    )
    with pytest.raises(ValueError, match="Config file not found"):
        validate_config(config, tmp_path)


def test_load_website_config_with_extra_hostnames_string(tmp_path: Path) -> None:
    """Website config can specify extra_hostnames as a string."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
extra-hostnames = "www.example.com"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.extra_hostnames == "www.example.com"


def test_load_website_config_with_extra_hostnames_list(tmp_path: Path) -> None:
    """Website config can specify extra_hostnames as a list."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
extra-hostnames = ["www.example.com", "example.cdn.com"]
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.extra_hostnames == ["www.example.com", "example.cdn.com"]


def test_load_website_config_with_external_secrets_list(tmp_path: Path) -> None:
    """Website config can specify external_secrets as a list of mount paths."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
external-secrets = ["/email-password", "/db/credentials"]
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.external_secrets == ["/email-password", "/db/credentials"]


def test_load_website_config_with_external_secrets_string(tmp_path: Path) -> None:
    """Website config can specify external_secrets as a single string (normalized to list)."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
external-secrets = "/api-key"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.external_secrets == ["/api-key"]


def test_load_website_config_with_custom_token_audiences(tmp_path: Path) -> None:
    """Website config can specify custom audiences for projected tokens."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
custom-token-audiences = ["vault", "api"]
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.custom_token_audiences == ["vault", "api"]


def test_load_website_config_custom_token_audiences_must_be_list(
    tmp_path: Path,
) -> None:
    """Website custom token audiences must be configured as a string list."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
custom-token-audiences = "vault"
""",
    )

    with pytest.raises(
        ValueError,
        match="'custom-token-audiences' must be a list of strings",
    ):
        load_test_configs(tmp_path)


def test_load_website_config_with_replicas(tmp_path: Path) -> None:
    """Website config can specify replicas for the Deployment."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
replicas = 5
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.replicas == 5


def test_load_website_config_replicas_single(tmp_path: Path) -> None:
    """Website config can specify replicas=1."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
replicas = 1
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.replicas == 1


def test_load_website_config_replicas_not_specified(tmp_path: Path) -> None:
    """Website config without replicas should default to DEFAULT_REPLICA_COUNT."""
    write_toml(
        tmp_path,
        "config.toml",
        """\
[[website]]
name = "my-app"
namespace = "default"
image = "nginx:latest"
""",
    )

    configs = load_test_configs(tmp_path)
    config = only_config(configs)
    assert isinstance(config, WebsiteConfig)
    assert config.replicas == DEFAULT_REPLICA_COUNT


def test_load_chart_config_with_extra_resources(tmp_path: Path) -> None:
    """Chart config can specify a directory with extra YAML resources."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    resources_dir = conf_dir / "resources"
    resources_dir.mkdir()

    write_toml(
        conf_dir,
        "config.toml",
        """\
[[helm]]
name = "my-chart"
namespace = "default"
chart = "./charts/myapp"
extra-resources = "resources"
""",
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
    assert config.extra_resources == resources_dir


def test_validate_config_chart_extra_resources_missing_directory(
    tmp_path: Path,
) -> None:
    """Validation should fail if extra_resources directory doesn't exist."""
    config = ChartConfig(
        name="my-chart",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
        extra_resources=tmp_path / "nonexistent",
    )
    with pytest.raises(ValueError, match="Extra resources directory not found"):
        validate_config(config, tmp_path)


def test_validate_config_chart_config_missing_file(tmp_path: Path) -> None:
    """Validation should fail if a Helm config file doesn't exist."""
    config = ChartConfig(
        name="my-chart",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
        config={"app.conf": tmp_path / "missing.conf"},
    )
    with pytest.raises(ValueError, match="Config file not found for chart"):
        validate_config(config, tmp_path)


def test_validate_config_chart_extra_resources_not_a_directory(tmp_path: Path) -> None:
    """Validation should fail if extra_resources path is not a directory."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("content")

    config = ChartConfig(
        name="my-chart",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
        extra_resources=not_a_dir,
    )
    with pytest.raises(ValueError, match="Extra resources path is not a directory"):
        validate_config(config, tmp_path)


def test_validate_config_simple_extra_resources_missing_directory(
    tmp_path: Path,
) -> None:
    """Validation should fail if simple extra_resources directory doesn't exist."""
    config = SimpleConfig(
        name="idcat",
        namespace="idcat",
        image="example.com/idcat:1.0",
        extra_resources=tmp_path / "nonexistent",
    )
    with pytest.raises(ValueError, match="Extra resources directory not found"):
        validate_config(config, tmp_path)


def test_validate_config_simple_extra_resources_not_a_directory(tmp_path: Path) -> None:
    """Validation should fail if simple extra_resources path is not a directory."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("content")

    config = SimpleConfig(
        name="idcat",
        namespace="idcat",
        image="example.com/idcat:1.0",
        extra_resources=not_a_dir,
    )
    with pytest.raises(ValueError, match="Extra resources path is not a directory"):
        validate_config(config, tmp_path)


def test_load_chart_config_with_init(tmp_path: Path) -> None:
    """Chart config can specify an init script to inject as initContainer."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    script_file = conf_dir / "setup.sh"
    script_file.write_text("#!/bin/sh\necho 'initializing'")

    write_toml(
        conf_dir,
        "config.toml",
        """\
[[helm]]
name = "my-chart"
namespace = "default"
chart = "./charts/myapp"
init = "setup.sh"
""",
    )

    configs = load_test_configs(conf_dir)
    config = only_config(configs)
    assert isinstance(config, ChartConfig)
    assert config.init == script_file


def test_validate_config_chart_init_missing_file(tmp_path: Path) -> None:
    """Validation should fail if init script file doesn't exist."""
    # Create the chart directory so that check passes
    chart_dir = tmp_path / "charts" / "myapp"
    chart_dir.mkdir(parents=True)

    config = ChartConfig(
        name="my-chart",
        namespace="default",
        chart="./charts/myapp",
        repo=None,
        version=None,
        values=[],
        release=None,
        init=tmp_path / "nonexistent.sh",
    )
    with pytest.raises(ValueError, match="init script not found"):
        validate_config(config, tmp_path)


# ---------------------------------------------------------------------------
# Copy config item parsing
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
        validate_config(config, tmp_path)


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
        validate_config(config, tmp_path)


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
        validate_config(config, tmp_path)


# ---------------------------------------------------------------------------
# Image loading
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
# load_owned_namespaces
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
