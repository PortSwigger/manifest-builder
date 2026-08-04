# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Website manifest generation from Mustache templates."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from manifest_builder.config import (
    DEFAULT_REPLICA_COUNT,
    ManifestConfig,
    WebsiteConfig,
    validate_known_fields,
    validate_website_config,
)
from manifest_builder.handlers import ConfigHandler, GenerationContext
from manifest_builder.k8s import (
    CLUSTER_SCOPED_KINDS,
    config_checksum,
    configmap_suffix_from_mount_path,
    inject_custom_token_projection,
    make_configmaps,
    make_k8s_name,
    secret_name_from_mount_path,
)
from manifest_builder.output import write_documents


class WebsiteConfigHandler(ConfigHandler):
    """Generate manifests for website configs."""

    def __init__(self, configs: Sequence[WebsiteConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "website"

    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        if not isinstance(data, list):
            raise ValueError(f"'website' must be a list of tables in {source_file}")

        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[website]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_website_config(
                    item, source_file, index, default_namespace, default_image
                )
            )

    def iter_configs(self) -> list[WebsiteConfig]:
        return self.configs

    def validate(self, config: ManifestConfig, repo_root: Path) -> None:
        if not isinstance(config, WebsiteConfig):
            raise TypeError(
                f"WebsiteConfigHandler cannot process {type(config).__name__}"
            )
        validate_website_config(config)

    def generate(
        self,
        config: ManifestConfig,
        context: GenerationContext,
    ) -> set[Path]:
        if not isinstance(config, WebsiteConfig):
            raise TypeError(
                f"WebsiteConfigHandler cannot process {type(config).__name__}"
            )
        return generate_website(
            config,
            context.output_dir,
            images=context.images,
            verbose=context.verbose,
        )


def _parse_website_config(
    data: dict,
    source_file: Path,
    table_index: int = 0,
    default_namespace: str | None = None,
    default_image: str | None = None,
) -> WebsiteConfig:
    """Parse a website app configuration from TOML data."""
    validate_known_fields(
        "[[website]]",
        data,
        {
            "name",
            "namespace",
            "hugo-repo",
            "image",
            "args",
            "env",
            "emptydir-path",
            "config",
            "extra-hostnames",
            "external-secrets",
            "custom-token-audiences",
            "persistence",
            "replicas",
        },
        source_file,
        table_index,
    )

    required_fields = ["name"]
    if default_namespace is None:
        required_fields.append("namespace")
    for required_field in required_fields:
        if required_field not in data:
            raise ValueError(
                f"Missing required field '{required_field}' in {source_file}"
            )

    hugo_repo = data.get("hugo-repo")
    image = data.get("image")
    if default_image is not None and image is not None:
        raise ValueError(
            f"Cannot specify 'image' in {source_file} when generate(image=...) is used"
        )
    if default_image is not None:
        image = default_image
        hugo_repo = None

    if hugo_repo and image:
        raise ValueError(
            f"Cannot specify both 'hugo-repo' and 'image' in {source_file}"
        )

    config_dir = source_file.parent
    config_dict = None
    if "config" in data:
        config_dict = {
            container_path: config_dir / local_path
            for container_path, local_path in data["config"].items()
        }

    external_secrets = data.get("external-secrets")
    if external_secrets is not None and isinstance(external_secrets, str):
        external_secrets = [external_secrets]

    custom_token_audiences = data.get("custom-token-audiences")
    if custom_token_audiences is not None and (
        not isinstance(custom_token_audiences, list)
        or not all(isinstance(audience, str) for audience in custom_token_audiences)
    ):
        raise ValueError(
            f"'custom-token-audiences' must be a list of strings in {source_file}"
        )

    env = data.get("env")
    if env is not None:
        _validate_env_config(env, source_file)

    emptydir_path = data.get("emptydir-path")
    if emptydir_path is not None:
        _validate_absolute_mount_path("emptydir-path", emptydir_path, source_file)

    return WebsiteConfig(
        name=data["name"],
        namespace=data.get("namespace", default_namespace),
        hugo_repo=hugo_repo,
        image=image,
        args=data.get("args"),
        env=env,
        emptydir_path=emptydir_path,
        config=config_dict,
        extra_hostnames=data.get("extra-hostnames"),
        external_secrets=external_secrets,
        custom_token_audiences=custom_token_audiences,
        persistence=data.get("persistence"),
        replicas=data.get("replicas", DEFAULT_REPLICA_COUNT),
    )


def _validate_env_config(env: object, source_file: Path) -> None:
    """Validate website env configuration parsed from TOML."""
    if not isinstance(env, dict):
        raise ValueError(
            f"'env' must be a table of string keys and values in {source_file}"
        )
    invalid = [
        key
        for key, value in env.items()
        if not isinstance(key, str) or not isinstance(value, str)
    ]
    if invalid:
        names = ", ".join(repr(key) for key in sorted(invalid))
        raise ValueError(
            f"'env' values must be strings in {source_file}; invalid keys: {names}"
        )


def _validate_absolute_mount_path(
    field_name: str, mount_path: object, source_file: Path
) -> None:
    """Validate a configured Kubernetes mount path."""
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        raise ValueError(
            f"'{field_name}' must be an absolute path string in {source_file}"
        )
    if mount_path == "/":
        raise ValueError(f"'{field_name}' cannot be '/' in {source_file}")


def _load_fragments(templates_dir: Path, context: dict) -> dict[str, dict]:
    """Load and render underscore-prefixed fragment templates.

    Fragment templates (files starting with _) are rendered with the same Mustache
    context as regular templates but are not written to output. They can be used by
    website.py code to programmatically inject content into documents.

    Args:
        templates_dir: Directory containing template files
        context: Mustache rendering context

    Returns:
        Dict mapping fragment name (filename without leading _ and .yaml suffix)
        to the parsed YAML document
    """
    import pystache

    fragments = {}
    for fragment_file in sorted(templates_dir.glob("_*.yaml")):
        # Strip leading underscore and .yaml suffix to get the fragment name
        name = fragment_file.stem[1:]
        template_source = fragment_file.read_text()
        rendered = pystache.render(template_source, context)
        doc = yaml.safe_load(rendered)
        if doc:
            fragments[name] = doc
    return fragments


def _persistent_volume_claim_name(k8s_name: str, mount_path: str) -> str:
    """Generate a PVC claim name for a website persistence mount."""
    return f"{k8s_name}-{secret_name_from_mount_path(mount_path)}"


def _emptydir_volume_name(mount_path: str) -> str:
    """Generate a volume name for an emptyDir mount."""
    return f"emptydir-{secret_name_from_mount_path(mount_path)}"


def _permission_init_container(volume_name: str, mount_path: str) -> dict[str, object]:
    """Build an init container that makes a mounted path writable by nonroot."""
    return {
        "name": f"fix-{volume_name}-permissions",
        "image": "alpine:3.23.4",
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "sh",
            "-c",
            f"mkdir -p {mount_path} && chown -R 65532:65532 {mount_path}",
        ],
        "resources": {},
        "securityContext": {"allowPrivilegeEscalation": False},
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
        "volumeMounts": [{"name": volume_name, "mountPath": mount_path}],
    }


