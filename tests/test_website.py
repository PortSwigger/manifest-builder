# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for website manifest generation."""

from pathlib import Path

import pytest
import yaml

from manifest_builder.config import WebsiteConfig
from manifest_builder.generator import ManifestError, generate_manifests
from manifest_builder.website import WebsiteConfigHandler, generate_website

SIMPLE_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-website
  namespace: staging
spec: {}
"""


def _make_website_config(tmp_path: Path) -> tuple[WebsiteConfig, Path]:
    """Create a WebsiteConfig for testing, with a temporary templates directory.

    Returns a tuple of (config, templates_dir).
    """
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(SIMPLE_DEPLOYMENT)
    return (
        WebsiteConfig(
            name="my-website",
            namespace="staging",
        ),
        templates_dir,
    )


def test_generate_website_writes_yaml_files(tmp_path: Path) -> None:
    config, templates_dir = _make_website_config(tmp_path)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    assert len(paths) == 1
    (path,) = paths
    assert path == tmp_path / "output" / "staging" / "deployment-my-website.yaml"
    assert path.exists()


def test_generate_website_adds_source_comment(tmp_path: Path) -> None:
    config, templates_dir = _make_website_config(tmp_path)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    content = path.read_text()
    assert content.startswith("# Source: my-website\n")


def test_generate_website_multiple_yaml_files(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deploy.yaml").write_text(SIMPLE_DEPLOYMENT)
    (templates_dir / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: simple-svc\n  namespace: staging\n"
    )
    config = WebsiteConfig(name="my-website", namespace="staging")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    names = {p.name for p in paths}
    assert names == {"deployment-my-website.yaml", "service-simple-svc.yaml"}


def test_generate_website_multi_document_template_file(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "resources.yaml").write_text(
        SIMPLE_DEPLOYMENT
        + "---\n"
        + "apiVersion: v1\nkind: Service\nmetadata:\n  name: simple-svc\n  namespace: staging\n"
    )
    config = WebsiteConfig(name="my-website", namespace="staging")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    names = {p.name for p in paths}
    assert names == {"deployment-my-website.yaml", "service-simple-svc.yaml"}


def test_generate_website_empty_templates_dir(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    config = WebsiteConfig(name="my-website", namespace="staging")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    assert paths == set()


def test_generate_website_non_yaml_files_ignored(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(SIMPLE_DEPLOYMENT)
    (templates_dir / "notes.txt").write_text("this should be ignored\n")
    (templates_dir / "script.sh").write_text("#!/bin/sh\n")
    config = WebsiteConfig(name="my-website", namespace="staging")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    assert len(paths) == 1


def test_generate_website_cluster_scoped_resource(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "clusterrole.yaml").write_text(
        "apiVersion: rbac.authorization.k8s.io/v1\n"
        "kind: ClusterRole\n"
        "metadata:\n"
        "  name: my-role\n"
        "rules: []\n"
    )
    config = WebsiteConfig(name="my-website", namespace="staging")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    (path,) = paths
    assert path == tmp_path / "output" / "cluster" / "clusterrole-my-role.yaml"


def test_generate_website_namespace_fallback(tmp_path: Path) -> None:
    """Resources without a namespace in metadata use the config namespace."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "configmap.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: my-config\ndata: {}\n"
    )
    config = WebsiteConfig(name="my-website", namespace="fallback-ns")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )
    (path,) = paths
    assert path.parent.name == "fallback-ns"


def test_generate_manifests_with_website_config(tmp_path: Path) -> None:
    """generate_manifests dispatches to website app generation without needing helm."""
    config = WebsiteConfig(name="zq.lu", namespace="web")
    output_dir = tmp_path / "output"

    generate_manifests(
        [WebsiteConfigHandler([config])],
        output_dir,
        repo_root=tmp_path,
    )

    # Check that files were generated from the bundled web templates
    assert output_dir.exists()
    generated_files = list(output_dir.rglob("*.yaml"))
    assert len(generated_files) > 0
    # The k8s_name template variable converts "zq.lu" to "zq-lu"
    assert any("zq-lu" in str(f) for f in generated_files)


def test_generate_website_creates_client_traffic_policy_for_gateway(
    tmp_path: Path,
) -> None:
    """Each website Gateway should have a same-named modern TLS policy."""
    config = WebsiteConfig(name="zq.lu", namespace="web")

    paths = generate_website(config, tmp_path / "output")

    policy_path = next(
        path for path in paths if path.name == "clienttrafficpolicy-zq-lu.yaml"
    )
    policy = yaml.safe_load(policy_path.read_text())
    assert policy == {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "ClientTrafficPolicy",
        "metadata": {"name": "zq-lu", "namespace": "web"},
        "spec": {
            "http3": {},
            "targetRefs": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "Gateway",
                    "name": "zq-lu",
                }
            ],
            "tls": {
                "ecdhCurves": ["X25519MLKEM768", "X25519", "P-256"],
                "minVersion": "1.3",
            },
        },
    }


