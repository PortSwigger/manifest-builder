# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for public ECR repository manifest generation."""

import json
import textwrap
from pathlib import Path

import pystache.context
import pytest
import yaml

from manifest_builder.blocks.public_repo import PublicRepoConfig
from manifest_builder.config import load_configs
from manifest_builder.blocks.public_repo import (
    PublicRepoConfigHandler,
    generate_public_repo,
)

ACCOUNT_ID = 436027055282


def _make_config(
    name: str = "idcat",
    enable_charts: bool = False,
) -> PublicRepoConfig:
    return PublicRepoConfig(
        name=name,
        namespace=name,
        enable_charts=enable_charts,
        variables={"account_id": ACCOUNT_ID},
    )


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _load_public_repo_configs(tmp_path: Path, content: str) -> list[PublicRepoConfig]:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "config.toml").write_text(textwrap.dedent(content))
    handler = PublicRepoConfigHandler()
    load_configs(conf_dir, [handler])
    return handler.configs


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_load_public_repo_config_defaults(tmp_path: Path) -> None:
    """A minimal [[public-repo]] entry parses with enable-charts off."""
    configs = _load_public_repo_configs(
        tmp_path,
        """\
        [[public-repo]]
        name = "idcat"
        """,
    )

    assert len(configs) == 1
    config = configs[0]
    assert config.name == "idcat"
    assert config.namespace == "idcat"
    assert config.enable_charts is False


def test_load_public_repo_config_enable_charts(tmp_path: Path) -> None:
    """enable-charts can be turned on per entry."""
    configs = _load_public_repo_configs(
        tmp_path,
        """\
        [[public-repo]]
        name = "idcat"

        [[public-repo]]
        name = "randomsecret"
        enable-charts = true
        """,
    )

    assert [config.enable_charts for config in configs] == [False, True]


def test_load_public_repo_config_collects_variables(tmp_path: Path) -> None:
    """Top-level [variables] are made available for template rendering."""
    configs = _load_public_repo_configs(
        tmp_path,
        """\
        [variables]
        account_id = 123456789012

        [[public-repo]]
        name = "idcat"
        """,
    )

    assert configs[0].variables == {"account_id": 123456789012}


def test_load_public_repo_config_requires_name(tmp_path: Path) -> None:
    """The name field is required."""
    with pytest.raises(ValueError, match="Missing required field 'name'"):
        _load_public_repo_configs(
            tmp_path,
            """\
            [[public-repo]]
            enable-charts = true
            """,
        )


def test_load_public_repo_config_rejects_non_string_name(tmp_path: Path) -> None:
    """The name field must be a string."""
    with pytest.raises(ValueError, match="'name' must be a string"):
        _load_public_repo_configs(
            tmp_path,
            """\
            [[public-repo]]
            name = 42
            """,
        )


def test_load_public_repo_config_rejects_non_bool_enable_charts(
    tmp_path: Path,
) -> None:
    """The enable-charts field must be a boolean."""
    with pytest.raises(ValueError, match="'enable-charts' must be a boolean"):
        _load_public_repo_configs(
            tmp_path,
            """\
            [[public-repo]]
            name = "idcat"
            enable-charts = "yes"
            """,
        )


def test_load_public_repo_config_rejects_unknown_fields(tmp_path: Path) -> None:
    """Unknown fields are reported with their location."""
    with pytest.raises(ValueError, match="Unknown field in \\[\\[public-repo\\]\\]"):
        _load_public_repo_configs(
            tmp_path,
            """\
            [[public-repo]]
            name = "idcat"
            charts = true
            """,
        )


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------


def test_generate_public_repo_without_charts(tmp_path: Path) -> None:
    """A charts-less config produces a Repository, Role and RolePolicy."""
    output_dir = tmp_path / "output"
    paths = generate_public_repo(_make_config(), output_dir)

    assert paths == {
        output_dir / "idcat" / "repository-idcat.yaml",
        output_dir / "idcat" / "role-idcat-ecr-publish.yaml",
        output_dir / "idcat" / "rolepolicy-idcat-ecr-publish.yaml",
    }


def test_generate_public_repo_repository_contents(tmp_path: Path) -> None:
    """The image Repository names the repo after the config entry."""
    output_dir = tmp_path / "output"
    generate_public_repo(_make_config(), output_dir)

    repo = _read_yaml(output_dir / "idcat" / "repository-idcat.yaml")
    assert repo["kind"] == "Repository"
    assert repo["metadata"]["name"] == "idcat"
    assert repo["metadata"]["namespace"] == "idcat"
    assert repo["metadata"]["annotations"]["crossplane.io/external-name"] == "idcat"
    assert repo["spec"]["forProvider"]["region"] == "us-east-1"
    assert (
        repo["spec"]["forProvider"]["catalogData"]["description"]
        == "Official images built from https://github.com/portswigger/idcat main"
    )


