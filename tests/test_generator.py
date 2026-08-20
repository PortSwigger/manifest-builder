# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for generation orchestration and manifest writing."""

import logging
from pathlib import Path

import pytest
import yaml
from conftest import ProbeBlock, ProbeConfig

from manifest_builder.generator import _ensure_namespaces, generate_manifests
from manifest_builder.k8s import make_k8s_name
from manifest_builder.output import strip_helm_metadata, write_manifests

NAMESPACED_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
  namespace: production
spec: {}
"""

CLUSTER_SCOPED_YAML = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: my-role
rules: []
"""

MULTI_DOC_YAML = NAMESPACED_YAML + "---\n" + CLUSTER_SCOPED_YAML


def test_write_manifests_namespaced_resource(tmp_path: Path) -> None:
    paths = write_manifests(NAMESPACED_YAML, tmp_path, "default")

    assert len(paths) == 1
    (path,) = paths
    # namespace from metadata overrides the passed-in namespace
    assert path == tmp_path / "production" / "deployment-myapp.yaml"
    assert path.exists()


def test_write_manifests_uses_chart_namespace_as_fallback(tmp_path: Path) -> None:
    yaml_without_ns = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-config
data: {}
"""
    paths = write_manifests(yaml_without_ns, tmp_path, "fallback-ns")
    (path,) = paths
    assert path.parent.name == "fallback-ns"


def test_write_manifests_cluster_scoped_resource(tmp_path: Path) -> None:
    paths = write_manifests(CLUSTER_SCOPED_YAML, tmp_path, "default")

    assert len(paths) == 1
    (path,) = paths
    assert path == tmp_path / "cluster" / "clusterrole-my-role.yaml"
    assert path.exists()


def test_write_manifests_crossplane_cluster_provider_config_is_cluster_scoped(
    tmp_path: Path,
) -> None:
    """Crossplane ClusterProviderConfig resources do not get a namespace."""
    content = """\
apiVersion: aws.m.upbound.io/v1beta1
kind: ClusterProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: InjectedIdentity
"""

    paths = write_manifests(content, tmp_path, "default")

    assert len(paths) == 1
    (path,) = paths
    assert path == tmp_path / "cluster" / "clusterproviderconfig-default.yaml"
    doc = yaml.safe_load(path.read_text())
    assert "namespace" not in doc.get("metadata", {})


@pytest.mark.parametrize(
    ("api_version", "kind", "filename"),
    [
        ("pkg.crossplane.io/v1", "Provider", "provider-provider-aws-iam.yaml"),
        (
            "pkg.crossplane.io/v1beta1",
            "DeploymentRuntimeConfig",
            "deploymentruntimeconfig-provider-aws-iam.yaml",
        ),
        ("pkg.crossplane.io/v1", "Function", "function-provider-aws-iam.yaml"),
    ],
)
def test_write_manifests_crossplane_pkg_kinds_are_cluster_scoped(
    tmp_path: Path, api_version: str, kind: str, filename: str
) -> None:
    """The crossplane pkg.crossplane.io kinds do not get a namespace.

    Unlike most custom resources these cannot be learned from a
    CustomResourceDefinition: crossplane applies its own CRDs at runtime and its
    Helm chart ships none, so nothing in a generated manifest set states their
    scope and the static list has to carry them.
    """
    content = f"""\
apiVersion: {api_version}
kind: {kind}
metadata:
  name: provider-aws-iam
spec: {{}}
"""

    paths = write_manifests(content, tmp_path, "crossplane-system")

    assert len(paths) == 1
    (path,) = paths
    assert path == tmp_path / "cluster" / filename
    doc = yaml.safe_load(path.read_text())
    assert "namespace" not in doc.get("metadata", {})


def test_write_manifests_crd_scope_beats_the_static_list(tmp_path: Path) -> None:
    """A CRD in the same document set overrides the static list.

    Guards the precedence that keeps the list a fallback rather than an override:
    an unrelated Provider kind that a chart declares Namespaced is treated that
    way, so listing the crossplane kinds cannot capture another group's.
    """
    content = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: providers.example.invalid
spec:
  group: example.invalid
  scope: Namespaced
  names:
    kind: Provider
---
apiVersion: example.invalid/v1
kind: Provider
metadata:
  name: not-crossplane
spec: {}
"""

    paths = write_manifests(content, tmp_path, "somewhere")

    provider = next(p for p in paths if p.name.startswith("provider-not-crossplane"))
    assert provider.parent.name == "somewhere"


