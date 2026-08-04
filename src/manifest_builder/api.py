# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
import hashlib
import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from manifest_builder import __version__
from manifest_builder.config import (
    TemplateValue,
    load_configs,
    load_extra_variables,
    load_images,
    load_owned_namespaces,
    load_toml_file,
    resolve_configs,
)
from manifest_builder.copy import CopyConfigHandler
from manifest_builder.generator import (
    HelmConfigHandler,
    generate_manifests,
    plural,
)
from manifest_builder.git_utils import (
    GitManifestChanges,
    create_manifest_commit,
    get_git_commit,
    get_git_commit_subject,
    get_git_head_file,
    get_git_manifest_changes,
    get_git_tracked_remote,
    is_git_checkout,
    is_git_dirty,
)
from manifest_builder.helmfile import load_helmfile
from manifest_builder.output import dump_yaml, load_all_yaml
from manifest_builder.public_repo import PublicRepoConfigHandler
from manifest_builder.result import GenerationResult, KubernetesObjectRef
from manifest_builder.simple import SimpleConfigHandler
from manifest_builder.website import WebsiteConfigHandler

logger = logging.getLogger(__name__)
DEPLOY_ID_ANNOTATION = "noa.re/deploy-id"


def generate(
    config: Path,
    output: Path,
    repo_root: Path | None = None,
    verbose: bool = False,
    create_commit: bool = False,
    allow_dirty_config: bool = False,
    vars_from: Path | None = None,
    namespace: str | None = None,
    image: str | None = None,
    vars: Mapping[str, TemplateValue] | None = None,
) -> GenerationResult:
    """Generate manifests from ``config`` into ``output``.

    Args:
        config: Configuration directory path, resolved relative to ``repo_root`` if
            it is not absolute.
        output: Output directory path, resolved relative to ``repo_root`` if it is
            not absolute.
        repo_root: Repository root for resolving relative paths. Defaults to the
            current working directory.
        verbose: If True, emit additional progress logging.
        create_commit: If True, create a git commit in the output checkout.
        allow_dirty_config: If True, allow commit creation when the config
            checkout has local changes.
        vars_from: Optional path to a TOML file of extra template variables,
            merged into the ``[variables]`` table from config.toml. Resolved
            relative to ``repo_root`` if it is not absolute.
        namespace: Optional namespace-owner mode. When set, config entries may
            omit their ``namespace`` field, an owner declaration is written to
            ``output/owners/<namespace>.toml``, and cluster-scoped output is
            rejected.
        image: Optional image override for namespace-owner mode. When set,
            simple and website config entries use this image and must not also
            set an ``image`` field in the config file.
        vars: Optional extra template variables, merged into the ``[variables]``
            table from config.toml the same way as variables loaded with
            ``vars_from``.

    Returns:
        Summary of written paths and object-level changes.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    config = repo_root / config
    output = repo_root / output
    extra_variables = _load_api_variables(repo_root, vars_from, vars)

    if image is not None and namespace is None:
        raise ValueError("generate(image=...) can only be used when namespace is set")

    if create_commit and not is_git_checkout(output):
        raise ValueError(
            f"It doesn't seem like {output} is a git checkout, "
            "a requirement to be able to generate a commit."
        )

    if create_commit and is_git_dirty(config) and not allow_dirty_config:
        raise ValueError(
            "Config directory has local changes. Use --allow-dirty-config "
            "to allow commit creation with uncommitted changes."
        )

    if verbose:
        logger.info("Repository root: %s", repo_root)
        logger.info("Configuration directory: %s", config)
        logger.info("Output directory: %s", output)

    helmfile_path = config / "releases.yaml"
    helmfile_data = load_helmfile(helmfile_path) if helmfile_path.exists() else None
    if verbose and helmfile_data is not None:
        count = len(helmfile_data.releases)
        logger.info("Loaded releases.yaml: %d release%s", count, plural(count))

    handlers = [
        HelmConfigHandler(),
        WebsiteConfigHandler(),
        SimpleConfigHandler(),
        CopyConfigHandler(),
        PublicRepoConfigHandler(),
    ]
    handlers = load_configs(
        config,
        handlers,
        extra_variables=extra_variables,
        default_namespace=namespace,
        default_image=image if namespace is not None else None,
    )
    handlers = resolve_configs(handlers, helmfile_data)

    if verbose:
        count = sum(1 for handler in handlers for _ in handler.iter_configs())
        logger.info("Loaded %d app configuration%s", count, plural(count))

    images = load_images(config)
    system_roots_before: set[str] = set()
    if namespace is None:
        system_roots_before = _load_system_owner_roots(output)
        owned_namespaces = load_owned_namespaces(
            config, exclude_owner_files={"system.toml"}
        ) | _load_non_system_owned_namespaces(output)
        _clear_output_roots(output, system_roots_before - owned_namespaces)
    else:
        owned_namespaces = load_owned_namespaces(config) | load_owned_namespaces(output)
        owned_namespaces.discard(namespace)
        _clear_output_roots(output, {namespace})
    if verbose and owned_namespaces:
        count = len(owned_namespaces)
        logger.info(
            "Loaded %d owned namespace%s: %s",
            count,
            plural(count),
            ", ".join(sorted(owned_namespaces)),
        )

    written_paths = generate_manifests(
        handlers=handlers,
        output_dir=output,
        repo_root=repo_root,
        images=images,
        verbose=verbose,
        owned_namespaces=owned_namespaces,
        managed_namespaces={namespace} if namespace is not None else None,
        cleanup=False,
    )

    written_roots = _output_roots(output, written_paths)
    if namespace is not None:
        cluster_paths = _cluster_output_paths(output, written_paths)
        if cluster_paths:
            details = "\n  ".join(str(path) for path in cluster_paths)
            raise ValueError(
                "--namespace mode cannot generate cluster-scoped manifests:\n  "
                f"{details}"
            )
        owner_path = _write_namespace_owner(output, namespace)
        written_paths.add(owner_path)
        commit_roots = {namespace}
        commit_paths = {output / namespace, owner_path}
    else:
        owner_path = _write_system_owner(output, written_roots)
        written_paths.add(owner_path)
        commit_roots = (system_roots_before - owned_namespaces) | written_roots
        commit_paths = {output / root for root in commit_roots}
        commit_paths.add(owner_path)

    config_commit = get_git_commit(config) if is_git_checkout(config) else None
    result = _collect_generation_result(
        output, written_paths, config_commit, commit_roots
    )

    if create_commit:
        if config_commit is None:
            config_commit = get_git_commit(config)
        config_subject = get_git_commit_subject(config)
        config_remote = get_git_tracked_remote(config)
        create_manifest_commit(
            output,
            __version__,
            config_remote,
            config_commit,
            config_subject,
            written_paths,
            commit_paths,
        )

    return result


def _load_api_variables(
    repo_root: Path,
    vars_from: Path | None,
    vars: Mapping[str, TemplateValue] | None,
) -> dict[str, TemplateValue] | None:
    """Load and validate extra variables supplied through the API."""
    extra_variables = (
        load_extra_variables(repo_root / vars_from) if vars_from is not None else {}
    )

    if vars is not None:
        if not isinstance(vars, Mapping):
            raise ValueError("generate(vars=...) must be a mapping")

        api_variables: dict[str, TemplateValue] = {}
        for key, value in vars.items():
            if not isinstance(key, str):
                raise ValueError(
                    "Variable keys passed to generate(vars=...) must be strings"
                )
            if not isinstance(value, str | int | float | bool):
                raise ValueError(
                    f"Variable '{key}' passed to generate(vars=...) must be a "
                    "string, number, or boolean"
                )
            api_variables[key] = value

        overlap = sorted(set(extra_variables) & set(api_variables))
        if overlap:
            names = ", ".join(repr(name) for name in overlap)
            suffix = "s" if len(overlap) != 1 else ""
            raise ValueError(
                f"Variable{suffix} {names} passed via both vars_from and "
                "generate(vars=...)"
            )
        extra_variables = {**extra_variables, **api_variables}

    return extra_variables or None


def _collect_generation_result(
    output: Path,
    written_paths: set[Path],
    config_commit: str | None,
    managed_roots: set[str] | None = None,
) -> GenerationResult:
    """Annotate changed manifests and return object-level git changes."""
    if not is_git_checkout(output):
        return GenerationResult(written_paths=written_paths)

    changes = get_git_manifest_changes(output)
    if managed_roots is not None:
        changes = _filter_manifest_changes(output, changes, managed_roots)
    if changes.modified:
        _restore_deploy_id_only_changes(changes.modified)
        changes = get_git_manifest_changes(output)
        if managed_roots is not None:
            changes = _filter_manifest_changes(output, changes, managed_roots)

    deploy_id = _make_deploy_id(__version__, config_commit) if config_commit else None
    if deploy_id is not None and changes.added_or_modified:
        _annotate_manifest_files(changes.added_or_modified, deploy_id)
        changes = get_git_manifest_changes(output)
        if managed_roots is not None:
            changes = _filter_manifest_changes(output, changes, managed_roots)

    return GenerationResult(
        written_paths=written_paths,
        created_or_modified=_object_refs_from_paths(changes.added_or_modified),
        removed=_object_refs_from_deleted_paths(changes.deleted),
        deploy_id=deploy_id,
    )


def _make_deploy_id(version: str, config_commit: str) -> str:
    """Return a deterministic 64-bit deploy id as 16 hex characters."""
    return hashlib.sha256(f"{version}\0{config_commit}".encode()).hexdigest()[:16]


def _restore_deploy_id_only_changes(paths: set[Path]) -> None:
    """Restore modified files whose only semantic diff is the deploy-id annotation."""
    for path in sorted(paths):
        head_content = get_git_head_file(path)
        working_content = path.read_text()
        if _manifests_equal_ignoring_deploy_id(head_content.decode(), working_content):
            path.write_bytes(head_content)


def _manifests_equal_ignoring_deploy_id(left: str, right: str) -> bool:
    """Return whether two manifest streams match aside from deploy-id annotations."""
    return _without_deploy_id(load_all_yaml(left)) == _without_deploy_id(
        load_all_yaml(right)
    )


def _without_deploy_id(documents: list[Any]) -> list[Any]:
    for doc in documents:
        if not isinstance(doc, dict) or not doc.get("kind"):
            continue
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            continue
        annotations = metadata.get("annotations")
        if annotations is None:
            metadata.pop("annotations", None)
            continue
        if not isinstance(annotations, dict):
            continue
        annotations.pop(DEPLOY_ID_ANNOTATION, None)
        if not annotations:
            del metadata["annotations"]
    return documents


def _annotate_manifest_files(paths: set[Path], deploy_id: str) -> None:
    for path in sorted(paths):
        text = path.read_text()
        leading_comments = _leading_comments(text)
        documents = load_all_yaml(text)
        changed = False
        for doc in documents:
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            metadata = doc.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                raise TypeError(f"metadata is not a dict in {path}")
            annotations = metadata.setdefault("annotations", {})
            if annotations is None:
                annotations = {}
                metadata["annotations"] = annotations
            if not isinstance(annotations, dict):
                raise TypeError(f"metadata.annotations is not a dict in {path}")
            if annotations.get(DEPLOY_ID_ANNOTATION) != deploy_id:
                annotations[DEPLOY_ID_ANNOTATION] = deploy_id
                changed = True

        if changed:
            with open(path, "w") as f:
                f.write(leading_comments)
                for index, doc in enumerate(documents):
                    if index:
                        f.write("---\n")
                    dump_yaml(doc, f)


def _leading_comments(text: str) -> str:
    comments: list[str] = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        comments.append(line)
    return "".join(comments)


def _object_refs_from_paths(paths: set[Path]) -> set[KubernetesObjectRef]:
    refs: set[KubernetesObjectRef] = set()
    for path in paths:
        refs.update(_object_refs_from_content(path.read_text()))
    return refs


def _object_refs_from_deleted_paths(paths: set[Path]) -> set[KubernetesObjectRef]:
    refs: set[KubernetesObjectRef] = set()
    for path in paths:
        refs.update(_object_refs_from_content(get_git_head_file(path).decode()))
    return refs


def _object_refs_from_content(content: str) -> set[KubernetesObjectRef]:
    refs: set[KubernetesObjectRef] = set()
    for doc in load_all_yaml(content):
        ref = _object_ref_from_doc(doc)
        if ref is not None:
            refs.add(ref)
    return refs


def _object_ref_from_doc(doc: Any) -> KubernetesObjectRef | None:
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    metadata = doc.get("metadata") or {}
    if not isinstance(kind, str) or not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(name, str):
        return None
    if namespace is not None and not isinstance(namespace, str):
        namespace = None
    return KubernetesObjectRef(kind=kind, namespace=namespace, name=name)


def _cluster_output_paths(output: Path, paths: set[Path]) -> list[Path]:
    """Return generated paths that landed in the output cluster directory."""
    cluster_paths: list[Path] = []
    for path in paths:
        try:
            parts = path.relative_to(output).parts
        except ValueError:
            continue
        if len(parts) > 1 and parts[0] == "cluster":
            cluster_paths.append(path)
    return sorted(cluster_paths)


def _write_namespace_owner(output: Path, namespace: str) -> Path:
    """Write this builder's namespace owner declaration."""
    owner_dir = output / "owners"
    owner_dir.mkdir(parents=True, exist_ok=True)
    owner_path = owner_dir / f"{namespace}.toml"
    owner_path.write_text(f"owned = {json.dumps(namespace)}\n")
    return owner_path