def test_generate_public_repo_role_trusts_main_and_tags(tmp_path: Path) -> None:
    """The role trusts the main branch and any tag of the repo."""
    output_dir = tmp_path / "output"
    generate_public_repo(_make_config(), output_dir)

    role = _read_yaml(output_dir / "idcat" / "role-idcat-ecr-publish.yaml")
    assert role["metadata"]["name"] == "idcat-ecr-publish"
    assert role["spec"]["forProvider"]["path"] == "/product-roles/"
    assert (
        role["spec"]["forProvider"]["permissionsBoundary"]
        == f"arn:aws:iam::{ACCOUNT_ID}:policy/pipeline-policies/"
        "crossplane-approle-permissions-boundary"
    )

    assume = json.loads(role["spec"]["forProvider"]["assumeRolePolicy"])
    statement = assume["Statement"][0]
    assert statement["Principal"]["Federated"] == (
        f"arn:aws:iam::{ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
    )
    assert statement["Condition"]["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    }
    subs = statement["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:sub"
    ]
    assert subs == [
        "repo:PortSwigger/idcat:*",
        "repo:PortSwigger@*/idcat@*:*",
    ]
    refs = statement["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:ref"
    ]
    assert refs == [
        "refs/heads/main",
        "refs/tags/*",
    ]


def test_generate_public_repo_rolepolicy_resources(tmp_path: Path) -> None:
    """Without charts, the policy covers the image repository and registry."""
    output_dir = tmp_path / "output"
    generate_public_repo(_make_config(), output_dir)

    rolepolicy = _read_yaml(output_dir / "idcat" / "rolepolicy-idcat-ecr-publish.yaml")
    assert rolepolicy["spec"]["forProvider"]["role"] == "idcat-ecr-publish"

    policy = json.loads(rolepolicy["spec"]["forProvider"]["policy"])
    publish_statement = policy["Statement"][0]
    assert "ecr-public:PutImage" in publish_statement["Action"]
    assert publish_statement["Resource"] == [
        f"arn:aws:ecr-public::{ACCOUNT_ID}:repository/idcat",
        f"arn:aws:ecr-public::{ACCOUNT_ID}:registry/*",
    ]
    assert {s["Action"] for s in policy["Statement"][1:]} == {
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken",
    }


def test_generate_public_repo_with_charts(tmp_path: Path) -> None:
    """enable_charts adds a charts repository and covers it in the policy."""
    output_dir = tmp_path / "output"
    config = _make_config(name="randomsecret", enable_charts=True)
    paths = generate_public_repo(config, output_dir)

    assert paths == {
        output_dir / "randomsecret" / "repository-randomsecret.yaml",
        output_dir / "randomsecret" / "repository-charts-randomsecret.yaml",
        output_dir / "randomsecret" / "role-randomsecret-ecr-publish.yaml",
        output_dir / "randomsecret" / "rolepolicy-randomsecret-ecr-publish.yaml",
    }

    charts_repo = _read_yaml(
        output_dir / "randomsecret" / "repository-charts-randomsecret.yaml"
    )
    assert charts_repo["metadata"]["name"] == "charts-randomsecret"
    assert (
        charts_repo["metadata"]["annotations"]["crossplane.io/external-name"]
        == "charts/randomsecret"
    )
    assert (
        charts_repo["spec"]["forProvider"]["catalogData"]["description"]
        == "Helm charts for https://github.com/portswigger/randomsecret"
    )

    role = _read_yaml(
        output_dir / "randomsecret" / "role-randomsecret-ecr-publish.yaml"
    )
    assume = json.loads(role["spec"]["forProvider"]["assumeRolePolicy"])
    condition = assume["Statement"][0]["Condition"]
    assert condition["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    }
    subs = condition["StringLike"]["token.actions.githubusercontent.com:sub"]
    assert subs == [
        "repo:PortSwigger/randomsecret:*",
        "repo:PortSwigger@*/randomsecret@*:*",
    ]
    assert condition["StringLike"]["token.actions.githubusercontent.com:ref"] == [
        "refs/heads/main",
        "refs/tags/*",
    ]

    rolepolicy = _read_yaml(
        output_dir / "randomsecret" / "rolepolicy-randomsecret-ecr-publish.yaml"
    )
    policy = json.loads(rolepolicy["spec"]["forProvider"]["policy"])
    assert policy["Statement"][0]["Resource"] == [
        f"arn:aws:ecr-public::{ACCOUNT_ID}:repository/randomsecret",
        f"arn:aws:ecr-public::{ACCOUNT_ID}:repository/charts/randomsecret",
        f"arn:aws:ecr-public::{ACCOUNT_ID}:registry/*",
    ]


def test_generate_public_repo_requires_account_id(tmp_path: Path) -> None:
    """Generation fails loudly when the account_id variable is missing."""
    config = PublicRepoConfig(name="idcat", namespace="idcat")
    with pytest.raises(pystache.context.KeyNotFoundError):
        generate_public_repo(config, tmp_path / "output")
