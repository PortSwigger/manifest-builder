# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Shared helpers for naming and building Kubernetes objects.

Config blocks use these to derive Kubernetes-safe names, build ConfigMaps
from mounted config files, and inject volumes into pod specs.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pystache
from pystache.common import MissingTags

# Kubernetes resource kinds that are cluster-scoped (not namespaced).
#
# A CustomResourceDefinition rendered alongside a custom resource is
# authoritative about its scope, so custom kinds only need listing here when no
# chart in the run defines them. That is the case for the crossplane pkg.crossplane.io
# kinds below: crossplane applies its own CRDs at runtime from the core binary,
# and its Helm chart ships none, so nothing in a generated manifest set ever
# says what scope they have.
CLUSTER_SCOPED_KINDS = {
    "APIService",
    "CertificateSigningRequest",
    "ClusterRole",
    "ClusterRoleBinding",
    "ClusterProviderConfig",
    "CSIDriver",
    "CSINode",
    "CustomResourceDefinition",
    "DeploymentRuntimeConfig",
    "FlowSchema",
    "Function",
    "IngressClass",
    "Namespace",
    "Node",
    "PersistentVolume",
    "PriorityClass",
    "PriorityLevelConfiguration",
    "Provider",
    "RuntimeClass",
    "StorageClass",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "VolumeAttachment",
}


def load_crd_scopes(documents: list[dict]) -> dict[tuple[str, str], str]:
    """Read the scope of each custom resource defined by a CRD in ``documents``.

    Charts that ship their own CustomResourceDefinitions describe the scope of
    the kinds they introduce, which beats guessing from :data:`CLUSTER_SCOPED_KINDS`.

    Returns:
        Mapping of (API group, kind) to the CRD's ``spec.scope``
    """
    scopes: dict[tuple[str, str], str] = {}
    for doc in documents:
        if doc.get("kind") != "CustomResourceDefinition":
            continue
        spec = doc.get("spec") or {}
        group = spec.get("group")
        kind = (spec.get("names") or {}).get("kind")
        scope = spec.get("scope")
        if isinstance(group, str) and isinstance(kind, str) and isinstance(scope, str):
            scopes[(group, kind)] = scope
    return scopes


def is_cluster_scoped(doc: dict, crd_scopes: dict[tuple[str, str], str]) -> bool:
    """Report whether a Kubernetes object lives outside any namespace.

    A CRD carried alongside the object is authoritative about the kinds it
    defines; everything else falls back to :data:`CLUSTER_SCOPED_KINDS`.
    """
    kind = doc.get("kind")
    api_version = doc.get("apiVersion")
    if isinstance(kind, str) and isinstance(api_version, str):
        group = api_version.rpartition("/")[0]
        scope = crd_scopes.get((group, kind))
        if scope is not None:
            return scope == "Cluster"
    return kind in CLUSTER_SCOPED_KINDS


def make_k8s_name(name: str) -> str:
    """Convert a name to a Kubernetes-safe name by replacing periods with dashes.

    Kubernetes object names must conform to RFC 1035 label naming rules:
    - Must be 63 characters or less
    - Must begin with an alphanumeric character
    - Must end with an alphanumeric character
    - May contain only lowercase alphanumerics or hyphens

    This converts names like 'example.com' to 'example-com'.

    Args:
        name: The original name (e.g., a domain name)

    Returns:
        A Kubernetes-safe name with periods replaced by dashes

    Raises:
        ValueError: If the resulting name violates RFC 1035 label naming constraints
    """
    k8s_name = name.replace(".", "-").lower()

    # Validate against RFC 1035 label naming constraints
    if not k8s_name:
        raise ValueError(f"Name '{name}' results in an empty Kubernetes object name")

    if len(k8s_name) > 63:
        raise ValueError(
            f"Kubernetes name '{k8s_name}' exceeds 63 character limit ({len(k8s_name)} characters)"
        )

    if not k8s_name[0].isalnum():
        raise ValueError(
            f"Kubernetes name '{k8s_name}' must start with an alphanumeric character, "
            f"but starts with '{k8s_name[0]}'"
        )

    if not k8s_name[-1].isalnum():
        raise ValueError(
            f"Kubernetes name '{k8s_name}' must end with an alphanumeric character, "
            f"but ends with '{k8s_name[-1]}'"
        )

    # Verify that only valid characters are present (lowercase alphanumeric and hyphens)
    if not all(c.isalnum() or c == "-" for c in k8s_name):
        invalid_chars = {c for c in k8s_name if not (c.isalnum() or c == "-")}
        raise ValueError(
            f"Kubernetes name '{k8s_name}' contains invalid characters: {invalid_chars}. "
            f"Only lowercase alphanumerics and hyphens are allowed."
        )

    return k8s_name