def test_write_manifests_replaces_colons_in_filenames(tmp_path: Path) -> None:
    """Object names with colons keep their metadata but get safe filenames."""
    content = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: system:metrics-server
rules: []
"""
    paths = write_manifests(content, tmp_path, "default")

    assert len(paths) == 1
    (path,) = paths
    assert path == tmp_path / "cluster" / "clusterrole-system_metrics-server.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["metadata"]["name"] == "system:metrics-server"


def test_write_manifests_multi_document(tmp_path: Path) -> None:
    paths = write_manifests(MULTI_DOC_YAML, tmp_path, "default")
    assert len(paths) == 2
    filenames = {p.name for p in paths}
    assert filenames == {"deployment-myapp.yaml", "clusterrole-my-role.yaml"}


def test_write_manifests_skips_empty_documents(tmp_path: Path) -> None:
    yaml_with_empty = "---\n" + NAMESPACED_YAML + "---\n"
    paths = write_manifests(yaml_with_empty, tmp_path, "default")
    assert len(paths) == 1


def test_write_manifests_summarizes_skipped_helm_hooks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Helm hook objects should be summarized at info level and detailed at debug."""
    yaml_with_hooks = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: my-hook
  annotations:
    helm.sh/hook: post-install
spec: {}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-other-hook
  annotations:
    helm.sh/hook: pre-install
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-config
data: {}
"""
    caplog.set_level(logging.DEBUG, logger="manifest_builder.output")

    paths = write_manifests(yaml_with_hooks, tmp_path, "default")

    assert len(paths) == 1
    assert "Skipped 2 helm hook objects" in caplog.text
    assert "Skipping Job my-hook (helm.sh/hook=post-install)" in caplog.text
    assert "Skipping ServiceAccount my-other-hook (helm.sh/hook=pre-install)" in (
        caplog.text
    )
    summary_records = [
        record
        for record in caplog.records
        if record.message == "Skipped 2 helm hook objects"
    ]
    detail_records = [
        record for record in caplog.records if record.message.startswith("Skipping ")
    ]
    assert [record.levelno for record in summary_records] == [logging.INFO]
    assert {record.levelno for record in detail_records} == {logging.DEBUG}


def test_generate_manifests_summarizes_chart_cache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Chart cache hit/miss details should be summarized once per generation run."""
    config = ProbeConfig(
        name="my-chart",
        namespace="default",
        documents=NAMESPACED_YAML,
        cache_hits=1,
    )
    caplog.set_level(logging.INFO, logger="manifest_builder.generator")

    generate_manifests(
        [ProbeBlock([config])],
        tmp_path / "out",
        repo_root=tmp_path,
    )

    assert "Chart cache: 1 hit, 0 misses" in caplog.text


def test_generate_manifests_rejects_config_in_owned_namespace(tmp_path: Path) -> None:
    """Configs targeting an externally-owned namespace must be rejected."""
    config = ProbeConfig(name="my-app", namespace="team-a")

    with pytest.raises(ValueError, match="owned by another service"):
        generate_manifests(
            [ProbeBlock([config])],
            tmp_path / "out",
            repo_root=tmp_path,
            owned_namespaces={"team-a"},
        )


def test_generate_manifests_preserves_files_in_owned_namespace(tmp_path: Path) -> None:
    """Pre-existing files in owned namespace directories must survive cleanup."""
    output_dir = tmp_path / "out"
    owned_file = output_dir / "team-a" / "configmap-foo.yaml"
    owned_file.parent.mkdir(parents=True)
    owned_file.write_text("# owned by team-a\n")

    config = ProbeConfig(name="my-app", namespace="default", documents=NAMESPACED_YAML)

    written = generate_manifests(
        [ProbeBlock([config])],
        output_dir,
        repo_root=tmp_path,
        owned_namespaces={"team-a"},
    )

    assert owned_file.exists()
    assert owned_file not in written