def test_generate_manifests_removes_stale_website_files(tmp_path: Path) -> None:
    """Stale files from previous runs are removed after generating website manifests."""
    output_dir = tmp_path / "output"
    stale = output_dir / "web" / "configmap-old.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale: true\n")

    config = WebsiteConfig(name="zq.lu", namespace="web")
    generate_manifests(
        [WebsiteConfigHandler([config])],
        output_dir,
        repo_root=tmp_path,
    )

    assert not stale.exists()
    # Check that files were generated
    assert any(output_dir.rglob("*.yaml"))


def test_generate_website_file_content_is_valid_yaml(tmp_path: Path) -> None:
    """Each output file must be parseable YAML with the correct structure."""
    config, templates_dir = _make_website_config(tmp_path)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    # skip the leading comment line before parsing
    text = path.read_text()
    doc = yaml.safe_load(text)
    assert doc["kind"] == "Deployment"
    assert doc["metadata"]["name"] == "my-website"


def test_generate_manifests_detects_output_file_conflicts(tmp_path: Path) -> None:
    """generate_manifests should error when different configs generate the same files."""
    templates_dir1 = tmp_path / "templates1"
    templates_dir1.mkdir()
    (templates_dir1 / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec: {}\n"
    )

    templates_dir2 = tmp_path / "templates2"
    templates_dir2.mkdir()
    (templates_dir2 / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec: {}\n"
    )

    config1 = WebsiteConfig(name="app1", namespace="ns1")
    config2 = WebsiteConfig(name="app2", namespace="ns1")

    output_dir = tmp_path / "output"

    # Mock the templates for both configs to generate the same output files
    import unittest.mock as mock

    with mock.patch(
        "manifest_builder.website.generate_website",
        side_effect=[
            {
                tmp_path / "output" / "ns1" / "deployment-app.yaml"
            },  # config1 generates this
            {
                tmp_path / "output" / "ns1" / "deployment-app.yaml"
            },  # config2 also generates this
        ],
    ):
        with pytest.raises(ManifestError, match="Configuration conflict") as exc_info:
            generate_manifests(
                [WebsiteConfigHandler([config1, config2])],
                output_dir,
                repo_root=tmp_path,
            )

    assert "ns1/deployment-app.yaml (generated by app1)" in str(exc_info.value)
    assert str(output_dir) not in str(exc_info.value)


