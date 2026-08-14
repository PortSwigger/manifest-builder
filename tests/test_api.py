# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for the reusable manifest-builder API."""

from pathlib import Path
from typing import Any, cast
from unittest import mock
from unittest.mock import call

import pytest
import yaml
from conftest import init_test_repo
from dulwich import porcelain
from dulwich.objects import Commit
from dulwich.repo import Repo

from manifest_builder import (
    ExternalPlugins,
    GenerationResult,
    __version__,
    generate,
    get_version,
)
from manifest_builder.api import (
    DEPLOY_ID_ANNOTATION,
    _collect_generation_result,
    _load_api_variables,
    _load_system_owner_roots,
    _make_deploy_id,
    _object_ref_from_doc,
)
from manifest_builder.api import (
    generate as api_generate,
)
from manifest_builder.git_utils import GitManifestChanges, get_git_manifest_changes
from manifest_builder.result import KubernetesObjectRef

#: A minimal block, written into a config directory's plugins/ subdirectory.
#: Blocks other than copy belong to the configuration directory that uses them,
#: so an end-to-end API test brings its own, and covers plugin discovery on the
#: way through.
DEMO_PLUGIN = """\
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.output import write_documents


@dataclass
class DemoConfig:
    name: str
    namespace: str
    image: str


class DemoBlock(ConfigBlock[DemoConfig]):
    def __init__(self, configs: Sequence[DemoConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "demo"

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
            namespace = item.get("namespace", default_namespace)
            self.configs.append(
                DemoConfig(
                    name=item.get("name") or namespace,
                    namespace=namespace,
                    image=item.get("image") or default_image,
                )
            )

    def iter_configs(self) -> list[DemoConfig]:
        return self.configs

    def validate(self, config: DemoConfig, repo_root: Path) -> None:
        pass

    def generate(
        self,
        config: DemoConfig,
        context: GenerationContext,
    ) -> set[Path]:
        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": config.name, "namespace": config.namespace},
                "spec": {"image": config.image},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": config.name, "namespace": config.namespace},
                "spec": {},
            },
        ]
        return write_documents(
            documents, context.output_dir, config.namespace, app_name=config.name
        )
"""


def write_demo_plugin(config_dir: Path) -> None:
    """Give a config directory the demo block its config.toml declares."""
    plugins = config_dir / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "demo.py").write_text(DEMO_PLUGIN)


def test_generate_is_available_from_top_level_package() -> None:
    """Call sites can import generate directly from manifest_builder."""
    assert generate.__name__ == "generate"


def test_get_version_returns_the_package_version() -> None:
    """Call sites can ask which manifest-builder version they are running."""
    assert get_version() == __version__
    assert isinstance(get_version(), str)


def test_object_ref_carries_the_api_version() -> None:
    """A ref keeps the group that tells same-named kinds apart."""
    ref = _object_ref_from_doc(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "reader", "namespace": "idcat"},
        }
    )
    assert ref == KubernetesObjectRef(
        "Role", "idcat", "reader", "rbac.authorization.k8s.io/v1"
    )
    other = _object_ref_from_doc(
        {
            "apiVersion": "iam.aws.m.upbound.io/v1beta1",
            "kind": "Role",
            "metadata": {"name": "reader", "namespace": "idcat"},
        }
    )
    assert ref != other


@pytest.mark.parametrize("doc_api_version", [None, 3])
def test_object_ref_without_a_usable_api_version(doc_api_version: object) -> None:
    """A document that says nothing usable still yields a ref, with no group."""
    doc: dict[str, Any] = {
        "kind": "ConfigMap",
        "metadata": {"name": "old", "namespace": "idcat"},
    }
    if doc_api_version is not None:
        doc["apiVersion"] = doc_api_version
    assert _object_ref_from_doc(doc) == KubernetesObjectRef("ConfigMap", "idcat", "old")


def test_object_refs_sort_by_kind_namespace_and_name() -> None:
    """The api_version field is last, so it does not disturb the sort order."""
    refs = [
        KubernetesObjectRef("Role", "idcat", "reader", "iam.aws.m.upbound.io/v1beta1"),
        KubernetesObjectRef("ConfigMap", "idcat", "zed", "v1"),
        KubernetesObjectRef("ConfigMap", "idcat", "alpha", "v1"),
    ]
    assert [ref.name for ref in sorted(refs)] == ["alpha", "zed", "reader"]