def test_generate_manifests_cleans_only_managed_namespaces(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Namespace-scoped generation must not remove files outside its namespace."""
    output_dir = tmp_path / "out"
    target_stale = output_dir / "team-a" / "configmap-old.yaml"
    other_stale = output_dir / "team-b" / "configmap-other.yaml"
    cluster_stale = output_dir / "cluster" / "clusterrole-other.yaml"
    for path in (target_stale, other_stale, cluster_stale):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stale\n")

    config = ProbeConfig(
        name="my-app",
        namespace="team-a",
        documents="""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: fresh
data: {}
""",
    )

    with caplog.at_level(logging.DEBUG, logger="manifest_builder.generator"):
        written = generate_manifests(
            [ProbeBlock([config])],
            output_dir,
            repo_root=tmp_path,
            managed_namespaces={"team-a"},
        )

    assert output_dir / "team-a" / "configmap-fresh.yaml" in written
    assert not target_stale.exists()
    assert other_stale.exists()
    assert cluster_stale.exists()
    assert not (output_dir / "team-b" / "namespace-team-b.yaml").exists()
    assert (
        "Deleted stale manifest during generation cleanup: team-a/configmap-old.yaml"
    ) in caplog.messages


def test_generate_manifests_rejects_output_landing_in_owned_namespace(
    tmp_path: Path,
) -> None:
    """A doc whose metadata.namespace targets an owned namespace must fail."""
    config = ProbeConfig(
        name="my-app",
        namespace="default",
        documents="""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: foo
  namespace: team-a
data: {}
""",
    )

    with pytest.raises(ValueError, match="owned by another service"):
        generate_manifests(
            [ProbeBlock([config])],
            tmp_path / "out",
            repo_root=tmp_path,
            owned_namespaces={"team-a"},
        )


def test_strip_helm_metadata_removes_helm_labels() -> None:
    doc = {
        "metadata": {
            "labels": {
                "app": "myapp",
                "helm.sh/chart": "mychart-1.0.0",
                "app.kubernetes.io/managed-by": "Helm",
            }
        }
    }
    strip_helm_metadata(doc)
    assert doc["metadata"]["labels"] == {"app": "myapp"}


def test_strip_helm_metadata_removes_helm_annotations() -> None:
    doc = {
        "metadata": {
            "annotations": {
                "helm.sh/hook": "post-install",
                "helm.sh/hook-weight": "1",
                "custom.io/keep": "yes",
            }
        }
    }
    strip_helm_metadata(doc)
    assert doc["metadata"]["annotations"] == {"custom.io/keep": "yes"}


def test_strip_helm_metadata_removes_empty_dicts() -> None:
    doc = {
        "metadata": {
            "labels": {"helm.sh/chart": "mychart-1.0.0"},
            "annotations": {"helm.sh/hook": "post-install"},
        }
    }
    strip_helm_metadata(doc)
    assert "labels" not in doc["metadata"]
    assert "annotations" not in doc["metadata"]


def test_strip_helm_metadata_strips_pod_template() -> None:
    doc = {
        "metadata": {"labels": {"helm.sh/chart": "mychart-1.0.0", "app": "myapp"}},
        "spec": {
            "template": {
                "metadata": {
                    "labels": {"helm.sh/chart": "mychart-1.0.0", "app": "myapp"},
                    "annotations": {"helm.sh/hook": "post-install"},
                }
            }
        },
    }
    strip_helm_metadata(doc)
    assert doc["metadata"]["labels"] == {"app": "myapp"}
    assert doc["spec"]["template"]["metadata"]["labels"] == {"app": "myapp"}
    assert "annotations" not in doc["spec"]["template"]["metadata"]


def test_strip_helm_metadata_preserves_non_helm_managed_by() -> None:
    doc = {
        "metadata": {
            "labels": {"app.kubernetes.io/managed-by": "ArgoCD", "app": "myapp"}
        }
    }
    strip_helm_metadata(doc)
    assert doc["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "ArgoCD",
        "app": "myapp",
    }


def test_strip_helm_metadata_handles_null_labels() -> None:
    """strip_helm_metadata should not crash when labels or annotations is null in the YAML."""
    doc = {"metadata": {"labels": None, "annotations": None}}
    strip_helm_metadata(doc)
    assert doc["metadata"]["labels"] is None
    assert doc["metadata"]["annotations"] is None


def test_write_manifests_handles_null_annotations(tmp_path: Path) -> None:
    """Documents emitted by the cilium chart contain `annotations:` with a null value.

    The hook-filter code path reaches `.get("annotations", {}).get("helm.sh/hook")`;
    because `.get(key, default)` only uses the default when the key is absent,
    a null annotations value used to surface as `AttributeError: 'NoneType' object
    has no attribute 'get'`. See cilium 1.19.3 Namespace/cilium-secrets.
    """
    cilium_namespace_yaml = """\
apiVersion: v1
kind: Namespace
metadata:
  name: "cilium-secrets"
  labels:
    app.kubernetes.io/part-of: cilium
  annotations:
"""
    paths = write_manifests(cilium_namespace_yaml, tmp_path, "default")
    assert len(paths) == 1


INGRESS_CLASS_PARAMS_CRD_YAML = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: ingressclassparams.elbv2.k8s.aws
spec:
  group: elbv2.k8s.aws
  scope: Cluster
  names:
    kind: IngressClassParams
    plural: ingressclassparams
"""

LIST_YAML = """\
apiVersion: v1
kind: List
metadata:
  name: ingress-class
  namespace: kube-system
items:
- apiVersion: elbv2.k8s.aws/v1beta1
  kind: IngressClassParams
  metadata:
    name: alb
    labels:
      app.kubernetes.io/managed-by: Helm
      helm.sh/chart: aws-load-balancer-controller-3.4.1
- apiVersion: networking.k8s.io/v1
  kind: IngressClass
  metadata:
    name: alb
  spec:
    controller: ingress.k8s.aws/alb
"""


def test_write_manifests_splits_list_into_one_file_per_item(tmp_path: Path) -> None:
    """A `kind: List` wrapper is expanded into a file per item it carries."""
    paths = write_manifests(LIST_YAML, tmp_path, "default")

    assert paths == {
        tmp_path / "kube-system" / "ingressclassparams-alb.yaml",
        tmp_path / "cluster" / "ingressclass-alb.yaml",
    }
    # the namespaced item inherits the list's namespace, and helm metadata is
    # stripped from items just like from top-level documents
    params = yaml.safe_load(
        (tmp_path / "kube-system" / "ingressclassparams-alb.yaml").read_text()
    )
    assert params["metadata"]["namespace"] == "kube-system"
    assert "labels" not in params["metadata"]
    # the cluster-scoped item stays free of a namespace
    ingress_class = yaml.safe_load(
        (tmp_path / "cluster" / "ingressclass-alb.yaml").read_text()
    )
    assert "namespace" not in ingress_class["metadata"]


def test_write_manifests_reads_scope_from_bundled_crd(tmp_path: Path) -> None:
    """A CRD shipped by the chart settles the scope of the kinds it defines."""
    paths = write_manifests(
        INGRESS_CLASS_PARAMS_CRD_YAML + "---\n" + LIST_YAML, tmp_path, "default"
    )

    assert (tmp_path / "cluster" / "ingressclassparams-alb.yaml") in paths, (
        "IngressClassParams is Cluster-scoped per the CRD"
    )
    params = yaml.safe_load(
        (tmp_path / "cluster" / "ingressclassparams-alb.yaml").read_text()
    )
    assert "namespace" not in params["metadata"]


def test_write_manifests_crd_can_mark_a_kind_namespaced(tmp_path: Path) -> None:
    """A Namespaced CRD wins over the built-in cluster-scoped kind names."""
    content = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: ingressclasses.example.com
spec:
  group: example.com
  scope: Namespaced
  names:
    kind: IngressClass
    plural: ingressclasses
---
apiVersion: example.com/v1
kind: IngressClass
metadata:
  name: mine
---
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: builtin
"""
    paths = write_manifests(content, tmp_path, "default")

    assert (tmp_path / "default" / "ingressclass-mine.yaml") in paths
    # the CRD only speaks for its own group, so the built-in IngressClass of
    # networking.k8s.io keeps its cluster scope
    assert (tmp_path / "cluster" / "ingressclass-builtin.yaml") in paths


def test_write_manifests_list_items_fall_back_to_chart_namespace(
    tmp_path: Path,
) -> None:
    """List items get the chart namespace when the list declares none."""
    content = """\
apiVersion: v1
kind: List
metadata:
  name: configs
items:
- apiVersion: v1
  kind: ConfigMap
  metadata:
    name: my-config
  data: {}
"""
    paths = write_manifests(content, tmp_path, "fallback-ns")

    assert paths == {tmp_path / "fallback-ns" / "configmap-my-config.yaml"}


def test_write_manifests_expands_nested_lists(tmp_path: Path) -> None:
    content = """\
apiVersion: v1
kind: List
metadata:
  name: outer
items:
- apiVersion: v1
  kind: List
  metadata:
    name: inner
    namespace: inner-ns
  items:
  - apiVersion: v1
    kind: ConfigMap
    metadata:
      name: nested
    data: {}
"""
    paths = write_manifests(content, tmp_path, "default")

    assert paths == {tmp_path / "inner-ns" / "configmap-nested.yaml"}


def test_write_manifests_skips_hooks_inside_lists(tmp_path: Path) -> None:
    content = """\
apiVersion: v1
kind: List
metadata:
  name: mixed
  namespace: hooks-ns
items:
- apiVersion: batch/v1
  kind: Job
  metadata:
    name: my-hook
    annotations:
      helm.sh/hook: post-install
  spec: {}
- apiVersion: v1
  kind: ConfigMap
  metadata:
    name: my-config
  data: {}
"""
    paths = write_manifests(content, tmp_path, "default")

    assert paths == {tmp_path / "hooks-ns" / "configmap-my-config.yaml"}


def test_write_manifests_raises_on_non_list_items(tmp_path: Path) -> None:
    content = """\
apiVersion: v1
kind: List
metadata:
  name: broken
items: "not-a-list"
"""
    with pytest.raises(TypeError, match=r"items of List broken is not a list"):
        write_manifests(content, tmp_path, "default")


def test_write_manifests_raises_on_non_dict_annotations(tmp_path: Path) -> None:
    """A YAML document with non-dict annotations should raise a descriptive error."""
    yaml_bad_annotations = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-config
  annotations: "not-a-dict"
data: {}
"""
    with pytest.raises(
        TypeError,
        match=(
            r"failed to read annotations on object ConfigMap from mychart, "
            r"item annotations is not a dict"
        ),
    ):
        write_manifests(yaml_bad_annotations, tmp_path, "default", app_name="mychart")


def test_write_manifests_returns_paths_for_stale_file_removal(tmp_path: Path) -> None:
    stale = tmp_path / "default" / "configmap-old.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale content\n")

    paths = write_manifests(NAMESPACED_YAML, tmp_path, "default")

    # stale file is NOT in the returned set
    assert stale not in paths
    # new file IS in the returned set
    assert any(p.name == "deployment-myapp.yaml" for p in paths)