def test_generate_website_provides_k8s_name_variable(tmp_path: Path) -> None:
    """Website templates should have k8s_name available (name with periods replaced by dashes)."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a template that uses the {{k8s_name}} variable
    (templates_dir / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: {{k8s_name}}\nspec: {}\n"
    )

    config = WebsiteConfig(name="my.example.com", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    # The {{k8s_name}} should have been rendered with periods replaced by dashes
    assert doc["metadata"]["name"] == "my-example-com"


def test_generate_website_renders_mustache_templates(tmp_path: Path) -> None:
    """Website templates should be rendered as Mustache with the website name available."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a template that uses the {{name}} variable
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{name}}\nspec: {}\n"
    )

    config = WebsiteConfig(name="my-app", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    # The {{name}} should have been rendered to "my-app"
    assert doc["metadata"]["name"] == "my-app"


def test_generate_website_adds_namespace_to_namespaced_resources(
    tmp_path: Path,
) -> None:
    """Namespaced resources should get the namespace from the config."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a Deployment without a namespace
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: test-app\nspec: {}\n"
    )
    # Create a ClusterRole (should NOT get namespace)
    (templates_dir / "clusterrole.yaml").write_text(
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: test-role\nrules: []\n"
    )

    config = WebsiteConfig(name="test-app", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Check the Deployment got the namespace
    deployment_files = [p for p in paths if "deployment" in p.name]
    assert len(deployment_files) == 1
    deployment_doc = yaml.safe_load(deployment_files[0].read_text())
    assert deployment_doc["metadata"]["namespace"] == "production"

    # Check the ClusterRole did NOT get the namespace
    clusterrole_files = [p for p in paths if "clusterrole" in p.name]
    assert len(clusterrole_files) == 1
    clusterrole_doc = yaml.safe_load(clusterrole_files[0].read_text())
    assert "namespace" not in clusterrole_doc["metadata"]


def test_generate_website_applies_hugo_repo_annotation(tmp_path: Path) -> None:
    """Website with hugo_repo should add hugo annotation to Deployment objects."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec: {}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        hugo_repo="https://github.com/user/repo",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["metadata"]["annotations"]["hugo"] == "https://github.com/user/repo"


def test_generate_website_no_hugo_repo_annotation(tmp_path: Path) -> None:
    """Website without hugo_repo should not add hugo annotation."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec: {}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert "annotations" not in doc["metadata"] or "hugo" not in doc["metadata"].get(
        "annotations", {}
    )


def test_generate_website_image_parameter_available_in_template(tmp_path: Path) -> None:
    """Website with image parameter should be available in templates as {{image}}."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      containers:\n      - image: {{image}}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        image="nginx:1.20",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["template"]["spec"]["containers"][0]["image"] == "nginx:1.20"


def test_generate_website_args_string_parameter_available_in_template(
    tmp_path: Path,
) -> None:
    """Website with args string parameter should be available in templates as {{args}}."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      containers:\n      - name: app\n        args:\n        - {{args}}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        args="--debug --log-level=info",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert (
        doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
        == "--debug --log-level=info"
    )


def test_generate_website_args_list_parameter_available_in_template(
    tmp_path: Path,
) -> None:
    """Website with args list parameter should be available in templates as YAML list."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a template that expects args to be a list that renders into YAML
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      containers:\n      - name: app\n        args:\n{{#args}}\n        - {{.}}\n{{/args}}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        args=["--debug", "--log-level=info"],
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "--debug",
        "--log-level=info",
    ]


def test_generate_website_ignores_underscore_prefixed_templates(tmp_path: Path) -> None:
    """Fragment templates starting with underscore should not generate output files."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a normal deployment template
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec: {}\n"
    )
    # Create a fragment template that would normally generate output
    (templates_dir / "_sidecar.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sidecar-config\ndata: {}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Only the deployment should be generated, not the fragment
    assert len(paths) == 1
    (path,) = paths
    assert "deployment" in path.name
    assert "sidecar" not in path.name


def test_generate_website_fragment_with_template_variables(tmp_path: Path) -> None:
    """Fragment templates should be rendered with the same context as regular templates."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a fragment that uses template variables
    (templates_dir / "_fragment.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{k8s_name}}-config\ndata: {}\n"
    )

    config = WebsiteConfig(name="my.app", namespace="production")
    # Call generate_website which will load the fragment (though it won't output it)
    generate_website(config, tmp_path / "output", _templates_override=templates_dir)

    # This test verifies that no errors occur during rendering with template variables
    # (errors would indicate the fragment isn't being rendered properly)
    # We can't directly verify the fragment was loaded, but if rendering fails, test fails


def test_generate_website_multiple_fragments_not_in_output(tmp_path: Path) -> None:
    """Multiple fragment templates should all be ignored in output."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create one normal template
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec: {}\n"
    )
    # Create multiple fragments
    (templates_dir / "_init_container.yaml").write_text("name: init\nimage: busybox\n")
    (templates_dir / "_sidecar.yaml").write_text("name: sidecar\nimage: nginx\n")

    config = WebsiteConfig(name="my-website", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Only the deployment should be in output, both fragments ignored
    assert len(paths) == 1
    filenames = {p.name for p in paths}
    assert filenames == {"deployment-web.yaml"}
    assert not any("fragment" in p.name for p in paths)


def test_generate_website_git_repo_parameter_from_hugo_repo(tmp_path: Path) -> None:
    """git_repo parameter should be populated from hugo_repo in template context."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a fragment that uses the git_repo parameter
    (templates_dir / "_git_init.yaml").write_text(
        "name: git\nimage: alpine/git\ncommand: [git, clone, {{git_repo}}]\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        hugo_repo="https://github.com/user/my-website",
    )
    # Call generate_website to render the fragment
    generate_website(config, tmp_path / "output", _templates_override=templates_dir)

    # If rendering succeeds without errors, the git_repo parameter was available
    # (errors would indicate the parameter wasn't in the context)


def test_generate_website_injects_hugo_fragments(tmp_path: Path) -> None:
    """Hugo fragments should be injected into Deployment when hugo_repo is set."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    # Create the deployment template
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    # Create Hugo fragment files
    (templates_dir / "_hugo_container.yaml").write_text(
        "name: web\nimage: static-web-server:2.36.1\nvolumeMounts:\n  - mountPath: /public\n    name: public\n"
    )

    (templates_dir / "_hugo_initcontainers.yaml").write_text(
        "- name: git\n  image: alpine/git:2.47.2\n  command:\n  - /bin/sh\n  - -c\n  - >\n    git clone {{git_repo}} --recurse-submodules --depth=1 /src\n"
    )

    (templates_dir / "_hugo_volumes.yaml").write_text(
        "- name: public\n  emptyDir: {}\n"
    )

    config = WebsiteConfig(
        name="my-website",
        namespace="production",
        image="nginx:latest",
        hugo_repo="https://github.com/user/my-website",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())

    # Verify Hugo container replaced the original
    containers = doc["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1  # replaced, not appended
    assert containers[0]["name"] == "web"
    assert "static-web-server" in containers[0]["image"]

    # Verify init containers were injected
    init_containers = doc["spec"]["template"]["spec"]["initContainers"]
    assert len(init_containers) == 1
    assert init_containers[0]["name"] == "git"
    # The command contains the git clone with the expanded git_repo
    command_script = init_containers[0]["command"][2]
    assert "git clone https://github.com/user/my-website" in command_script

    # Verify volumes were injected
    volumes = doc["spec"]["template"]["spec"]["volumes"]
    assert any(v["name"] == "public" for v in volumes)

    # Verify hugo annotation was added
    assert (
        doc["metadata"]["annotations"]["hugo"] == "https://github.com/user/my-website"
    )


def test_generate_website_emits_configmap(tmp_path: Path) -> None:
    """ConfigMap should be generated from config."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    # Create a simple deployment template
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    # Create a config file
    config_file = tmp_path / "app.toml"
    config_file.write_text("[app]\ndebug = true\n")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={"/config/app.toml": config_file},
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Should have 2 files: deployment + configmap
    assert len(paths) == 2
    filenames = {p.name for p in paths}
    assert "deployment-my-app.yaml" in filenames
    assert "configmap-my-app-config.yaml" in filenames

    # Check ConfigMap content
    configmap_path = next(p for p in paths if "configmap" in p.name)
    configmap_doc = yaml.safe_load(configmap_path.read_text())
    assert configmap_doc["kind"] == "ConfigMap"
    assert configmap_doc["metadata"]["name"] == "my-app-config"
    assert configmap_doc["metadata"]["namespace"] == "production"
    assert configmap_doc["data"]["app.toml"] == "[app]\ndebug = true\n"


def test_generate_website_configmap_groups_by_top_level_dir(tmp_path: Path) -> None:
    """Multiple config files in different directories should create separate ConfigMaps."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file1 = tmp_path / "app.toml"
    config_file1.write_text("app config")
    config_file2 = tmp_path / "db.conf"
    config_file2.write_text("db config")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={
            "/config/app.toml": config_file1,
            "/etc/db.conf": config_file2,
        },
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Should have 3 files: deployment + 2 configmaps
    assert len(paths) == 3
    configmap_names = {p.stem for p in paths if "configmap" in p.name}
    assert "configmap-my-app-config" in configmap_names
    assert "configmap-my-app-etc" in configmap_names


def test_generate_website_configmap_same_dir_merged(tmp_path: Path) -> None:
    """Multiple config files in the same directory should merge into one ConfigMap."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file1 = tmp_path / "app.toml"
    config_file1.write_text("app config")
    config_file2 = tmp_path / "other.toml"
    config_file2.write_text("other config")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={
            "/config/app.toml": config_file1,
            "/config/other.toml": config_file2,
        },
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Should have 2 files: deployment + 1 configmap
    assert len(paths) == 2
    configmap_path = next(p for p in paths if "configmap" in p.name)
    configmap_doc = yaml.safe_load(configmap_path.read_text())
    assert len(configmap_doc["data"]) == 2
    assert "app.toml" in configmap_doc["data"]
    assert "other.toml" in configmap_doc["data"]


def test_generate_website_injects_volume_and_mount(tmp_path: Path) -> None:
    """ConfigMap volumes and mounts should be injected into Deployment."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file = tmp_path / "app.toml"
    config_file.write_text("app config")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={"/config/app.toml": config_file},
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    # Check volume is present
    volumes = deployment_doc["spec"]["template"]["spec"]["volumes"]
    assert any(
        v["name"] == "my-app-config"
        and v.get("configMap", {}).get("name") == "my-app-config"
        for v in volumes
    )

    # Check volumeMount is present in container
    containers = deployment_doc["spec"]["template"]["spec"]["containers"]
    mounts = containers[0]["volumeMounts"]
    assert any(
        m["name"] == "my-app-config" and m["mountPath"] == "/config" for m in mounts
    )


def test_generate_website_mounts_nested_config_at_parent_directory(
    tmp_path: Path,
) -> None:
    """Nested config paths should mount at their full parent directory."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file = tmp_path / "config.cfg"
    config_file.write_text("setting=true\n")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={"/etc/acme-dns/config.cfg": config_file},
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())
    configmap_path = next(p for p in paths if "configmap" in p.name)
    configmap_doc = yaml.safe_load(configmap_path.read_text())

    assert configmap_doc["metadata"]["name"] == "my-app-etc-acme-dns"
    assert configmap_doc["data"]["config.cfg"] == "setting=true\n"

    volumes = deployment_doc["spec"]["template"]["spec"]["volumes"]
    assert any(
        v["name"] == "my-app-etc-acme-dns"
        and v.get("configMap", {}).get("name") == "my-app-etc-acme-dns"
        for v in volumes
    )

    containers = deployment_doc["spec"]["template"]["spec"]["containers"]
    mounts = containers[0]["volumeMounts"]
    assert any(
        m["name"] == "my-app-etc-acme-dns" and m["mountPath"] == "/etc/acme-dns"
        for m in mounts
    )


def test_generate_website_adds_config_checksum_annotation(tmp_path: Path) -> None:
    """Deployment pod template should include a config checksum annotation."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file = tmp_path / "app.toml"
    config_file.write_text("[app]\ndebug = true\n")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={"/config/app.toml": config_file},
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    annotations = deployment_doc["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" in annotations
    assert isinstance(annotations["checksum/config"], str)
    assert len(annotations["checksum/config"]) == 64


def test_generate_website_config_checksum_changes_with_content(tmp_path: Path) -> None:
    """Changing config content should produce a different checksum annotation."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config_file = tmp_path / "app.toml"
    config_file.write_text("[app]\ndebug = true\n")

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
        config={"/config/app.toml": config_file},
    )
    output_dir = tmp_path / "output"
    first_paths = generate_website(
        config, output_dir, _templates_override=templates_dir
    )
    first_deployment = yaml.safe_load(
        next(p for p in first_paths if "deployment" in p.name).read_text()
    )
    first_checksum = first_deployment["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]

    config_file.write_text("[app]\ndebug = false\n")
    second_paths = generate_website(
        config, output_dir, _templates_override=templates_dir
    )
    second_deployment = yaml.safe_load(
        next(p for p in second_paths if "deployment" in p.name).read_text()
    )
    second_checksum = second_deployment["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]

    assert first_checksum != second_checksum


def test_generate_website_no_config_no_configmap(tmp_path: Path) -> None:
    """Website without config should not generate ConfigMaps."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  template:\n    metadata:\n      labels:\n        app: {{k8s_name}}\n    spec:\n      containers:\n      - name: {{k8s_name}}\n        image: {{image}}\n"
    )

    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="myapp:1.0",
    )
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    # Should have only deployment, no configmap
    assert len(paths) == 1
    assert "deployment" in next(iter(paths)).name


def test_generate_website_extra_hostnames_emits_second_certificate(
    tmp_path: Path,
) -> None:
    """Second Certificate should be generated for extra_hostnames."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    # Use bundled templates (which now include extra certificate logic)
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        extra_hostnames=["www.example.com", "app.example.com"],
    )
    paths = generate_website(config, tmp_path / "output")

    # Should have certificates, gateway, httproute, service
    cert_paths = [p for p in paths if "certificate" in p.name]
    assert len(cert_paths) == 2

    # Check the extra certificate
    extra_cert_path = next(p for p in cert_paths if "extra" in p.name)
    cert_doc = yaml.safe_load(extra_cert_path.read_text())
    assert cert_doc["metadata"]["name"] == "my-app-extra"
    assert cert_doc["spec"]["secretName"] == "my-app-extra-tls"
    assert "www.example.com" in cert_doc["spec"]["dnsNames"]
    assert "app.example.com" in cert_doc["spec"]["dnsNames"]


def test_generate_website_extra_hostnames_gateway_has_extra_listeners(
    tmp_path: Path,
) -> None:
    """Gateway should have extra listeners for each extra hostname."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        extra_hostnames=["www.example.com", "app.example.com"],
    )
    paths = generate_website(config, tmp_path / "output")

    gateway_path = next(p for p in paths if "gateway" in p.name)
    gateway_doc = yaml.safe_load(gateway_path.read_text())

    listeners = gateway_doc["spec"]["listeners"]
    assert len(listeners) == 3  # 1 main + 2 extra

    # Check main listener
    assert listeners[0]["hostname"] == "my-app"
    assert listeners[0]["tls"]["certificateRefs"][0]["name"] == "my-app-tls"

    # Check extra listeners
    assert listeners[1]["hostname"] == "www.example.com"
    assert listeners[1]["tls"]["certificateRefs"][0]["name"] == "my-app-extra-tls"
    assert listeners[2]["hostname"] == "app.example.com"
    assert listeners[2]["tls"]["certificateRefs"][0]["name"] == "my-app-extra-tls"


def test_generate_website_extra_hostnames_in_httproute(tmp_path: Path) -> None:
    """HTTPRoute should include extra hostnames."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        extra_hostnames=["www.example.com", "app.example.com"],
    )
    paths = generate_website(config, tmp_path / "output")

    httproute_path = next(p for p in paths if "httproute" in p.name)
    httproute_doc = yaml.safe_load(httproute_path.read_text())

    hostnames = httproute_doc["spec"]["hostnames"]
    assert "my-app" in hostnames
    assert "www.example.com" in hostnames
    assert "app.example.com" in hostnames