def _write_system_owner(output: Path, roots: set[str]) -> Path:
    """Write the output roots owned by full system generation."""
    owner_dir = output / "owners"
    owner_dir.mkdir(parents=True, exist_ok=True)
    owner_path = owner_dir / "system.toml"
    owned = json.dumps(sorted(roots))
    owner_path.write_text(f"owned = {owned}\n")
    return owner_path


def _load_system_owner_roots(output: Path) -> set[str]:
    """Return the output roots owned by the previous system run."""
    owner_path = output / "owners" / "system.toml"
    if not owner_path.exists():
        return set()

    data = load_toml_file(owner_path)
    owned = data.get("owned", [])
    if isinstance(owned, str):
        owned = [owned]
    if not isinstance(owned, list) or not all(isinstance(root, str) for root in owned):
        raise ValueError(f"'owned' must be a string or list of strings in {owner_path}")
    return {_validate_output_root(root, owner_path) for root in owned}


def _load_non_system_owned_namespaces(output: Path) -> set[str]:
    """Load namespace owners without treating system-owned roots as external."""
    return load_owned_namespaces(output, exclude_owner_files={"system.toml"})


def _clear_output_roots(output: Path, roots: set[str]) -> None:
    """Delete all existing content in the owned output roots."""
    for root in sorted(roots):
        _validate_output_root(root, output / "owners" / "system.toml")
        root_path = output / root
        if not root_path.exists():
            continue
        if not root_path.is_dir() or root_path.is_symlink():
            raise ValueError(f"Owned output root is not a directory: {root_path}")
        for child in sorted(root_path.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            logger.debug("Deleted owned output path before generation: %s", child)


def _output_roots(output: Path, paths: set[Path]) -> set[str]:
    """Return top-level output roots touched by generated manifest paths."""
    roots: set[str] = set()
    for path in paths:
        try:
            rel_parts = path.relative_to(output).parts
        except ValueError:
            continue
        if not rel_parts or rel_parts[0] == "owners":
            continue
        roots.add(_validate_output_root(rel_parts[0], path))
    return roots


def _validate_output_root(root: str, source: Path) -> str:
    """Validate that an owned output root is a single safe path component."""
    if (
        not root
        or root in {".", "..", "owners"}
        or root.startswith(".")
        or Path(root).parts != (root,)
    ):
        raise ValueError(f"Invalid output root {root!r} in {source}")
    return root


def _filter_manifest_changes(
    output: Path, changes: GitManifestChanges, managed_roots: set[str]
) -> GitManifestChanges:
    """Limit git change reporting to the roots owned by this invocation."""
    return GitManifestChanges(
        added=_filter_paths_to_roots(output, changes.added, managed_roots),
        modified=_filter_paths_to_roots(output, changes.modified, managed_roots),
        deleted=_filter_paths_to_roots(output, changes.deleted, managed_roots),
    )


def _filter_paths_to_roots(
    output: Path, paths: set[Path], managed_roots: set[str]
) -> set[Path]:
    return {path for path in paths if _path_output_root(output, path) in managed_roots}


def _path_output_root(output: Path, path: Path) -> str | None:
    try:
        rel_parts = path.resolve().relative_to(output.resolve()).parts
    except ValueError:
        return None
    return rel_parts[0] if rel_parts else None