def secret_name_from_mount_path(mount_path: str) -> str:
    """Generate a secret name from a mount path.

    Removes the leading / and converts subsequent / to -.

    Examples:
        "/email-password" -> "email-password"
        "/config/database" -> "config-database"

    Args:
        mount_path: The mount path (e.g., "/email-password")

    Returns:
        The generated secret name
    """
    if not mount_path.startswith("/"):
        raise ValueError(f"Mount path must start with /: {mount_path}")
    return mount_path[1:].replace("/", "-")


def configmap_suffix_from_mount_path(mount_path: str) -> str:
    """Generate a ConfigMap name suffix from a mount path."""
    if mount_path == "/":
        return "root"
    return mount_path.lstrip("/").replace("/", "-")


def make_configmaps(
    k8s_name: str,
    config_files: dict[str, Path],
    context: dict[str, Any] | None = None,
) -> list[dict]:
    """Build ConfigMap objects grouped by the parent directory of each path.

    Args:
        k8s_name: Kubernetes-safe name for the app (used in ConfigMap names)
        config_files: Dict mapping container path -> resolved local file path
        context: Optional Mustache context for rendering file contents

    Returns:
        List of ConfigMap dictionaries grouped by parent directory
    """
    renderer = None
    if context is not None:
        renderer = pystache.Renderer(missing_tags=MissingTags.strict)

    groups: dict[str, dict[str, str]] = {}
    for container_path, local_path in config_files.items():
        path = Path(container_path)
        if not path.is_absolute():
            raise ValueError(f"Config file path must be absolute: {container_path}")
        mount_path = str(path.parent)
        if mount_path == ".":
            raise ValueError(
                f"Config file path must include a filename: {container_path}"
            )
        data_key = path.name
        content = local_path.read_text()
        if renderer is not None:
            content = renderer.render(content, context)
        groups.setdefault(mount_path, {})[data_key] = content

    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{k8s_name}-{configmap_suffix_from_mount_path(mount_path)}"
            },
            "data": data,
        }
        for mount_path, data in sorted(groups.items())
    ]


def config_checksum(configmaps: list[dict]) -> str:
    """Build a deterministic checksum for generated ConfigMap contents."""
    normalized = [
        {
            "name": configmap["metadata"]["name"],
            "data": {
                key: value for key, value in sorted(configmap.get("data", {}).items())
            },
        }
        for configmap in sorted(configmaps, key=lambda item: item["metadata"]["name"])
    ]
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inject_custom_token_projection(doc: dict, audiences: list[str]) -> None:
    """Inject a projected service account token volume into a Deployment."""
    if doc.get("kind") != "Deployment":
        return

    pod_spec = (
        doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    )

    for container in pod_spec.get("containers", []):
        container.setdefault("volumeMounts", []).append(
            {
                "name": "tokens",
                "mountPath": "/var/run/secrets/tokens",
                "readOnly": True,
            }
        )

    pod_spec.setdefault("volumes", []).append(
        {
            "name": "tokens",
            "projected": {
                "sources": [
                    {
                        "serviceAccountToken": {
                            "path": audience,
                            "expirationSeconds": 3600,
                            "audience": audience,
                        }
                    }
                    for audience in audiences
                ]
            },
        }
    )