def test_generate_website_extra_hostnames_string_normalized(tmp_path: Path) -> None:
    """Extra hostnames string should be normalized to list."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        extra_hostnames="www.example.com",  # string instead of list
    )
    paths = generate_website(config, tmp_path / "output")

    httproute_path = next(p for p in paths if "httproute" in p.name)
    httproute_doc = yaml.safe_load(httproute_path.read_text())

    hostnames = httproute_doc["spec"]["hostnames"]
    assert "www.example.com" in hostnames


def test_generate_website_bundled_template_args_string(tmp_path: Path) -> None:
    """Bundled template should correctly render string args in Deployment."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        args="--debug --log-level=info",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert "args" in container
    assert container["args"] == ["--debug --log-level=info"]


def test_generate_website_bundled_template_args_list(tmp_path: Path) -> None:
    """Bundled template should correctly render list args in Deployment."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        args=["--debug", "--log-level=info", "--port=8080"],
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert "args" in container
    assert container["args"] == ["--debug", "--log-level=info", "--port=8080"]


def test_generate_website_bundled_template_no_args(tmp_path: Path) -> None:
    """Bundled template should omit args field when not specified."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert "args" not in container


def test_generate_website_image_uses_healthz_liveness_probe(tmp_path: Path) -> None:
    """Image-backed websites should get an HTTP liveness probe on /healthz."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"] == {"httpGet": {"path": "/healthz", "port": 8080}}


def test_generate_website_hugo_uses_root_liveness_probe(tmp_path: Path) -> None:
    """Hugo-backed websites should get an HTTPS liveness probe on /."""
    config = WebsiteConfig(
        name="hugo.example.com",
        namespace="web",
        hugo_repo="https://github.com/user/repo",
    )
    images = {
        "git_image": "alpine/git:2.47.2",
        "hugo_image": "floryn90/hugo:0.155.3-alpine",
        "static_web_server_image": "ghcr.io/static-web-server/static-web-server:2.36.1",
    }
    paths = generate_website(config, tmp_path / "output", images=images)

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"] == {
        "httpGet": {"path": "/", "port": "https", "scheme": "HTTPS"}
    }


def test_generate_website_hugo_uses_cluster_local_backend_tls(tmp_path: Path) -> None:
    """Hugo-backed websites should serve HTTPS with a cluster-local identity."""
    config = WebsiteConfig(
        name="hugo.example.com",
        namespace="web",
        hugo_repo="https://github.com/user/repo",
    )
    images = {
        "git_image": "alpine/git:2.47.2",
        "hugo_image": "floryn90/hugo:0.155.3-alpine",
        "static_web_server_image": "ghcr.io/static-web-server/static-web-server:2.43",
    }
    paths = generate_website(config, tmp_path / "output", images=images)
    documents = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for path in paths
        if (doc := yaml.safe_load(path.read_text()))
    }

    deployment = documents[("Deployment", "hugo-example-com")]
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["ports"] == [
        {"containerPort": 8443, "name": "https", "protocol": "TCP"}
    ]
    assert {entry["name"]: entry["value"] for entry in container["env"]} == {
        "SERVER_PORT": "8443",
        "SERVER_HTTP2_TLS": "true",
        "SERVER_HTTP2_TLS_CERT": "/tls/tls.crt",
        "SERVER_HTTP2_TLS_KEY": "/tls/tls.key",
    }
    assert {"mountPath": "/tls", "name": "tls", "readOnly": True} in container[
        "volumeMounts"
    ]
    assert {
        "name": "tls",
        "secret": {"secretName": "hugo-example-com-internal-tls"},
    } in pod_spec["volumes"]

    service = documents[("Service", "hugo-example-com")]
    assert service["spec"]["ports"] == [
        {
            "name": "https",
            "appProtocol": "https",
            "protocol": "TCP",
            "port": 443,
            "targetPort": "https",
        }
    ]

    route = documents[("HTTPRoute", "hugo-example-com")]
    assert route["spec"]["rules"][0]["backendRefs"][0]["port"] == 443

    certificate = documents[("Certificate", "hugo-example-com-internal")]
    assert certificate["spec"]["dnsNames"] == ["hugo-example-com.web.svc"]
    assert certificate["spec"]["issuerRef"] == {
        "name": "cluster-local",
        "kind": "ClusterIssuer",
        "group": "cert-manager.io",
    }
    assert certificate["spec"]["secretName"] == "hugo-example-com-internal-tls"

    policy = documents[("BackendTLSPolicy", "hugo-example-com")]
    assert policy["spec"]["targetRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": "hugo-example-com",
            "sectionName": "https",
        }
    ]
    assert policy["spec"]["validation"] == {
        "caCertificateRefs": [
            {"group": "", "kind": "ConfigMap", "name": "cluster-local-ca"}
        ],
        "hostname": "hugo-example-com.web.svc",
    }


def test_generate_website_image_backend_remains_http(tmp_path: Path) -> None:
    """Image-backed websites should retain the existing HTTP backend contract."""
    config = WebsiteConfig(
        name="app.example.com",
        namespace="apps",
        image="example.com/app:latest",
    )
    paths = generate_website(config, tmp_path / "output")
    documents = [
        doc for path in paths if (doc := yaml.safe_load(path.read_text())) is not None
    ]

    service = next(doc for doc in documents if doc["kind"] == "Service")
    assert service["spec"]["ports"] == [
        {"protocol": "TCP", "port": 80, "targetPort": 8080}
    ]

    route = next(doc for doc in documents if doc["kind"] == "HTTPRoute")
    assert route["spec"]["rules"][0]["backendRefs"][0]["port"] == 80

    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"] == {"httpGet": {"path": "/healthz", "port": 8080}}
    assert all(doc["kind"] != "BackendTLSPolicy" for doc in documents)
    assert all(
        doc["kind"] != "Certificate"
        or doc["metadata"]["name"] != "app-example-com-internal"
        for doc in documents
    )


def test_generate_website_bundled_template_env(tmp_path: Path) -> None:
    """Bundled template should emit configured environment variables."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        env={"PUBLIC_URL": "https://example.com", "LOG_LEVEL": "debug"},
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [
        {"name": "LOG_LEVEL", "value": "debug"},
        {"name": "PUBLIC_URL", "value": "https://example.com"},
    ]