def _make_persistent_volume_claims(
    k8s_name: str, persistence: dict[str, str]
) -> list[dict]:
    """Build PersistentVolumeClaim objects for configured persistence mounts."""
    return [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": _persistent_volume_claim_name(k8s_name, mount_path),
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": size}},
            },
        }
        for mount_path, size in sorted(persistence.items())
    ]


def _inject_persistence_mounts(
    doc: dict, k8s_name: str, persistence: dict[str, str]
) -> None:
    """Inject PVC volumes, mounts, and permission-fixer init containers."""
    if doc.get("kind") != "Deployment":
        return

    pod_spec = (
        doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    )
    for mount_path in sorted(persistence):
        volume_name = secret_name_from_mount_path(mount_path)
        claim_name = _persistent_volume_claim_name(k8s_name, mount_path)

        for container in pod_spec.get("containers", []):
            container.setdefault("volumeMounts", []).append(
                {"name": volume_name, "mountPath": mount_path}
            )

        pod_spec.setdefault("volumes", []).append(
            {
                "name": volume_name,
                "persistentVolumeClaim": {"claimName": claim_name},
            }
        )
        pod_spec.setdefault("initContainers", []).append(
            _permission_init_container(volume_name, mount_path)
        )


def _inject_emptydir_mount(doc: dict, mount_path: str) -> None:
    """Inject an emptyDir volume, main-container mount, and permission fixer."""
    if doc.get("kind") != "Deployment":
        return

    pod_spec = (
        doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    )
    volume_name = _emptydir_volume_name(mount_path)

    containers = pod_spec.get("containers", [])
    if containers:
        containers[0].setdefault("volumeMounts", []).append(
            {"name": volume_name, "mountPath": mount_path}
        )

    pod_spec.setdefault("volumes", []).append(
        {
            "name": volume_name,
            "emptyDir": {},
        }
    )
    pod_spec.setdefault("initContainers", []).append(
        _permission_init_container(volume_name, mount_path)
    )