def test_make_k8s_name_valid_domain() -> None:
    """Valid domain names should be converted correctly."""
    assert make_k8s_name("example.com") == "example-com"
    assert make_k8s_name("my.example.com") == "my-example-com"
    assert make_k8s_name("zq.lu") == "zq-lu"


def test_make_k8s_name_valid_alphanumeric() -> None:
    """Valid alphanumeric names with dashes should be preserved."""
    assert make_k8s_name("my-app") == "my-app"
    assert make_k8s_name("app123") == "app123"
    assert make_k8s_name("A-B-C") == "a-b-c"


def test_make_k8s_name_starts_with_dash() -> None:
    """Names starting with a period (resulting in dash) should raise ValueError."""
    with pytest.raises(ValueError, match="must start with an alphanumeric character"):
        make_k8s_name(".example.com")


def test_make_k8s_name_ends_with_dash() -> None:
    """Names ending with a period (resulting in dash) should raise ValueError."""
    with pytest.raises(ValueError, match="must end with an alphanumeric character"):
        make_k8s_name("example.com.")


def test_make_k8s_name_exceeds_63_characters() -> None:
    """Names exceeding 63 characters should raise ValueError."""
    long_name = "a" * 50 + "." + "b" * 20  # Will exceed 63 after replacement
    with pytest.raises(ValueError, match="exceeds 63 character limit"):
        make_k8s_name(long_name)


