# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Return types for manifest generation."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, order=True)
class KubernetesObjectRef:
    """Stable identity for a Kubernetes object."""

    kind: str
    namespace: str | None
    name: str
    api_version: str = ""
    """The document's apiVersion, for example ``rbac.authorization.k8s.io/v1``.

    A kind alone is not unique in a cluster, so consumers that resolve a ref
    through API discovery need the group to tell candidates apart. Declared
    last with a default so the kind/namespace/name ordering and positional
    construction both keep working.
    """


@dataclass
class GenerationResult:
    """Summary of manifests written and object-level git changes."""

    written_paths: set[Path] = field(default_factory=set)
    created_or_modified: set[KubernetesObjectRef] = field(default_factory=set)
    removed: set[KubernetesObjectRef] = field(default_factory=set)
    manifest_ids: dict[KubernetesObjectRef, str] = field(default_factory=dict)
    """The ``noa.re/manifest-id`` each created or modified object was written with.

    A manifest-id is a hash of the object's generated content, so a live object
    carrying it has that content.
    """