def _make_env_vars(env: dict[str, str]) -> list[dict[str, str]]:
    """Build Kubernetes EnvVar objects from configured environment variables."""
    return [{"name": name, "value": value} for name, value in sorted(env.items())]


def _inject_env_vars(doc: dict, env: dict[str, str]) -> None:
    """Inject configured environment variables into all Deployment containers."""
    if doc.get("kind") != "Deployment":
        return

    pod_spec = (
        doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    )
    for container in pod_spec.get("containers", []):
        container.setdefault("env", []).extend(_make_env_vars(env))


def _inject_liveness_probe(
    doc: dict, path: str, port: int | str = 8080, scheme: str | None = None
) -> None:
    """Inject an HTTP or HTTPS liveness probe into Deployment containers."""
    if doc.get("kind") != "Deployment":
        return

    pod_spec = (
        doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    )
    http_get: dict[str, object] = {"path": path, "port": port}
    if scheme is not None:
        http_get["scheme"] = scheme
    for container in pod_spec.get("containers", []):
        container.setdefault(
            "livenessProbe",
            {"httpGet": http_get},
        )


def generate_website(
    config: WebsiteConfig,
    output_dir: Path,
    images: dict[str, str] | None = None,
    verbose: bool = False,
    _templates_override: Path | None = None,  # for testing only
) -> set[Path]:
    """Generate manifests for a website app from bundled Mustache templates.

    Args:
        config: Website configuration
        output_dir: Directory to write generated manifests
        images: Dict mapping image variable names to image references (e.g., {"git_image": "alpine/git:2.47.2"})
        verbose: If True, print detailed output
        _templates_override: Override templates directory (for testing only)

    Returns:
        Set of paths written
    """
    import pystache

    # Use the bundled website templates from the package (or override for testing)
    if _templates_override is not None:
        templates_dir = _templates_override
    else:
        from importlib.resources import files as get_package_files

        templates_dir = Path(
            str(get_package_files("manifest_builder") / "templates" / "web")
        )

    # Prepare the template context with name, k8s_name, and optional image/args/git_repo
    context: dict[str, Any] = {
        "name": config.name,
        "k8s_name": make_k8s_name(config.name),
        "namespace": config.namespace,
        "replicas": config.replicas,
    }
    if images:
        context.update(images)
    if config.image:
        context["image"] = config.image
    if config.args:
        context["args"] = config.args
    if config.hugo_repo:
        context["git_repo"] = config.hugo_repo
    if config.extra_hostnames:
        normalized = (
            config.extra_hostnames
            if isinstance(config.extra_hostnames, list)
            else [config.extra_hostnames]
        )

        class ExtraHostname:
            def __init__(self, hostname: str, k8s_hostname: str) -> None:
                self.hostname = hostname
                self.k8s_hostname = k8s_hostname

        context["extra_hostnames"] = [
            ExtraHostname(h, make_k8s_name(h)) for h in normalized
        ]
        context["has_extra_hostnames"] = True

    docs: list[dict] = []
    for template_file in sorted(templates_dir.glob("*.yaml")):
        # Skip fragment templates (starting with underscore)
        if template_file.name.startswith("_"):
            continue

        with open(template_file) as f:
            template_source = f.read()

        # Render the Mustache template
        rendered = pystache.render(template_source, context)

        # Parse the rendered YAML documents
        for doc in yaml.safe_load_all(rendered):
            if doc:
                docs.append(doc)

    # Load fragment templates for use in post-processing injection logic
    fragments = _load_fragments(templates_dir, context)

    # Add the namespace metadata to namespaced resources
    for doc in docs:
        kind = doc.get("kind")
        if kind and kind not in CLUSTER_SCOPED_KINDS:
            doc.setdefault("metadata", {})["namespace"] = config.namespace

    # Apply Hugo fragments and annotations if configured
    if config.hugo_repo:
        for doc in docs:
            if doc.get("kind") == "Deployment":
                # Inject Hugo init containers if available
                if "hugo_initcontainers" in fragments:
                    doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                        "spec", {}
                    )["initContainers"] = fragments["hugo_initcontainers"]

                # Inject Hugo container if available (replaces existing containers)
                if "hugo_container" in fragments:
                    doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                        "spec", {}
                    )["containers"] = [fragments["hugo_container"]]

                # Inject Hugo volumes if available
                if "hugo_volumes" in fragments:
                    doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                        "spec", {}
                    )["volumes"] = fragments["hugo_volumes"]

                # Add Hugo repo annotation
                doc.setdefault("metadata", {}).setdefault("annotations", {})["hugo"] = (
                    config.hugo_repo
                )

    liveness_probe_path = "/" if config.hugo_repo else "/healthz"
    for doc in docs:
        _inject_liveness_probe(
            doc,
            liveness_probe_path,
            port="https" if config.hugo_repo else 8080,
            scheme="HTTPS" if config.hugo_repo else None,
        )

    if config.env:
        for doc in docs:
            _inject_env_vars(doc, config.env)

    # Generate ConfigMaps from config files and inject volumes/mounts if configured
    if config.config:
        k8s_name = make_k8s_name(config.name)
        configmaps = make_configmaps(k8s_name, config.config)
        checksum = config_checksum(configmaps)
        # Inject namespace into ConfigMaps (they're added after the main namespace loop)
        for cm in configmaps:
            cm.setdefault("metadata", {})["namespace"] = config.namespace
        docs.extend(configmaps)

        # Determine mount points from config (grouped by top-level directory)
        mount_groups = {
            str(Path(container_path).parent) for container_path in config.config
        }
        for doc in docs:
            if doc.get("kind") == "Deployment":
                doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
                    "metadata", {}
                ).setdefault("annotations", {})["checksum/config"] = checksum
                pod_spec = (
                    doc.setdefault("spec", {})
                    .setdefault("template", {})
                    .setdefault("spec", {})
                )
                for mount_path in sorted(mount_groups):
                    cm_name = (
                        f"{k8s_name}-{configmap_suffix_from_mount_path(mount_path)}"
                    )
                    # Add volumeMount to each container
                    for container in pod_spec.get("containers", []):
                        container.setdefault("volumeMounts", []).append(
                            {"name": cm_name, "mountPath": mount_path}
                        )
                    # Add volume at pod level
                    pod_spec.setdefault("volumes", []).append(
                        {"name": cm_name, "configMap": {"name": cm_name}}
                    )

    # Handle external secrets if configured
    if config.external_secrets:
        k8s_name = make_k8s_name(config.name)
        for mount_path in config.external_secrets:
            secret_name = secret_name_from_mount_path(mount_path)
            # Inject volumes and mounts into Deployment
            for doc in docs:
                if doc.get("kind") == "Deployment":
                    pod_spec = (
                        doc.setdefault("spec", {})
                        .setdefault("template", {})
                        .setdefault("spec", {})
                    )
                    # Add volumeMount to each container
                    for container in pod_spec.get("containers", []):
                        container.setdefault("volumeMounts", []).append(
                            {"name": secret_name, "mountPath": mount_path}
                        )
                    # Add volume at pod level
                    pod_spec.setdefault("volumes", []).append(
                        {"name": secret_name, "secret": {"secretName": secret_name}}
                    )

    if config.persistence:
        k8s_name = make_k8s_name(config.name)
        pvcs = _make_persistent_volume_claims(k8s_name, config.persistence)
        for pvc in pvcs:
            pvc.setdefault("metadata", {})["namespace"] = config.namespace
        docs.extend(pvcs)

        for doc in docs:
            _inject_persistence_mounts(doc, k8s_name, config.persistence)

    if config.emptydir_path:
        for doc in docs:
            _inject_emptydir_mount(doc, config.emptydir_path)

    if config.custom_token_audiences:
        for doc in docs:
            inject_custom_token_projection(doc, config.custom_token_audiences)

    return write_documents(docs, output_dir, config.namespace, config.name)