def test_make_k8s_name_only_periods() -> None:
    """Names consisting only of periods should fail validation."""
    # A single period becomes a single dash, which fails the alphanumeric start/end check
    with pytest.raises(ValueError, match="must start with an alphanumeric character"):
        make_k8s_name(".")


def test_make_k8s_name_invalid_characters() -> None:
    """Names with invalid characters (after conversion) should raise ValueError."""
    with pytest.raises(ValueError, match="contains invalid characters"):
        make_k8s_name("my_app")  # underscore is invalid


# ---------------------------------------------------------------------------
# _ensure_namespaces
# ---------------------------------------------------------------------------


def test_ensure_namespaces_creates_namespace_for_each_directory(
    tmp_path: Path,
) -> None:
    """A Namespace manifest is created for each namespace directory."""
    ns_dir = tmp_path / "my-app"
    ns_dir.mkdir()
    (ns_dir / "deployment-my-app.yaml").write_text("placeholder")

    written: dict[Path, str] = {ns_dir / "deployment-my-app.yaml": "my-app"}
    new = _ensure_namespaces(tmp_path, written)

    ns_file = ns_dir / "namespace-my-app.yaml"
    assert ns_file in new
    doc = yaml.safe_load(ns_file.read_text())
    assert doc["kind"] == "Namespace"
    assert doc["metadata"]["name"] == "my-app"
    # PLAT-739: generated namespaces prune last so Crossplane can finalize the
    # in-namespace resources before the namespace itself is removed.
    assert (
        doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        == "PruneLast=true"
    )


