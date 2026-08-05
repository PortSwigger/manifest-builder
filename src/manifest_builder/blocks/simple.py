# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Simple manifest generation from bundled Mustache templates."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from manifest_builder.blocks import ConfigBlock, GenerationContext
from manifest_builder.config import (
    DEFAULT_REPLICA_COUNT,
    TemplateValue,
    parse_variables,
    validate_known_fields,
)
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


@dataclass
class SimpleConfig:
    """Configuration for a simple deployment built from bundled YAML templates."""

    name: str
    namespace: str
    image: str
    args: list[str] | None = None
    iam_role: str | None = None
    k8s_role: str | None = None
    service_account_annotations: dict[str, str] | None = (
        None  # extra annotations for the managed ServiceAccount
    )
    config: dict[str, Path] | None = None  # container path -> resolved local path
    external_secrets: list[str] | None = (
        None  # mount paths for external secrets (e.g., ["/email-password"])
    )
    custom_token_audiences: list[str] | None = None
    variables: dict[str, TemplateValue] = field(default_factory=dict)
    extra_resources: Path | None = (
        None  # directory with additional YAML resources to include
    )
    replicas: int = DEFAULT_REPLICA_COUNT  # number of deployment replicas
    arch: str | None = None  # node architecture (sets kubernetes.io/arch nodeSelector)
    random_secrets: list[str] | None = (
        None  # secret key names for a RandomSecret mounted at /random-secrets
    )


def validate_simple_config(config: SimpleConfig) -> None:
    """Validate a simple app configuration."""
    for container_path, local_path in (config.config or {}).items():
        if not local_path.exists():
            raise ValueError(
                f"Config file not found for '{config.name}': {local_path} "
                f"(mapped from {container_path})"
            )

    if config.extra_resources is not None:
        if not config.extra_resources.exists():
            raise ValueError(
                f"Extra resources directory not found for '{config.name}': {config.extra_resources}"
            )
        if not config.extra_resources.is_dir():
            raise ValueError(
                f"Extra resources path is not a directory for '{config.name}': {config.extra_resources}"
            )


# Annotation the ServiceAccount template emits for 'iam-role' (EKS IRSA).
IAM_ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"


class SimpleBlock(ConfigBlock[SimpleConfig]):
    """Generate manifests for simple configs."""

    def __init__(self, configs: Sequence[SimpleConfig] | None = None) -> None:
        self.configs = list(configs or [])

    def top_level_config_name(self) -> str:
        return "simple"

    def load_config(
        self,
        data: object,
        source_file: Path,
        root_config: dict[str, Any],
        default_namespace: str | None = None,
        default_image: str | None = None,
    ) -> None:
        if not isinstance(data, list):
            raise ValueError(f"'simple' must be a list of tables in {source_file}")

        variables = parse_variables(root_config.get("variables"), source_file)
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[simple]] entry must be a table in {source_file}"
                )
            self.configs.append(
                _parse_simple_config(
                    item,
                    source_file,
                    variables,
                    index,
                    default_namespace,
                    default_image,
                )
            )

    def iter_configs(self) -> list[SimpleConfig]:
        return self.configs

    def validate(self, config: SimpleConfig, repo_root: Path) -> None:
        validate_simple_config(config)

    def generate(
        self,
        config: SimpleConfig,
        context: GenerationContext,
    ) -> set[Path]:
        return generate_simple(
            config,
            context.output_dir,
            images=context.images,
        )


def _parse_config_files(data: object, source_file: Path) -> dict[str, Path] | None:
    """Parse config file mappings from inline or array-of-table TOML syntax."""
    if data is None:
        return None

    config_data: dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each [[simple.config]] entry must be a table in {source_file}"
                )
            config_data.update(_parse_config_file_table(item, source_file))
    elif isinstance(data, dict):
        config_data = _parse_config_file_table(data, source_file)
    else:
        raise ValueError(f"'simple.config' must be a table in {source_file}")

    config_dir = source_file.parent
    return {
        container_path: config_dir / local_path
        for container_path, local_path in config_data.items()
    }