def test_generate_website_hugo_container_adds_configured_env(tmp_path: Path) -> None:
    """Configured env should be appended to the Hugo container fragment."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        hugo_repo="https://github.com/user/repo",
        env={"LOG_LEVEL": "debug"},
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "SERVER_PORT", "value": "8443"} in container["env"]
    assert {"name": "LOG_LEVEL", "value": "debug"} in container["env"]


def test_generate_website_external_secrets_injects_volumes_and_mounts(
    tmp_path: Path,
) -> None:
    """External secrets should be injected as volumes and mounts in Deployment."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        external_secrets=["/email-password", "/db/credentials"],
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    # Check volume mounts
    mounts = container.get("volumeMounts", [])
    mount_paths = {m["mountPath"]: m["name"] for m in mounts}
    assert "/email-password" in mount_paths
    assert "/db/credentials" in mount_paths
    assert mount_paths["/email-password"] == "email-password"
    assert mount_paths["/db/credentials"] == "db-credentials"

    # Check volumes
    volumes = {v["name"]: v for v in pod_spec.get("volumes", [])}
    assert "email-password" in volumes
    assert "db-credentials" in volumes
    assert volumes["email-password"]["secret"]["secretName"] == "email-password"
    assert volumes["db-credentials"]["secret"]["secretName"] == "db-credentials"