def test_ensure_namespaces_skips_cluster_directory(tmp_path: Path) -> None:
    """The cluster/ directory is not treated as a namespace."""
    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()
    (cluster_dir / "clusterrole-foo.yaml").write_text("placeholder")

    written: dict[Path, str] = {cluster_dir / "clusterrole-foo.yaml": "foo"}
    new = _ensure_namespaces(tmp_path, written)

    assert new == {}


def test_ensure_namespaces_skips_owners_directory(tmp_path: Path) -> None:
    """The owners/ directory contains ownership TOML, not Kubernetes resources."""
    owners_dir = tmp_path / "owners"
    owners_dir.mkdir()
    (owners_dir / "team-a.toml").write_text('owned = "team-a"\n')

    new = _ensure_namespaces(tmp_path, {})

    assert new == {}
    assert not (owners_dir / "namespace-owners.yaml").exists()


def test_ensure_namespaces_skips_when_namespace_already_written(
    tmp_path: Path,
) -> None:
    """No Namespace is created if one was already written by a generator."""
    ns_dir = tmp_path / "my-app"
    ns_dir.mkdir()
    ns_file = ns_dir / "namespace-my-app.yaml"
    ns_file.write_text("existing")

    written: dict[Path, str] = {ns_file: "my-app"}
    new = _ensure_namespaces(tmp_path, written)

    assert new == {}


def test_ensure_namespaces_skips_when_namespace_in_cluster_dir(
    tmp_path: Path,
) -> None:
    """No Namespace is created if one was written to cluster/ by a generator."""
    ns_dir = tmp_path / "my-app"
    ns_dir.mkdir()
    cluster_ns = tmp_path / "cluster" / "namespace-my-app.yaml"
    cluster_ns.parent.mkdir()
    cluster_ns.write_text("existing")

    written: dict[Path, str] = {cluster_ns: "my-app"}
    new = _ensure_namespaces(tmp_path, written)

    assert new == {}


def test_ensure_namespaces_multiple_namespaces(tmp_path: Path) -> None:
    """Each namespace directory gets its own Namespace manifest."""
    written: dict[Path, str] = {}
    for ns in ("ns-a", "ns-b"):
        ns_dir = tmp_path / ns
        ns_dir.mkdir()
        manifest = ns_dir / f"deployment-{ns}.yaml"
        manifest.write_text("placeholder")
        written[manifest] = ns

    new = _ensure_namespaces(tmp_path, written)

    assert tmp_path / "ns-a" / "namespace-ns-a.yaml" in new
    assert tmp_path / "ns-b" / "namespace-ns-b.yaml" in new


def test_ensure_namespaces_nonexistent_output_dir(tmp_path: Path) -> None:
    """Returns empty dict when the output directory does not exist yet."""
    new = _ensure_namespaces(tmp_path / "nonexistent", {})
    assert new == {}