def _commit_all(path: Path, message: bytes = b"commit") -> bytes:
    """Commit all changes in a temporary Dulwich repository."""
    porcelain.add(path)
    return porcelain.commit(
        path,
        message=message,
        author=b"Test User <test@example.com>",
        committer=b"Test User <test@example.com>",
    )


def test_generate_reports_changed_objects_and_adds_deploy_id(
    tmp_path: Path,
) -> None:
    """Generation result lists git changes and annotates changed objects."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    init_test_repo(config)
    init_test_repo(output)
    (config / "config.toml").write_text(
        """\
[[demo]]
namespace = "idcat"
image = "registry.example.com/idcat:1.0"
"""
    )
    write_demo_plugin(config)
    config_commit = _commit_all(config).decode("ascii")

    stale = output / "idcat" / "configmap-old.yaml"
    stale.parent.mkdir()
    stale.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: old
  namespace: idcat
"""
    )
    owners_dir = output / "owners"
    owners_dir.mkdir()
    (owners_dir / "system.toml").write_text('owned = "idcat"\n')
    _commit_all(output)

    result = api_generate(config, output, repo_root=tmp_path)

    deploy_id = _make_deploy_id(__version__, config_commit)
    assert result.deploy_id == deploy_id
    assert result.created_or_modified == {
        KubernetesObjectRef("Deployment", "idcat", "idcat", "apps/v1"),
        KubernetesObjectRef("Namespace", None, "idcat", "v1"),
        KubernetesObjectRef("Service", "idcat", "idcat", "v1"),
    }
    assert result.removed == {KubernetesObjectRef("ConfigMap", "idcat", "old", "v1")}

    for path in result.written_paths:
        if path.suffix != ".yaml":
            continue
        doc = yaml.safe_load(path.read_text())
        assert doc["metadata"]["annotations"][DEPLOY_ID_ANNOTATION] == deploy_id


def test_generate_reports_manifests_below_a_new_directory(tmp_path: Path) -> None:
    """The first manifests of an application are created, not invisible.

    A directory git has never seen is untracked as a whole, so the manifests
    below it have to be found through it rather than being reported in their
    own right.
    """
    output = tmp_path / "output"
    output.mkdir()
    init_test_repo(output)
    existing = output / "idcat" / "configmap-settings.yaml"
    existing.parent.mkdir()
    existing.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
"""
    )
    _commit_all(output, b"generated manifests")
    manifest = output / "blackbox-exporter" / "deployment-blackbox-exporter.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: blackbox-exporter
  namespace: observability
"""
    )
    config_commit = "a" * 40

    result = _collect_generation_result(
        output, {manifest}, config_commit, {"idcat", "blackbox-exporter"}
    )

    assert result.created_or_modified == {
        KubernetesObjectRef(
            "Deployment", "observability", "blackbox-exporter", "apps/v1"
        )
    }
    assert result.removed == set()
    doc = yaml.safe_load(manifest.read_text())
    assert doc["metadata"]["annotations"][DEPLOY_ID_ANNOTATION] == result.deploy_id


def test_generate_ignores_deploy_id_only_manifest_changes(tmp_path: Path) -> None:
    """A new deploy id alone should not make otherwise unchanged objects modified."""
    output = tmp_path / "output"
    output.mkdir()
    init_test_repo(output)
    manifest = output / "idcat" / "configmap-settings.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
  annotations:
    noa.re/deploy-id: old-deploy-id
data:
  key: value
"""
    )
    _commit_all(output, b"generated manifests")
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
data:
  key: value
"""
    )
    config_commit = "a" * 40

    result = _collect_generation_result(output, {manifest}, config_commit, {"idcat"})

    assert result.deploy_id == _make_deploy_id(__version__, config_commit)
    assert result.created_or_modified == set()
    assert result.removed == set()
    assert get_git_manifest_changes(output) == GitManifestChanges()


def test_generate_ignores_deploy_id_changes_with_null_annotations(
    tmp_path: Path,
) -> None:
    """A null annotations field is equivalent to no annotations."""
    output = tmp_path / "output"
    output.mkdir()
    init_test_repo(output)
    manifest = output / "loki" / "service-loki.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: loki
  namespace: loki
  annotations:
    noa.re/deploy-id: old-deploy-id