def _parse_config_file_table(data: dict, source_file: Path) -> dict[str, str]:
    config_data: dict[str, str] = {}
    for container_path, local_path in data.items():
        if not isinstance(container_path, str) or not isinstance(local_path, str):
            raise ValueError(
                f"'simple.config' entries must map strings to strings in {source_file}"
            )
        config_data[container_path] = local_path
    return config_data


def _parse_simple_config(
    data: dict,
    source_file: Path,
    variables: dict[str, TemplateValue],
    table_index: int = 0,
    default_namespace: str | None = None,
    default_image: str | None = None,
) -> SimpleConfig:
    """Parse a simple app configuration from TOML data."""
    validate_known_fields(
        "[[simple]]",
        data,
        {
            "name",
            "namespace",
            "image",
            "args",
            "iam-role",
            "k8s-role",
            "service-account-annotations",
            "config",
            "external-secret",
            "external-secrets",
            "custom-token-audiences",
            "extra-resources",
            "replicas",
            "arch",
            "random-secret",
            "random-secrets",
        },
        source_file,
        table_index,
    )

    if "namespace" not in data and default_namespace is None:
        raise ValueError(f"Missing required field 'namespace' in {source_file}")

    if default_image is not None and "image" in data:
        raise ValueError(
            f"Cannot specify 'image' in {source_file} when generate(image=...) is used"
        )
    if "image" not in data and default_image is None:
        raise ValueError(f"Missing required field 'image' in {source_file}")

    iam_role = data.get("iam-role")
    if iam_role is not None and not isinstance(iam_role, str):
        raise ValueError(f"'iam-role' must be a string in {source_file}")

    k8s_role = data.get("k8s-role")
    if k8s_role is not None and not isinstance(k8s_role, str):
        raise ValueError(f"'k8s-role' must be a string in {source_file}")

    service_account_annotations = data.get("service-account-annotations")
    if service_account_annotations is not None and (
        not isinstance(service_account_annotations, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in service_account_annotations.items()
        )
    ):
        raise ValueError(
            f"'service-account-annotations' must be a table mapping strings to "
            f"strings in {source_file}"
        )

    if (
        iam_role is not None
        and service_account_annotations is not None
        and IAM_ROLE_ANNOTATION in service_account_annotations
    ):
        raise ValueError(
            f"'service-account-annotations' cannot set '{IAM_ROLE_ANNOTATION}' when "
            f"'iam-role' is also specified in {source_file}; use one or the other"
        )

    arch = data.get("arch")
    if arch is not None and not isinstance(arch, str):
        raise ValueError(f"'arch' must be a string in {source_file}")

    args = data.get("args")
    if args is not None and (
        not isinstance(args, list) or not all(isinstance(arg, str) for arg in args)
    ):
        raise ValueError(f"'args' must be a list of strings in {source_file}")

    custom_token_audiences = data.get("custom-token-audiences")
    if custom_token_audiences is not None and (
        not isinstance(custom_token_audiences, list)
        or not all(isinstance(audience, str) for audience in custom_token_audiences)
    ):
        raise ValueError(
            f"'custom-token-audiences' must be a list of strings in {source_file}"
        )

    external_secrets = _parse_external_secrets(data, source_file)

    random_secrets = _parse_random_secrets(data, source_file)

    namespace = data.get("namespace", default_namespace)
    image = data.get("image", default_image)
    name = data.get("name", namespace)
    extra_resources = None
    if "extra-resources" in data:
        extra_resources = source_file.parent / data["extra-resources"]

    return SimpleConfig(
        name=name,
        namespace=namespace,
        image=image,
        args=args,
        iam_role=iam_role,
        k8s_role=k8s_role,
        service_account_annotations=service_account_annotations,
        config=_parse_config_files(data.get("config"), source_file),
        external_secrets=external_secrets,
        custom_token_audiences=custom_token_audiences,
        variables=variables.copy(),
        extra_resources=extra_resources,
        replicas=data.get("replicas", DEFAULT_REPLICA_COUNT),
        arch=arch,
        random_secrets=random_secrets,
    )


