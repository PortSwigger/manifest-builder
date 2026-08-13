# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for copy manifest generation."""

from pathlib import Path

import pystache
import pystache.context
import pytest
import yaml

from manifest_builder.blocks.copy import CopyConfig, generate_copy


def _make_config(
    tmp_path: Path,
    source: Path,
    config: dict[str, Path] | None = None,
    name: str = "acme-dns",
    namespace: str = "acme-dns",
) -> CopyConfig:
    return CopyConfig(
        name=name,
        namespace=namespace,
        source=source,
        config=config,
    )


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# Manifest copying
# ---------------------------------------------------------------------------


def test_generate_copy_copies_manifests(tmp_path: Path) -> None:
    """YAML files in the source directory are written to output."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-dns
  namespace: acme-dns
spec:
  replicas: 1
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir)
    paths = generate_copy(config, output_dir)

    assert len(paths) == 1
    out = _read_yaml(next(iter(paths)))
    assert out["kind"] == "Deployment"
    assert out["metadata"]["name"] == "acme-dns"


def test_generate_copy_injects_namespace_when_missing(tmp_path: Path) -> None:
    """Namespaced resources without a namespace get the configured namespace."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "service.yaml").write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: acme-dns
spec:
  ports:
    - port: 53
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir)
    generate_copy(config, output_dir)

    out_file = output_dir / "acme-dns" / "service-acme-dns.yaml"
    assert out_file.exists()
    out = _read_yaml(out_file)
    assert out["metadata"]["namespace"] == "acme-dns"


def test_generate_copy_preserves_existing_namespace(tmp_path: Path) -> None:
    """Resources that already have a namespace are not modified."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "service.yaml").write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: my-svc
  namespace: other-ns
spec:
  ports:
    - port: 80
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir)
    generate_copy(config, output_dir)

    out_file = output_dir / "other-ns" / "service-my-svc.yaml"
    assert out_file.exists()
    out = _read_yaml(out_file)
    assert out["metadata"]["namespace"] == "other-ns"


def test_generate_copy_cluster_scoped_only_allows_no_namespace(tmp_path: Path) -> None:
    """When all source resources are cluster-scoped, namespace can be omitted."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "clusterrole.yaml").write_text(
        """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: acme-dns-role
rules: []
"""
    )

    output_dir = tmp_path / "output"
    config = CopyConfig(name="cluster-stuff", namespace=None, source=manifests_dir)
    paths = generate_copy(config, output_dir)

    out_file = output_dir / "cluster" / "clusterrole-acme-dns-role.yaml"
    assert out_file.exists()
    assert paths == {out_file}


def test_generate_copy_reads_scope_from_bundled_crd(tmp_path: Path) -> None:
    """A CRD in the source directory settles the scope of the kinds it defines."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "crd.yaml").write_text(
        """\
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
---
apiVersion: elbv2.k8s.aws/v1beta1
kind: IngressClassParams
metadata:
  name: alb
"""
    )

    output_dir = tmp_path / "output"
    config = CopyConfig(name="alb", namespace=None, source=manifests_dir)
    paths = generate_copy(config, output_dir)

    assert paths == {
        output_dir / "cluster" / "customresourcedefinition-ingressclassparams."
        "elbv2.k8s.aws.yaml",
        output_dir / "cluster" / "ingressclassparams-alb.yaml",
    }


def test_generate_copy_requires_namespace_for_namespaced_resource(
    tmp_path: Path,
) -> None:
    """Namespaced resources without an explicit namespace require config.namespace."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "service.yaml").write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: acme-dns
spec:
  ports:
    - port: 53
"""
    )

    output_dir = tmp_path / "output"
    config = CopyConfig(name="acme-dns", namespace=None, source=manifests_dir)
    with pytest.raises(ValueError, match="namespace.*is required"):
        generate_copy(config, output_dir)


def test_generate_copy_requires_namespace_when_config_present(tmp_path: Path) -> None:
    """A config table (creates ConfigMap) requires config.namespace."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "clusterrole.yaml").write_text(
        """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: acme-dns-role
rules: []
"""
    )
    cfg_file = tmp_path / "app.cfg"
    cfg_file.write_text("setting=true\n")

    output_dir = tmp_path / "output"
    config = CopyConfig(
        name="acme-dns",
        namespace=None,
        source=manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    with pytest.raises(ValueError, match="namespace.*is required"):
        generate_copy(config, output_dir)


def test_generate_copy_namespaced_resource_with_metadata_ns_no_config_namespace(
    tmp_path: Path,
) -> None:
    """Namespaced resources that have metadata.namespace don't require config.namespace."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "service.yaml").write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: my-svc
  namespace: other-ns
spec:
  ports:
    - port: 80
"""
    )

    output_dir = tmp_path / "output"
    config = CopyConfig(name="cluster-stuff", namespace=None, source=manifests_dir)
    generate_copy(config, output_dir)

    out_file = output_dir / "other-ns" / "service-my-svc.yaml"
    assert out_file.exists()