spec:
  type: ClusterIP
"""
    )
    _commit_all(output, b"generated manifests")
    manifest.write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: loki
  namespace: loki
  annotations: null
spec:
  type: ClusterIP
"""
    )
    config_commit = "a" * 40

    result = _collect_generation_result(output, {manifest}, config_commit, {"loki"})

    assert result.deploy_id == _make_deploy_id(__version__, config_commit)
    assert result.created_or_modified == set()
    assert result.removed == set()
    assert get_git_manifest_changes(output) == GitManifestChanges()


def test_generate_restores_deploy_id_only_changes_without_config_commit(
    tmp_path: Path,
) -> None:
    """Git-backed output preserves existing deploy ids when no new id is available."""
    output = tmp_path / "output"
    output.mkdir()
    init_test_repo(output)
    manifest = output / "idcat" / "configmap-settings.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
  annotations:
    noa.re/deploy-id: old-deploy-id
data:
  key: value
"""
    )
    _commit_all(output, b"generated manifests")
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
data:
  key: value
"""
    )

    result = _collect_generation_result(output, {manifest}, None, {"idcat"})

    assert result.deploy_id is None
    assert result.created_or_modified == set()
    assert result.removed == set()
    assert get_git_manifest_changes(output) == GitManifestChanges()
    assert (
        DEPLOY_ID_ANNOTATION
        in yaml.safe_load(manifest.read_text())["metadata"]["annotations"]
    )


def test_generate_restores_deploy_id_only_changes_with_unresolved_output_path(
    tmp_path: Path,
) -> None:
    """Managed-root filtering handles output paths containing parent segments."""
    output = tmp_path / "output"
    caller = tmp_path / "caller"
    output.mkdir()
    caller.mkdir()
    init_test_repo(output)
    manifest = output / "idcat" / "configmap-settings.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
  annotations:
    noa.re/deploy-id: old-deploy-id
data:
  key: value
"""
    )
    _commit_all(output, b"generated manifests")
    manifest.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: idcat
data:
  key: value
"""
    )

    result = _collect_generation_result(
        caller / ".." / "output",
        {manifest},
        None,
        {"idcat"},
    )

    assert result.created_or_modified == set()
    assert get_git_manifest_changes(output) == GitManifestChanges()


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.is_git_checkout", return_value=False)
def test_create_commit_requires_output_git_checkout(
    mock_is_git_checkout: mock.Mock,
    mock_generate_manifests: mock.Mock,
) -> None:
    """Commit creation fails fast when the output directory is not a git checkout."""
    output = Path("/tmp/out")

    try:
        api_generate(Path("conf"), output, create_commit=True)
    except ValueError as e:
        error = str(e)
    else:
        raise AssertionError("generate() should reject non-git commit output")

    assert (
        "It doesn't seem like /tmp/out is a git checkout, "
        "a requirement to be able to generate a commit."
    ) == error
    mock_is_git_checkout.assert_called_once_with(output)
    mock_generate_manifests.assert_not_called()


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value={"owned"})
@mock.patch("manifest_builder.api.load_images", return_value={"app": "image"})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_generate_accepts_config_and_output_paths(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    tmp_path: Path,
) -> None:
    """The reusable generation function accepts config and output Paths."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    (config / "releases.yaml").write_text("releases: []\n")
    generated = output / "app" / "app.yaml"
    mock_generate_manifests.return_value = {generated}

    result = api_generate(config, output, repo_root=tmp_path)

    system_owner = output / "owners" / "system.toml"
    assert result.written_paths == {generated, system_owner}
    assert system_owner.read_text() == 'owned = ["app"]\n'
    mock_load_helmfile.assert_called_once_with(config / "releases.yaml")
    mock_load_configs.assert_called_once_with(
        config,
        mock.ANY,
        extra_variables=None,
        default_namespace=None,
        default_image=None,
        target=None,
    )
    mock_resolve_configs.assert_called_once_with(["loaded"], None)
    mock_load_images.assert_called_once_with(config)
    mock_load_owned_namespaces.assert_has_calls(
        [
            call(config, exclude_owner_files={"system.toml"}),
            call(output, exclude_owner_files={"system.toml"}),
        ]
    )
    mock_generate_manifests.assert_called_once_with(
        blocks=["resolved"],
        output_dir=output,
        repo_root=tmp_path,
        images={"app": "image"},
        verbose=False,
        owned_namespaces={"owned"},
        managed_namespaces=None,
        cleanup=False,
    )


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value=set())
@mock.patch("manifest_builder.api.load_images", return_value={})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_generate_passes_vars_as_extra_variables(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    tmp_path: Path,
) -> None:
    """The API vars parameter is merged like --vars-from variables."""
    del (
        mock_load_helmfile,
        mock_resolve_configs,
        mock_load_images,
        mock_load_owned_namespaces,
    )
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    mock_generate_manifests.return_value = {output / "app" / "deployment-app.yaml"}

    api_generate(
        config,
        output,
        repo_root=tmp_path,
        vars={"cluster_name": "prod", "replica_count": 3, "use_tls": True},
    )

    mock_load_configs.assert_called_once_with(
        config,
        mock.ANY,
        extra_variables={
            "cluster_name": "prod",
            "replica_count": 3,
            "use_tls": True,
        },
        default_namespace=None,
        default_image=None,
        target=None,
    )


def test_generate_renders_copy_manifests_with_vars(tmp_path: Path) -> None:
    """Copy manifests are rendered with the same variables as --vars-from."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    source = config / "manifests"
    source.mkdir(parents=True)
    output.mkdir()
    (source / "configmap.yaml").write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  domain: {{domain}}