def test_generate_website_external_secrets_single_secret(tmp_path: Path) -> None:
    """Single external secret should work correctly."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        external_secrets=["/api-key"],
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    # Check mount exists
    mounts = container.get("volumeMounts", [])
    assert any(m["mountPath"] == "/api-key" for m in mounts)

    # Check volume exists with correct secret name
    volumes = {v["name"]: v for v in pod_spec.get("volumes", [])}
    assert "api-key" in volumes
    assert volumes["api-key"]["secret"]["secretName"] == "api-key"


def test_generate_website_no_external_secrets(tmp_path: Path) -> None:
    """Deployment without external secrets should not have unnecessary volumes."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    volumes = pod_spec.get("volumes", [])

    # Should not have any secret volumes
    secret_volumes = [v for v in volumes if "secret" in v]
    assert len(secret_volumes) == 0


def test_generate_website_persistence_creates_pvc_and_mount(tmp_path: Path) -> None:
    """Persistence should create a PVC and mount it in the Deployment."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        persistence={"/data": "1Gi"},
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    pvc_path = next(p for p in paths if "persistentvolumeclaim" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())
    pvc_doc = yaml.safe_load(pvc_path.read_text())

    assert pvc_doc["metadata"]["name"] == "my-app-data"
    assert pvc_doc["metadata"]["namespace"] == "production"
    assert pvc_doc["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert {"name": "data", "mountPath": "/data"} in container["volumeMounts"]
    assert {
        "name": "data",
        "persistentVolumeClaim": {"claimName": "my-app-data"},
    } in pod_spec["volumes"]


def test_generate_website_persistence_adds_permission_init_container(
    tmp_path: Path,
) -> None:
    """Persistence should add an initContainer that fixes mount permissions."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        persistence={"/data": "1Gi"},
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    init_containers = deployment_doc["spec"]["template"]["spec"]["initContainers"]
    assert init_containers == [
        {
            "name": "fix-data-permissions",
            "image": "alpine:3.23.4",
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "sh",
                "-c",
                "mkdir -p /data && chown -R 65532:65532 /data",
            ],
            "resources": {},
            "securityContext": {"allowPrivilegeEscalation": False},
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
        }
    ]