def test_generate_copy_cluster_scoped_resources_no_namespace(tmp_path: Path) -> None:
    """Cluster-scoped resources (e.g. ClusterRole) do not get a namespace."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "clusterrole.yaml").write_text(
        """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: acme-dns-role
rules: []
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir)
    generate_copy(config, output_dir)

    out_file = output_dir / "cluster" / "clusterrole-acme-dns-role.yaml"
    assert out_file.exists()
    out = _read_yaml(out_file)
    assert "namespace" not in out.get("metadata", {})


# ---------------------------------------------------------------------------
# ConfigMap generation
# ---------------------------------------------------------------------------


def test_generate_copy_creates_configmap_from_config(tmp_path: Path) -> None:
    """A ConfigMap is created from the files listed in the config table."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    cfg_file = tmp_path / "app.cfg"
    cfg_file.write_text("[dns]\nport = 53\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    cm_file = output_dir / "acme-dns" / "configmap-acme-dns-config.yaml"
    assert cm_file.exists()
    cm = _read_yaml(cm_file)
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == "acme-dns-config"
    assert cm["metadata"]["namespace"] == "acme-dns"
    assert "app.cfg" in cm["data"]
    assert cm["data"]["app.cfg"] == "[dns]\nport = 53\n"


def test_generate_copy_configmap_preserves_unicode_as_block_scalar(
    tmp_path: Path,
) -> None:
    """Config content with non-ASCII characters renders as a literal block.

    Without allow_unicode, PyYAML cannot emit a block scalar for strings
    containing non-ASCII characters (such as an em-dash) and falls back to a
    double-quoted single line with escaped newlines.
    """
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    cfg_file = tmp_path / "app.cfg"
    content = "[dns]\n# Primary resolver — do not change\nport = 53\n"
    cfg_file.write_text(content)

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    cm_file = output_dir / "acme-dns" / "configmap-acme-dns-config.yaml"
    raw = cm_file.read_text()
    # Em-dash is emitted literally, not escaped.
    assert "—" in raw
    assert "\\u2014" not in raw
    # A literal block scalar is used (no escaped newlines on a single line).
    assert "app.cfg: |" in raw
    assert "\\n" not in raw
    # And the round-tripped content matches exactly.
    cm = _read_yaml(cm_file)
    assert cm["data"]["app.cfg"] == content


def test_generate_copy_configmap_key_is_filename(tmp_path: Path) -> None:
    """The ConfigMap key is the last component of the container path."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    cfg_file = tmp_path / "config.cfg"
    cfg_file.write_text("setting=true\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/config.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    cm_file = output_dir / "acme-dns" / "configmap-acme-dns-config.yaml"
    cm = _read_yaml(cm_file)
    assert "config.cfg" in cm["data"]


def test_generate_copy_injects_config_volume_and_mount(tmp_path: Path) -> None:
    """Deployment containers should mount generated ConfigMaps."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-dns
spec:
  template:
    spec:
      containers:
      - name: acme-dns
        image: example.com/acme-dns:1.0
"""
    )
    cfg_file = tmp_path / "app.cfg"
    cfg_file.write_text("[dns]\nport = 53\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    deployment = _read_yaml(output_dir / "acme-dns" / "deployment-acme-dns.yaml")
    pod_spec = deployment["spec"]["template"]["spec"]

    assert any(
        volume["name"] == "acme-dns-config"
        and volume["configMap"]["name"] == "acme-dns-config"
        for volume in pod_spec["volumes"]
    )
    assert any(
        mount["name"] == "acme-dns-config" and mount["mountPath"] == "/config"
        for mount in pod_spec["containers"][0]["volumeMounts"]
    )


def test_generate_copy_mounts_nested_config_at_parent_directory(
    tmp_path: Path,
) -> None:
    """Nested config paths should mount at their full parent directory."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-dns
spec:
  template:
    spec:
      containers:
      - name: acme-dns
        image: example.com/acme-dns:1.0
"""
    )
    cfg_file = tmp_path / "config.cfg"
    cfg_file.write_text("setting=true\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/etc/acme-dns/config.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    deployment = _read_yaml(output_dir / "acme-dns" / "deployment-acme-dns.yaml")
    pod_spec = deployment["spec"]["template"]["spec"]
    configmap = _read_yaml(
        output_dir / "acme-dns" / "configmap-acme-dns-etc-acme-dns.yaml"
    )

    assert configmap["metadata"]["name"] == "acme-dns-etc-acme-dns"
    assert configmap["data"]["config.cfg"] == "setting=true\n"
    assert any(
        volume["name"] == "acme-dns-etc-acme-dns"
        and volume["configMap"]["name"] == "acme-dns-etc-acme-dns"
        for volume in pod_spec["volumes"]
    )
    assert any(
        mount["name"] == "acme-dns-etc-acme-dns"
        and mount["mountPath"] == "/etc/acme-dns"
        for mount in pod_spec["containers"][0]["volumeMounts"]
    )


def test_generate_copy_adds_config_checksum_annotation(tmp_path: Path) -> None:
    """Deployment pod template should include a config checksum annotation."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-dns
spec:
  template:
    metadata:
      labels:
        app: acme-dns
    spec:
      containers:
      - name: acme-dns
        image: example.com/acme-dns:1.0
"""
    )
    cfg_file = tmp_path / "app.cfg"
    cfg_file.write_text("[dns]\nport = 53\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    generate_copy(config, output_dir)

    deployment = _read_yaml(output_dir / "acme-dns" / "deployment-acme-dns.yaml")
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" in annotations
    assert isinstance(annotations["checksum/config"], str)
    assert len(annotations["checksum/config"]) == 64


def test_generate_copy_config_checksum_changes_with_content(tmp_path: Path) -> None:
    """Changing config content should produce a different checksum annotation."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-dns
spec:
  template:
    spec:
      containers:
      - name: acme-dns
        image: example.com/acme-dns:1.0
"""
    )
    cfg_file = tmp_path / "app.cfg"
    cfg_file.write_text("[dns]\nport = 53\n")

    output_dir = tmp_path / "output"
    config = _make_config(
        tmp_path,
        manifests_dir,
        config={"/config/app.cfg": cfg_file},
    )
    generate_copy(config, output_dir)
    first = _read_yaml(output_dir / "acme-dns" / "deployment-acme-dns.yaml")
    first_checksum = first["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]

    cfg_file.write_text("[dns]\nport = 54\n")
    generate_copy(config, output_dir)
    second = _read_yaml(output_dir / "acme-dns" / "deployment-acme-dns.yaml")
    second_checksum = second["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]

    assert first_checksum != second_checksum


def test_generate_copy_no_config_no_configmap(tmp_path: Path) -> None:
    """When no config is specified, no ConfigMap is created."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "service.yaml").write_text(
        """\
apiVersion: v1
kind: Service
metadata:
  name: acme-dns
  namespace: acme-dns
spec:
  ports:
    - port: 53
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir)
    paths = generate_copy(config, output_dir)

    configmaps = [p for p in paths if "configmap" in p.name]
    assert configmaps == []


# ---------------------------------------------------------------------------
# Image templating
# ---------------------------------------------------------------------------


def test_generate_copy_with_images_renders_template(tmp_path: Path) -> None:
    """Manifests are rendered as Mustache templates when images are provided."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  template:
    spec:
      containers:
      - name: main
        image: "{{my_app_image}}"
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir, name="my-app", namespace="default")
    images = {"my_app_image": "example.com/my-app:1.2.3"}
    paths = generate_copy(config, output_dir, images=images)

    assert len(paths) == 1
    out = _read_yaml(next(iter(paths)))
    container = out["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example.com/my-app:1.2.3"


def test_generate_copy_renders_template_variables(tmp_path: Path) -> None:
    """Template variables (e.g. from --vars-from) are available to copied manifests."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "configmap.yaml").write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  domain: "{{domain}}"
"""
    )

    output_dir = tmp_path / "output"
    config = CopyConfig(
        name="my-app",
        namespace="default",
        source=manifests_dir,
        variables={"domain": "example.com"},
    )
    paths = generate_copy(config, output_dir)

    assert len(paths) == 1
    out = _read_yaml(next(iter(paths)))
    assert out["data"]["domain"] == "example.com"


def test_generate_copy_raises_on_missing_image(tmp_path: Path) -> None:
    """Manifest generation fails if a template variable is missing from images."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "deployment.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  template:
    spec:
      containers:
      - name: main
        image: "{{my_app_image}}"
"""
    )

    output_dir = tmp_path / "output"
    config = _make_config(tmp_path, manifests_dir, name="my-app", namespace="default")

    with pytest.raises(pystache.context.KeyNotFoundError):
        generate_copy(config, output_dir, images=None)