"""
    )
    (config / "config.toml").write_text(
        """\
[[copy]]
name = "thing"
namespace = "idcat"
source = "manifests"
"""
    )

    result = api_generate(
        config, output, repo_root=tmp_path, vars={"domain": "example.com"}
    )

    configmap = output / "idcat" / "configmap-settings.yaml"
    assert configmap in result.written_paths
    assert yaml.safe_load(configmap.read_text())["data"]["domain"] == "example.com"


def test_load_api_variables_merges_vars_from_and_vars(tmp_path: Path) -> None:
    """In-memory API variables can be combined with file-loaded variables."""
    vars_file = tmp_path / "extra.toml"
    vars_file.write_text('domain = "example.com"\n')

    assert _load_api_variables(
        tmp_path, Path("extra.toml"), {"cluster_name": "prod"}
    ) == {
        "domain": "example.com",
        "cluster_name": "prod",
    }


def test_load_api_variables_rejects_vars_from_conflict(tmp_path: Path) -> None:
    """Duplicate variables across API sources are rejected."""
    vars_file = tmp_path / "extra.toml"
    vars_file.write_text('domain = "example.com"\n')

    with pytest.raises(ValueError, match="'domain'"):
        _load_api_variables(tmp_path, Path("extra.toml"), {"domain": "other.com"})


def test_load_api_variables_rejects_nested_vars(tmp_path: Path) -> None:
    """API variables follow the same scalar-only rules as --vars-from."""
    bad_vars = yaml.safe_load("domain:\n  name: example.com\n")

    with pytest.raises(ValueError, match="'domain'.*string, number, or boolean"):
        _load_api_variables(tmp_path, None, bad_vars)


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value=set())
@mock.patch("manifest_builder.api.load_images", return_value={})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_generate_namespace_mode_writes_owner_file(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    tmp_path: Path,
) -> None:
    """Namespace mode declares ownership in the output owners directory."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    mock_generate_manifests.return_value = {output / "team-a" / "deployment-app.yaml"}

    result = api_generate(config, output, repo_root=tmp_path, namespace="team-a")

    owner = output / "owners" / "team-a.toml"
    assert owner in result.written_paths
    assert owner.read_text() == 'owned = "team-a"\n'
    mock_load_configs.assert_called_once_with(
        config,
        mock.ANY,
        extra_variables=None,
        default_namespace="team-a",
        default_image=None,
        target=None,
    )
    mock_load_owned_namespaces.assert_has_calls([call(config), call(output)])
    mock_generate_manifests.assert_called_once_with(
        blocks=["resolved"],
        output_dir=output,
        repo_root=tmp_path,
        images={},
        verbose=False,
        owned_namespaces=set(),
        managed_namespaces={"team-a"},
        cleanup=False,
    )


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value=set())
@mock.patch("manifest_builder.api.load_images", return_value={})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_generate_namespace_mode_passes_image_default(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    tmp_path: Path,
) -> None:
    """The API image parameter is passed as a namespace-mode config default."""
    del (
        mock_load_helmfile,
        mock_resolve_configs,
        mock_load_images,
        mock_load_owned_namespaces,
    )
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    mock_generate_manifests.return_value = {output / "team-a" / "deployment-app.yaml"}

    api_generate(
        config,
        output,
        repo_root=tmp_path,
        namespace="team-a",
        image="registry.example.com/app:1.0",
    )

    mock_load_configs.assert_called_once_with(
        config,
        mock.ANY,
        extra_variables=None,
        default_namespace="team-a",
        default_image="registry.example.com/app:1.0",
        target=None,
    )


