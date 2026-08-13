# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""YAML serialization and manifest file writing.

Config blocks hand finished Kubernetes documents to :func:`write_documents`
or :func:`write_manifests`, which route each object to the namespace or
``cluster`` directory under the output root.
"""

import io
import logging
from pathlib import Path
from typing import Any

import yaml

from manifest_builder.k8s import is_cluster_scoped, load_crd_scopes

logger = logging.getLogger(__name__)

YAML_LOADER: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
YAML_DUMPER: type[yaml.Dumper] = getattr(yaml, "CDumper", yaml.Dumper)


def _literal_str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
    """Represent multi-line strings using literal block scalar (|-) syntax."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


# Register the custom representer for multi-line strings.
yaml.add_representer(str, _literal_str_representer, Dumper=YAML_DUMPER)


def load_all_yaml(content: str) -> list[Any]:
    """Parse a multi-document YAML string, dropping empty documents."""
    return [doc for doc in yaml.load_all(content, Loader=YAML_LOADER) if doc]


def dump_yaml(doc: Any, stream: Any) -> None:
    """Write a single YAML document to ``stream``."""
    yaml.dump(
        doc,
        stream,
        Dumper=YAML_DUMPER,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def dump_all_yaml(docs: list[Any]) -> str:
    """Serialize documents into a single multi-document YAML string."""
    stream = io.StringIO()
    yaml.dump_all(
        docs,
        stream,
        Dumper=YAML_DUMPER,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return stream.getvalue()


def _strip_helm_from_metadata(metadata: dict) -> None:
    for key in ("labels", "annotations"):
        if key in metadata and metadata[key] is not None:
            metadata[key] = {
                k: v
                for k, v in metadata[key].items()
                if not k.startswith("helm.sh/")
                and not (k == "app.kubernetes.io/managed-by" and v == "Helm")
            }
            if not metadata[key]:
                del metadata[key]


def strip_helm_metadata(doc: dict) -> dict:
    """Remove helm-specific labels and annotations from a Kubernetes manifest."""
    _strip_helm_from_metadata(doc.get("metadata") or {})
    template_metadata = (doc.get("spec") or {}).get("template", {}).get("metadata")
    if template_metadata:
        _strip_helm_from_metadata(template_metadata)
    return doc


def write_documents(
    documents: list[dict],
    output_dir: Path,
    namespace: str | None,
    app_name: str | None = None,
    crd_scopes: dict[tuple[str, str], str] | None = None,
) -> set[Path]:
    """Write each Kubernetes document to its own file under ``output_dir``.

    Args:
        documents: Kubernetes objects to write
        output_dir: Base output directory
        namespace: Namespace for objects that do not set one themselves
        app_name: If provided, written as a comment at the top of each file
        crd_scopes: Scopes of custom resources, as returned by
            :func:`~manifest_builder.k8s.load_crd_scopes`. Read from
            ``documents`` when not given.

    Returns:
        Set of paths written
    """
    if crd_scopes is None:
        crd_scopes = load_crd_scopes(documents)
    written: set[Path] = set()
    for doc in documents:
        kind = doc.get("kind", "unknown")
        name = doc.get("metadata", {}).get("name", "unknown")
        try:
            strip_helm_metadata(doc)
        except Exception as e:
            raise RuntimeError(
                f"Failed to strip helm metadata from {kind}/{name}: {e}"
            ) from e

        if not kind or not name:
            continue

        if is_cluster_scoped(doc, crd_scopes):
            subdir = "cluster"
        else:
            subdir = doc.get("metadata", {}).get("namespace") or namespace
            if subdir is None:
                raise ValueError(
                    f"Cannot write namespaced resource {kind}/{name} without a namespace"
                )
        dest_dir = output_dir / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)

        filename = _manifest_filename(kind, name)
        output_path = dest_dir / filename

        with open(output_path, "w") as f:
            if app_name:
                f.write(f"# Source: {app_name}\n")
            dump_yaml(doc, f)

        logger.debug(f"Wrote {subdir}/{filename}")
        written.add(output_path)

    return written


def _manifest_filename(kind: str, name: str) -> str:
    """Return a filesystem-safe manifest filename for a Kubernetes object."""
    return f"{kind.lower()}-{name}.yaml".replace(":", "_")


def _expand_lists(
    documents: list[dict], default_namespace: str | None = None
) -> list[tuple[dict, str | None]]:
    """Replace ``kind: List`` documents with the objects they carry.

    Some charts wrap several objects in a single ``List``. Each item becomes a
    document of its own, and nested lists are expanded too.

    Returns:
        Each document paired with the namespace of the list it came from, which
        is the namespace to fall back on for that document
    """
    expanded: list[tuple[dict, str | None]] = []
    for doc in documents:
        if doc.get("kind") != "List":
            expanded.append((doc, default_namespace))
            continue

        items = doc.get("items") or []
        if not isinstance(items, list):
            name = doc.get("metadata", {}).get("name")
            raise TypeError(f"items of List {name} is not a list")

        list_namespace = (doc.get("metadata") or {}).get("namespace")
        if not isinstance(list_namespace, str):
            list_namespace = default_namespace
        for item in items:
            if not isinstance(item, dict):
                name = doc.get("metadata", {}).get("name")
                raise TypeError(f"item of List {name} is not a mapping")
            expanded.extend(_expand_lists([item], list_namespace))

    return expanded


def write_manifests(
    content: str,
    output_dir: Path,
    namespace: str,
    app_name: str | None = None,
) -> set[Path]:
    """
    Split YAML content into individual documents and write each to a separate file.

    Files are named following the pattern: kind-name.yaml, written into
    output_dir/<namespace>/ for namespaced resources or output_dir/cluster/
    for cluster-scoped resources, where a CustomResourceDefinition among the
    documents decides the scope of the kinds it defines. ``kind: List``
    documents are expanded so each item they carry gets its own file, falling
    back to the list's namespace rather than the chart namespace.

    Args:
        content: YAML manifest content with multiple documents
        output_dir: Base output directory
        namespace: Kubernetes namespace (used for namespaced resources)
        app_name: If provided, written as a comment at the top of each file

    Returns:
        Set of paths written

    Raises:
        OSError: If files cannot be written
    """
    expanded = _expand_lists(load_all_yaml(content))

    # Filter out Helm test hook documents and log them
    filtered_documents: list[tuple[dict, str | None]] = []
    skipped_hooks = 0
    for doc, list_namespace in expanded:
        kind = doc.get("kind", "unknown")
        annotations = doc.get("metadata", {}).get("annotations") or {}
        if not isinstance(annotations, dict):
            raise TypeError(
                f"failed to read annotations on object {kind} from {app_name}, "
                f"item annotations is not a dict"
            )
        hook_value = annotations.get("helm.sh/hook")
        if hook_value is not None:
            name = doc.get("metadata", {}).get("name")
            skipped_hooks += 1
            logger.debug(f"Skipping {kind} {name} (helm.sh/hook={hook_value})")
        else:
            filtered_documents.append((doc, list_namespace))
    if skipped_hooks:
        logger.info(f"Skipped {skipped_hooks} helm hook objects")

    documents = [doc for doc, _ in filtered_documents]
    crd_scopes = load_crd_scopes(documents)

    # Add namespace to namespaced resources that don't already have one
    for doc, list_namespace in filtered_documents:
        if (
            doc.get("kind")
            and not is_cluster_scoped(doc, crd_scopes)
            and "namespace" not in doc.get("metadata", {})
        ):
            doc.setdefault("metadata", {})["namespace"] = list_namespace or namespace

    return write_documents(documents, output_dir, namespace, app_name, crd_scopes)