def test_generate_website_emptydir_path_adds_mount_and_volume(
    tmp_path: Path,
) -> None:
    """emptydir-path should add an emptyDir mounted into the main container."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        emptydir_path="/cache",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert {
        "name": "emptydir-cache",
        "mountPath": "/cache",
    } in container["volumeMounts"]
    assert {
        "name": "emptydir-cache",
        "emptyDir": {},
    } in pod_spec["volumes"]


def test_generate_website_emptydir_path_adds_permission_init_container(
    tmp_path: Path,
) -> None:
    """emptydir-path should add an initContainer that fixes mount permissions."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        emptydir_path="/cache",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    init_containers = deployment_doc["spec"]["template"]["spec"]["initContainers"]
    assert init_containers == [
        {
            "name": "fix-emptydir-cache-permissions",
            "image": "alpine:3.23.4",
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "sh",
                "-c",
                "mkdir -p /cache && chown -R 65532:65532 /cache",
            ],
            "resources": {},
            "securityContext": {"allowPrivilegeEscalation": False},
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
            "volumeMounts": [{"name": "emptydir-cache", "mountPath": "/cache"}],
        }
    ]


def test_generate_website_custom_token_audiences_inject_projected_tokens(
    tmp_path: Path,
) -> None:
    """Custom token audiences should inject projected service account tokens."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
        custom_token_audiences=["vault", "api"],
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    mounts = container.get("volumeMounts", [])
    assert any(
        m["name"] == "tokens"
        and m["mountPath"] == "/var/run/secrets/tokens"
        and m["readOnly"] is True
        for m in mounts
    )

    volumes = {v["name"]: v for v in pod_spec.get("volumes", [])}
    assert "tokens" in volumes
    assert volumes["tokens"]["projected"]["sources"] == [
        {
            "serviceAccountToken": {
                "path": "vault",
                "expirationSeconds": 3600,
                "audience": "vault",
            }
        },
        {
            "serviceAccountToken": {
                "path": "api",
                "expirationSeconds": 3600,
                "audience": "api",
            }
        },
    ]


def test_generate_website_without_custom_token_audiences_has_no_token_volume(
    tmp_path: Path,
) -> None:
    """Deployment without custom token audiences should not inject a token volume."""
    config = WebsiteConfig(
        name="my-app",
        namespace="production",
        image="nginx:latest",
    )
    paths = generate_website(config, tmp_path / "output")

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    pod_spec = deployment_doc["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod_spec.get("volumes", [])}
    assert "tokens" not in volumes


def test_generate_website_images_available_in_template(tmp_path: Path) -> None:
    """Images dict should be available in template context."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # Create a template that uses image variables
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      initContainers:\n      - image: {{git_image}}\n      containers:\n      - image: {{static_web_server_image}}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production")
    images = {
        "git_image": "alpine/git:2.47.2",
        "static_web_server_image": "ghcr.io/static-web-server/static-web-server:2.36.1",
    }
    paths = generate_website(
        config, tmp_path / "output", images=images, _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert (
        doc["spec"]["template"]["spec"]["initContainers"][0]["image"]
        == "alpine/git:2.47.2"
    )
    assert (
        doc["spec"]["template"]["spec"]["containers"][0]["image"]
        == "ghcr.io/static-web-server/static-web-server:2.36.1"
    )


def test_generate_website_bundled_hugo_uses_images(tmp_path: Path) -> None:
    """Bundled Hugo templates should render image variables from images dict."""
    config = WebsiteConfig(
        name="hugo.example.com",
        namespace="web",
        hugo_repo="https://github.com/user/repo",
    )
    images = {
        "git_image": "alpine/git:2.47.2",
        "hugo_image": "floryn90/hugo:0.155.3-alpine",
        "static_web_server_image": "ghcr.io/static-web-server/static-web-server:2.36.1",
    }
    paths = generate_website(config, tmp_path / "output", images=images)

    deployment_path = next(p for p in paths if "deployment" in p.name)
    deployment_doc = yaml.safe_load(deployment_path.read_text())

    # Check that the main web container has the correct image
    containers = deployment_doc["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    assert (
        containers[0]["image"] == "ghcr.io/static-web-server/static-web-server:2.36.1"
    )

    # Check that init containers have correct images
    init_containers = deployment_doc["spec"]["template"]["spec"]["initContainers"]
    git_container = next(c for c in init_containers if c["name"] == "git")
    assert git_container["image"] == "alpine/git:2.47.2"
    hugo_container = next(c for c in init_containers if c["name"] == "hugo")
    assert hugo_container["image"] == "floryn90/hugo:0.155.3-alpine"


def test_generate_website_default_replicas(tmp_path: Path) -> None:
    """Website without replicas specified should default to 2 replicas."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  replicas: {{replicas}}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production")
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["replicas"] == 2


def test_generate_website_custom_replicas(tmp_path: Path) -> None:
    """Website with custom replicas should use the specified value."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  replicas: {{replicas}}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production", replicas=5)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["replicas"] == 5


def test_generate_website_single_replica(tmp_path: Path) -> None:
    """Website with replicas=1 should use 1 replica."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  replicas: {{replicas}}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production", replicas=1)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["replicas"] == 1


def test_generate_website_zero_replicas(tmp_path: Path) -> None:
    """Website with replicas=0 should use 0 replicas."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{k8s_name}}\nspec:\n  replicas: {{replicas}}\n"
    )

    config = WebsiteConfig(name="my-website", namespace="production", replicas=0)
    paths = generate_website(
        config, tmp_path / "output", _templates_override=templates_dir
    )

    (path,) = paths
    doc = yaml.safe_load(path.read_text())
    assert doc["spec"]["replicas"] == 0