def test_generate_image_requires_namespace(tmp_path: Path) -> None:
    """The API image parameter only has meaning in namespace mode."""
    try:
        api_generate(
            tmp_path / "config",
            tmp_path / "output",
            repo_root=tmp_path,
            image="registry.example.com/app:1.0",
        )
    except ValueError as e:
        error = str(e)
    else:
        raise AssertionError("generate() should reject image without namespace")

    assert error == "generate(image=...) can only be used when namespace is set"


def test_load_system_owner_roots_accepts_owned_string(tmp_path: Path) -> None:
    """System ownership may contain a single owned output root."""
    owners_dir = tmp_path / "output" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "system.toml").write_text('owned = "cluster"\n')

    assert _load_system_owner_roots(tmp_path / "output") == {"cluster"}


def test_load_system_owner_roots_accepts_owned_list(tmp_path: Path) -> None:
    """System ownership may contain a list of owned output roots."""
    owners_dir = tmp_path / "output" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "system.toml").write_text('owned = ["cluster", "team-a"]\n')

    assert _load_system_owner_roots(tmp_path / "output") == {"cluster", "team-a"}


def test_load_system_owner_roots_rejects_invalid_owned_value(tmp_path: Path) -> None:
    """System ownership must be a string or a list of strings."""
    owners_dir = tmp_path / "output" / "owners"
    owners_dir.mkdir(parents=True)
    (owners_dir / "system.toml").write_text("owned = 42\n")

    with pytest.raises(ValueError, match="'owned' must be a string or list"):
        _load_system_owner_roots(tmp_path / "output")


def test_system_mode_reconciles_system_owner_roots_and_commit(
    tmp_path: Path,
) -> None:
    """System mode clears previously owned roots and syncs owners/system.toml."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    config_repo = init_test_repo(config)
    config_file = config_repo.get_config()
    config_file.set((b"remote", b"origin"), b"url", b"https://example.com/config.git")
    config_file.write_to_path()
    config_repo.close()
    (config / "config.toml").write_text(
        """\
[[demo]]
namespace = "team-a"
image = "registry.example.com/team-a:1.0"
"""
    )
    write_demo_plugin(config)
    _commit_all(config)

    init_test_repo(output)
    team_stale = output / "team-a" / "configmap-old.yaml"
    team_stale.parent.mkdir()
    team_stale.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: old
  namespace: team-a
"""
    )
    old_stale = output / "old-ns" / "configmap-old.yaml"
    old_stale.parent.mkdir()
    old_stale.write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: old
  namespace: old-ns