def _parse_external_secrets(data: dict, source_file: Path) -> list[str] | None:
    """Normalize singular and plural external secret fields into mount paths.

    A single mount path may also be given as a bare plural-field string for
    backwards compatibility.
    """
    external_secret = data.get("external-secret")
    external_secrets = data.get("external-secrets")

    if external_secret is not None and external_secrets is not None:
        raise ValueError(
            f"Cannot specify both 'external-secret' and 'external-secrets' in {source_file}"
        )

    if external_secret is not None:
        if not isinstance(external_secret, str):
            raise ValueError(f"'external-secret' must be a string in {source_file}")
        return [external_secret]

    if external_secrets is None:
        return None

    if isinstance(external_secrets, str):
        return [external_secrets]

    if not isinstance(external_secrets, list):
        raise ValueError(
            f"'external-secrets' must be a string or list of strings in {source_file}"
        )

    mount_paths: list[str] = []
    for mount_path in external_secrets:
        if not isinstance(mount_path, str):
            raise ValueError(
                f"'external-secrets' must be a string or list of strings in "
                f"{source_file}"
            )
        mount_paths.append(mount_path)
    return mount_paths


def _parse_random_secrets(data: dict, source_file: Path) -> list[str] | None:
    """Normalize the 'random-secret'/'random-secrets' fields into a list of names.

    'random-secret' names a single secret key; 'random-secrets' names a list.
    Specifying both is an error.
    """
    random_secret = data.get("random-secret")
    random_secrets = data.get("random-secrets")

    if random_secret is not None and random_secrets is not None:
        raise ValueError(
            f"Cannot specify both 'random-secret' and 'random-secrets' in {source_file}"
        )

    if random_secret is not None:
        if not isinstance(random_secret, str):
            raise ValueError(f"'random-secret' must be a string in {source_file}")
        return [random_secret]

    if random_secrets is not None:
        if not isinstance(random_secrets, list) or not all(
            isinstance(secret, str) for secret in random_secrets
        ):
            raise ValueError(
                f"'random-secrets' must be a list of strings in {source_file}"
            )
        return random_secrets

    return None


def _inject_configmaps(
    docs: list[dict],
    config: SimpleConfig,
    k8s_name: str,
    context: dict[str, Any],
) -> None:
    if not config.config:
        return

    configmaps = make_configmaps(k8s_name, config.config, context)
    checksum = config_checksum(configmaps)
    for cm in configmaps:
        cm.setdefault("metadata", {})["namespace"] = config.namespace
    docs.extend(configmaps)

    mount_groups = {
        str(Path(container_path).parent) for container_path in config.config
    }
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue

        doc.setdefault("spec", {}).setdefault("template", {}).setdefault(
            "metadata", {}
        ).setdefault("annotations", {})["checksum/config"] = checksum
        pod_spec = (
            doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        )
        for mount_path in sorted(mount_groups):
            cm_name = f"{k8s_name}-{configmap_suffix_from_mount_path(mount_path)}"
            for container in pod_spec.get("containers", []):
                container.setdefault("volumeMounts", []).append(
                    {"name": cm_name, "mountPath": mount_path}
                )
            pod_spec.setdefault("volumes", []).append(
                {"name": cm_name, "configMap": {"name": cm_name}}
            )


def _inject_external_secrets(docs: list[dict], config: SimpleConfig) -> None:
    """Mount externally managed Secrets at the configured paths.

    Each mount path names the Secret it mounts, so ``/email-password`` mounts the
    Secret ``email-password`` and ``/db/credentials`` mounts ``db-credentials``.
    """
    if not config.external_secrets:
        return

    for mount_path in config.external_secrets:
        secret_name = secret_name_from_mount_path(mount_path)
        for doc in docs:
            if doc.get("kind") != "Deployment":
                continue

            pod_spec = (
                doc.setdefault("spec", {})
                .setdefault("template", {})
                .setdefault("spec", {})
            )
            for container in pod_spec.get("containers", []):
                container.setdefault("volumeMounts", []).append(
                    {"name": secret_name, "mountPath": mount_path}
                )
            pod_spec.setdefault("volumes", []).append(
                {"name": secret_name, "secret": {"secretName": secret_name}}
            )


RANDOM_SECRETS_MOUNT_PATH = "/random-secrets"