"""
    )
    owners_dir = output / "owners"
    owners_dir.mkdir()
    (owners_dir / "system.toml").write_text('owned = ["old-ns", "team-a"]\n')
    _commit_all(output)

    result = api_generate(config, output, repo_root=tmp_path, create_commit=True)

    assert output / "team-a" / "deployment-team-a.yaml" in result.written_paths
    assert not team_stale.exists()
    assert not old_stale.exists()
    assert (owners_dir / "system.toml").read_text() == 'owned = ["team-a"]\n'
    assert result.removed == {
        KubernetesObjectRef("ConfigMap", "old-ns", "old", "v1"),
        KubernetesObjectRef("ConfigMap", "team-a", "old", "v1"),
    }
    status = porcelain.status(output)
    assert status.unstaged == []
    assert status.staged == {"add": [], "delete": [], "modify": []}


def test_generate_with_plugins_from_outside_the_config_repo(tmp_path: Path) -> None:
    """Config that does not carry its own plugins can be given them separately."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    plugins = tmp_path / "plugins-checkout" / "plugins"
    config.mkdir()
    output.mkdir()
    config_repo = init_test_repo(config)
    config_file = config_repo.get_config()
    config_file.set((b"remote", b"origin"), b"url", b"https://example.com/config.git")
    config_file.write_to_path()
    config_repo.close()
    (config / "config.toml").write_text(
        """\
[[demo]]
namespace = "team-a"
image = "registry.example.com/team-a:1.0"
"""
    )
    _commit_all(config)

    plugins.mkdir(parents=True)
    (plugins / "demo.py").write_text(DEMO_PLUGIN)
    init_test_repo(output)
    _commit_all(output)

    result = api_generate(
        config,
        output,
        repo_root=tmp_path,
        create_commit=True,
        plugins=ExternalPlugins(
            path=plugins, source="https://example.com/plugins.git@def456"
        ),
    )

    assert output / "team-a" / "deployment-team-a.yaml" in result.written_paths
    with Repo.discover(output) as repo:
        message = cast(Commit, repo[repo.head()]).message
    assert b"Plugins from: https://example.com/plugins.git@def456\n" in message


def test_generate_resolves_a_relative_plugins_path_against_repo_root(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    (config / "config.toml").write_text(
        """\
[[demo]]
namespace = "team-a"
image = "registry.example.com/team-a:1.0"
"""
    )
    plugins = tmp_path / "elsewhere" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "demo.py").write_text(DEMO_PLUGIN)

    result = api_generate(
        config,
        output,
        repo_root=tmp_path,
        plugins=ExternalPlugins(path=Path("elsewhere/plugins"), source="test"),
    )

    assert output / "team-a" / "deployment-team-a.yaml" in result.written_paths


@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value=set())
@mock.patch("manifest_builder.api.load_images", return_value={})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_generate_namespace_mode_rejects_cluster_output(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    tmp_path: Path,
) -> None:
    """Namespace mode fails when any generated file lands in cluster/."""
    del (
        mock_load_helmfile,
        mock_load_configs,
        mock_resolve_configs,
        mock_load_images,
        mock_load_owned_namespaces,
    )
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    mock_generate_manifests.return_value = {output / "cluster" / "clusterrole-app.yaml"}

    try:
        api_generate(config, output, repo_root=tmp_path, namespace="team-a")
    except ValueError as e:
        error = str(e)
    else:
        raise AssertionError("generate() should reject cluster output")

    assert "--namespace mode cannot generate cluster-scoped manifests" in error
    assert not (output / "owners" / "team-a.toml").exists()


@mock.patch("manifest_builder.api.create_manifest_commit")
@mock.patch("manifest_builder.api.get_git_tracked_remote", return_value="config.git")
@mock.patch("manifest_builder.api.get_git_commit_subject", return_value="Config change")
@mock.patch("manifest_builder.api.get_git_commit", return_value="abc123")
@mock.patch("manifest_builder.api.get_git_manifest_changes")
@mock.patch("manifest_builder.api.is_git_dirty", return_value=False)
@mock.patch("manifest_builder.api.is_git_checkout", return_value=True)
@mock.patch("manifest_builder.api.generate_manifests")
@mock.patch("manifest_builder.api.load_owned_namespaces", return_value=set())
@mock.patch("manifest_builder.api.load_images", return_value={})
@mock.patch("manifest_builder.api.resolve_configs", return_value=["resolved"])
@mock.patch("manifest_builder.api.load_configs", return_value=["loaded"])
@mock.patch("manifest_builder.api.load_helmfile", return_value=None)
def test_namespace_mode_commit_preserves_non_target_directories(
    mock_load_helmfile: mock.Mock,
    mock_load_configs: mock.Mock,
    mock_resolve_configs: mock.Mock,
    mock_load_images: mock.Mock,
    mock_load_owned_namespaces: mock.Mock,
    mock_generate_manifests: mock.Mock,
    mock_is_git_checkout: mock.Mock,
    mock_is_git_dirty: mock.Mock,
    mock_get_git_manifest_changes: mock.Mock,
    mock_get_git_commit: mock.Mock,
    mock_get_git_commit_subject: mock.Mock,
    mock_get_git_tracked_remote: mock.Mock,
    mock_create_manifest_commit: mock.Mock,
    tmp_path: Path,
) -> None:
    """Namespace-mode commits stage only the target namespace and owner file."""
    mock_get_git_manifest_changes.return_value = GitManifestChanges()
    del (
        mock_load_helmfile,
        mock_load_configs,
        mock_resolve_configs,
        mock_load_images,
        mock_load_owned_namespaces,
        mock_is_git_checkout,
        mock_is_git_dirty,
        mock_get_git_manifest_changes,
        mock_get_git_commit,
        mock_get_git_commit_subject,
        mock_get_git_tracked_remote,
    )
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    (output / "team-a").mkdir(parents=True)
    (output / "team-b").mkdir()
    (output / "cluster").mkdir()
    generated = output / "team-a" / "deployment-app.yaml"
    mock_generate_manifests.return_value = {generated}

    result = api_generate(
        config,
        output,
        repo_root=tmp_path,
        namespace="team-a",
        create_commit=True,
    )

    assert generated in result.written_paths
    mock_create_manifest_commit.assert_called_once()
    assert mock_create_manifest_commit.call_args.args[2:5] == (
        "config.git",
        "abc123",
        "Config change",
    )
    assert mock_create_manifest_commit.call_args.args[6] == {
        output / "team-a",
        output / "owners" / "team-a.toml",
    }


def test_namespace_mode_commit_does_not_stage_preexisting_cluster_deletion(
    tmp_path: Path,
) -> None:
    """Namespace mode must not commit unrelated deletions already in the checkout."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    config.mkdir()
    output.mkdir()
    config_repo = init_test_repo(config)
    config_file = config_repo.get_config()
    config_file.set((b"remote", b"origin"), b"url", b"https://example.com/config.git")
    config_file.write_to_path()
    config_repo.close()
    (config / "config.toml").write_text(
        """\
[[demo]]
image = "registry.example.com/team-a:1.0"
"""
    )
    write_demo_plugin(config)
    _commit_all(config)

    init_test_repo(output)
    protected = output / "cluster" / "clusterrole-system:metrics-server.yaml"
    protected.parent.mkdir()
    protected.write_text(
        """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: system:metrics-server
rules: []
"""
    )
    _commit_all(output)
    protected.unlink()

    result = api_generate(
        config,
        output,
        repo_root=tmp_path,
        namespace="team-a",
        create_commit=True,
    )

    assert result.removed == set()
    assert output / "team-a" / "deployment-team-a.yaml" in result.written_paths
    status = porcelain.status(output)
    assert status.staged == {"add": [], "delete": [], "modify": []}
    assert status.unstaged == [b"cluster/clusterrole-system:metrics-server.yaml"]


@mock.patch(
    "manifest_builder.api.generate",
    return_value=GenerationResult(written_paths={Path("/out/app.yaml")}),
)
def test_top_level_generate_delegates_to_api(mock_generate: mock.Mock) -> None:
    """The top-level convenience import calls the API implementation."""
    result = generate(
        Path("conf"),
        Path("output"),
        repo_root=Path("/repo"),
        verbose=True,
        create_commit=True,
        allow_dirty_config=True,
    )

    assert result.written_paths == {Path("/out/app.yaml")}
    mock_generate.assert_called_once_with(
        Path("conf"),
        Path("output"),
        Path("/repo"),
        True,
        True,
        True,
        vars_from=None,
        namespace=None,
        image=None,
        vars=None,
        target=None,
        plugins=None,
    )


@mock.patch(
    "manifest_builder.api.generate",
    return_value=GenerationResult(written_paths={Path("/out/app.yaml")}),
)
def test_top_level_generate_passes_namespace_image(mock_generate: mock.Mock) -> None:
    """The top-level convenience wrapper exposes the image override."""
    result = generate(
        Path("conf"),
        Path("output"),
        repo_root=Path("/repo"),
        namespace="team-a",
        image="registry.example.com/app:1.0",
    )

    assert result.written_paths == {Path("/out/app.yaml")}
    mock_generate.assert_called_once_with(
        Path("conf"),
        Path("output"),
        Path("/repo"),
        False,
        False,
        False,
        vars_from=None,
        namespace="team-a",
        image="registry.example.com/app:1.0",
        vars=None,
        target=None,
        plugins=None,
    )


@mock.patch(
    "manifest_builder.api.generate",
    return_value=GenerationResult(written_paths={Path("/out/app.yaml")}),
)
def test_top_level_generate_passes_vars(mock_generate: mock.Mock) -> None:
    """The top-level convenience wrapper exposes direct API variables."""
    result = generate(
        Path("conf"),
        Path("output"),
        repo_root=Path("/repo"),
        vars_from=Path("extra.toml"),
        vars={"domain": "example.com"},
    )

    assert result.written_paths == {Path("/out/app.yaml")}
    mock_generate.assert_called_once_with(
        Path("conf"),
        Path("output"),
        Path("/repo"),
        False,
        False,
        False,
        vars_from=Path("extra.toml"),
        namespace=None,
        image=None,
        vars={"domain": "example.com"},
        target=None,
        plugins=None,
    )


@mock.patch(
    "manifest_builder.api.generate",
    return_value=GenerationResult(written_paths={Path("/out/app.yaml")}),
)
def test_top_level_generate_passes_target(mock_generate: mock.Mock) -> None:
    """The top-level convenience wrapper selects a target."""
    result = generate(
        Path("conf"),
        Path("output"),
        repo_root=Path("/repo"),
        target="dev",
    )

    assert result.written_paths == {Path("/out/app.yaml")}
    mock_generate.assert_called_once_with(
        Path("conf"),
        Path("output"),
        Path("/repo"),
        False,
        False,
        False,
        vars_from=None,
        namespace=None,
        image=None,
        vars=None,
        target="dev",
        plugins=None,
    )


def test_generate_from_a_target(tmp_path: Path) -> None:
    """A version = 2 config directory generates the selected target's sections."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    output.mkdir()

    base = config / "base"
    base.mkdir(parents=True)
    (base / "config.toml").write_text(
        """\
[[copy]]
name = "base-settings"
namespace = "base"
source = "manifests"
"""
    )
    base_manifests = base / "manifests"
    base_manifests.mkdir()
    (base_manifests / "configmap.yaml").write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: base-settings