def _inject_random_secrets(
    docs: list[dict],
    config: SimpleConfig,
    k8s_name: str,
) -> None:
    """Emit a RandomSecret and mount its generated Secret at /random-secrets.

    The randomsecret controller (https://github.com/portswigger/randomsecret)
    reconciles a RandomSecret into a Secret of the same name in the same
    namespace, populating one entry per name in ``spec.secrets``.
    """
    if not config.random_secrets:
        return

    docs.append(
        {
            "apiVersion": "noa.re/v1alpha1",
            "kind": "RandomSecret",
            "metadata": {"name": k8s_name, "namespace": config.namespace},
            "spec": {"secrets": [{"name": secret} for secret in config.random_secrets]},
        }
    )

    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue

        pod_spec = (
            doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
        )
        for container in pod_spec.get("containers", []):
            container.setdefault("volumeMounts", []).append(
                {"name": "random-secrets", "mountPath": RANDOM_SECRETS_MOUNT_PATH}
            )
        pod_spec.setdefault("volumes", []).append(
            {"name": "random-secrets", "secret": {"secretName": k8s_name}}
        )


def generate_simple(
    config: SimpleConfig,
    output_dir: Path,
    images: dict[str, str] | None = None,
    _templates_override: Path | None = None,  # for testing only
) -> set[Path]:
    """Generate manifests for a simple app from bundled Mustache templates."""
    import pystache
    from pystache.common import MissingTags

    if _templates_override is not None:
        templates_dir = _templates_override
    else:
        from importlib.resources import files as get_package_files

        templates_dir = Path(
            str(get_package_files("manifest_builder") / "templates" / "simple")
        )

    context: dict[str, Any] = {
        **(images or {}),
        **config.variables,
        "name": config.name,
        "k8s_name": make_k8s_name(config.name),
        "namespace": config.namespace,
        "replicas": config.replicas,
    }
    context["image"] = config.image
    if config.args:
        context["args"] = config.args
        context["container_args"] = {
            "items": [{"yaml": json.dumps(arg)} for arg in config.args]
        }
    if config.arch:
        context["arch"] = config.arch
    if config.iam_role or config.k8s_role or config.service_account_annotations:
        context["service_account"] = True
    renderer = pystache.Renderer(missing_tags=MissingTags.strict)
    if config.iam_role:
        context["iam_role"] = renderer.render(config.iam_role, context)
    if config.k8s_role:
        context["k8s_role"] = renderer.render(config.k8s_role, context)
    if config.iam_role or config.service_account_annotations:
        context["service_account_has_annotations"] = True
    if config.service_account_annotations:
        context["service_account_annotations"] = [
            {"key": key, "value": json.dumps(renderer.render(value, context))}
            for key, value in config.service_account_annotations.items()
        ]

    docs: list[dict] = []
    for template_file in sorted(templates_dir.glob("*.yaml")):
        if template_file.name.startswith("_"):
            continue

        rendered = pystache.render(template_file.read_text(), context)
        for doc in yaml.safe_load_all(rendered):
            if doc:
                docs.append(doc)

    for doc in docs:
        kind = doc.get("kind")
        if kind and kind not in CLUSTER_SCOPED_KINDS:
            doc.setdefault("metadata", {})["namespace"] = config.namespace

    if config.extra_resources:
        renderer = pystache.Renderer(missing_tags=MissingTags.strict)
        for yaml_file in sorted(config.extra_resources.glob("*.yaml")):
            rendered = renderer.render(yaml_file.read_text(), context)
            for doc in yaml.safe_load_all(rendered):
                if not doc:
                    continue
                kind = doc.get("kind")
                if (
                    kind
                    and kind not in CLUSTER_SCOPED_KINDS
                    and "namespace" not in doc.get("metadata", {})
                ):
                    doc.setdefault("metadata", {})["namespace"] = config.namespace
                docs.append(doc)

    k8s_name = make_k8s_name(config.name)
    _inject_configmaps(docs, config, k8s_name, context)

    _inject_external_secrets(docs, config)

    if config.random_secrets:
        _inject_random_secrets(docs, config, k8s_name)

    if config.custom_token_audiences:
        for doc in docs:
            inject_custom_token_projection(doc, config.custom_token_audiences)

    return write_documents(docs, output_dir, config.namespace, config.name)