data:
  cluster: {{cluster_name}}
"""
    )

    platform = config / "platform"
    platform.mkdir()
    (platform / "config.toml").write_text(
        """\
[[copy]]
name = "platform-settings"
namespace = "platform"
source = "manifests"
"""
    )
    platform_manifests = platform / "manifests"
    platform_manifests.mkdir()
    (platform_manifests / "configmap.yaml").write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-settings
data:
  domain: {{vanity_domain}}
"""
    )

    (config / "config.toml").write_text(
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
sections = ["base"]
[target.vars]
cluster_name = "platform-prod"
"""
    )

    result = api_generate(config, output, repo_root=tmp_path, target="platform-dev")

    base_cm = output / "base" / "configmap-base-settings.yaml"
    platform_cm = output / "platform" / "configmap-platform-settings.yaml"
    assert base_cm in result.written_paths
    assert platform_cm in result.written_paths
    assert yaml.safe_load(base_cm.read_text())["data"]["cluster"] == "platform-dev"
    assert (
        yaml.safe_load(platform_cm.read_text())["data"]["domain"] == "portswigger.com"
    )


def test_generate_from_a_target_omitting_a_section(tmp_path: Path) -> None:
    """A target generates only the sections it names."""
    config = tmp_path / "config"
    output = tmp_path / "output"
    output.mkdir()

    for section in ("base", "platform"):
        section_dir = config / section
        manifests = section_dir / "manifests"
        manifests.mkdir(parents=True)
        (section_dir / "config.toml").write_text(
            f"""\
[[copy]]
name = "{section}-settings"
namespace = "{section}"
source = "manifests"
"""
        )
        (manifests / "configmap.yaml").write_text(
            f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {section}-settings
data:
  cluster: {{{{cluster_name}}}}
"""
        )

    (config / "config.toml").write_text(
        """\
version = 2

[[target]]
name = "small"
sections = ["base"]
[target.vars]
cluster_name = "small-cluster"
"""
    )

    result = api_generate(config, output, repo_root=tmp_path, target="small")

    assert output / "base" / "configmap-base-settings.yaml" in result.written_paths
    assert not (output / "platform").exists()
