# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Git utilities for manifest generation and versioning."""

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from dulwich import porcelain
from dulwich.diff_tree import CHANGE_ADD, CHANGE_DELETE, TreeChange, tree_changes
from dulwich.errors import NotGitRepository
from dulwich.ignore import IgnoreFilterManager
from dulwich.index import (
    ConflictedIndexEntry,
    blob_from_path_and_stat,
    cleanup_mode,
    commit_index,
    commit_tree,
)
from dulwich.object_store import (
    BaseObjectStore,
    MemoryObjectStore,
    OverlayObjectStore,
)
from dulwich.objects import Blob, Commit, ObjectID, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo

logger = logging.getLogger(__name__)


@dataclass
class GitManifestChanges:
    """Manifest file changes reported by git."""

    added: set[Path] = field(default_factory=set)
    modified: set[Path] = field(default_factory=set)
    deleted: set[Path] = field(default_factory=set)

    @property
    def added_or_modified(self) -> set[Path]:
        """Return files that exist in the working tree after generation."""
        return self.added | self.modified


class _GitConfig(Protocol):
    """Subset of the Dulwich config API used by remote resolution."""

    def sections(self) -> Iterable[tuple[bytes, ...]]: ...

    def get(self, section: tuple[bytes, ...], name: bytes) -> bytes: ...


def get_git_commit(path: Path) -> str:
    """
    Get the current git commit hash of a directory.

    Args:
        path: Directory to get commit hash for

    Returns:
        Full commit hash (40 characters)

    Raises:
        RuntimeError: If not a git repository or git operations fail
    """
    try:
        repo = Repo.discover(path)
        with repo:
            return repo.head().decode("ascii")
    except Exception as e:
        raise RuntimeError(f"Failed to get git commit for {path}: {e}") from e


def get_git_commit_subject(path: Path) -> str:
    """
    Get the first line of the current git commit message for a directory.

    Args:
        path: Directory to get commit subject for

    Returns:
        First line of the HEAD commit message

    Raises:
        RuntimeError: If not a git repository or git operations fail
    """
    try:
        repo = Repo.discover(path)
        with repo:
            commit = cast(Commit, repo[repo.head()])
            return commit.message.decode("utf-8", errors="replace").partition("\n")[0]
    except Exception as e:
        raise RuntimeError(f"Failed to get git commit subject for {path}: {e}") from e


def get_git_tracked_remote(path: Path) -> str:
    """
    Get the URL of the remote that identifies the current checkout.

    Args:
        path: Directory to inspect

    Returns:
        URL of the upstream remote for the current branch, or a configured remote

    Raises:
        RuntimeError: If no remote can be resolved
    """
    try:
        repo = Repo.discover(path)
        with repo:
            head_ref = repo.refs.read_ref(cast(Ref, b"HEAD"))
            config = repo.get_config_stack()

            if head_ref is not None and head_ref.startswith(b"ref: refs/heads/"):
                branch_name = head_ref.removeprefix(b"ref: refs/heads/")
                try:
                    remote_name = config.get((b"branch", branch_name), b"remote")
                    remote_url = config.get((b"remote", remote_name), b"url")
                    return remote_url.decode("utf-8")
                except KeyError:
                    pass

            return _get_configured_remote_url(config)
    except Exception as e:
        raise RuntimeError(f"Failed to get git tracked remote for {path}: {e}") from e


def _get_configured_remote_url(config: _GitConfig) -> str:
    """Return the sole configured remote URL, or origin when several exist."""
    remote_names = sorted(
        section[1]
        for section in config.sections()
        if len(section) == 2 and section[0] == b"remote"
    )
    if not remote_names:
        raise RuntimeError("No git remotes are configured for the config checkout")

    remote_name = b"origin" if b"origin" in remote_names else remote_names[0]
    if len(remote_names) > 1 and remote_name != b"origin":
        names = ", ".join(
            name.decode("utf-8", errors="replace") for name in remote_names
        )
        raise RuntimeError(
            "Multiple git remotes are configured for the config checkout, "
            f"but none is named 'origin': {names}"
        )

    remote_url = config.get((b"remote", remote_name), b"url")
    return remote_url.decode("utf-8")


def is_git_checkout(path: Path) -> bool:
    """
    Check whether a path is inside a git checkout.

    Args:
        path: Directory to check

    Returns:
        True if the directory is in a git checkout, False otherwise
    """
    if path.exists() and not path.is_dir():
        return False

    try:
        repo = Repo.discover(_nearest_existing_path(path))
        repo.close()
        return True
    except NotGitRepository:
        return False


def is_git_dirty(path: Path) -> bool:
    """
    Check if a path inside a git checkout has uncommitted changes.

    Args:
        path: Directory to check

    Returns:
        True if there are uncommitted changes, False otherwise

    Raises:
        RuntimeError: If not a git repository or git operations fail
    """
    try:
        repo = Repo.discover(path)
        try:
            return not _status_is_clean(porcelain.status(repo))
        finally:
            repo.close()
    except Exception as e:
        raise RuntimeError(f"Failed to check git status for {path}: {e}") from e


def get_git_manifest_changes(
    path: Path, roots: set[Path] | None = None
) -> GitManifestChanges:
    """Return changed YAML files below ``path``, comparing ``roots`` with HEAD.

    Only files under ``roots`` (``path`` by default) are read: they are hashed
    into a tree held in memory and diffed against HEAD's, so directories
    outside them cost one hash comparison each and the index is left alone.
    """
    try:
        repo = Repo.discover(path)
        try:
            repo_root = Path(repo.path).resolve()
            output_root = path.resolve()
            prefixes = _tree_prefixes(repo_root, roots or {path})
            index = repo.open_index()
            scratch = MemoryObjectStore()
            store = OverlayObjectStore([scratch, repo.object_store], scratch)
            normalizer = repo.get_blob_normalizer()
            entries: dict[bytes, tuple[ObjectID, int]] = {}
            for tree_path, entry in index.iteritems():
                if isinstance(entry, ConflictedIndexEntry) or _under_any(
                    tree_path, prefixes
                ):
                    continue
                entries[tree_path] = (entry.sha, entry.mode)
            for tree_path in _files_on_disk(repo, repo_root, prefixes):
                full_path = os.path.join(os.fsencode(repo_root), tree_path)
                st = os.lstat(full_path)
                blob = normalizer.checkin_normalize(
                    blob_from_path_and_stat(full_path, st), tree_path
                )
                entries[tree_path] = (blob.id, cleanup_mode(st.st_mode))
            tree = commit_tree(
                store, [(p, sha, mode) for p, (sha, mode) in entries.items()]
            )

            changes = GitManifestChanges()
            for change in _changes_under(store, _head_tree(repo), tree, prefixes):
                if change.type == CHANGE_ADD:
                    paths = changes.added
                elif change.type == CHANGE_DELETE:
                    paths = changes.deleted
                else:
                    paths = changes.modified
                _add_status_path(paths, repo_root, output_root, _changed_path(change))
            return changes
        finally:
            repo.close()
    except Exception as e:
        raise RuntimeError(
            f"Failed to inspect git manifest changes in {path}: {e}"
        ) from e


def get_git_head_file(path: Path) -> bytes:
    """Return a file's contents from HEAD."""
    try:
        repo = Repo.discover(path)
        try:
            repo_root = Path(repo.path).resolve()
            relative_path = path.resolve().relative_to(repo_root)
            commit = cast(Commit, repo[repo.head()])
            tree = cast(Tree, repo[commit.tree])
            _mode, sha = tree.lookup_path(
                repo.object_store.__getitem__,
                str(relative_path).encode("utf-8"),
            )
            blob = cast(Blob, repo[sha])
            return blob.data
        finally:
            repo.close()
    except Exception as e:
        raise RuntimeError(f"Failed to read {path} from git HEAD: {e}") from e


def _add_status_path(
    paths: set[Path], repo_root: Path, output_root: Path, raw_path: bytes
) -> None:
    absolute_path = repo_root / raw_path.decode("utf-8")
    if absolute_path.suffix != ".yaml":
        return
    try:
        absolute_path.relative_to(output_root)
    except ValueError:
        return
    paths.add(absolute_path)


def _status_is_clean(status: porcelain.GitStatus) -> bool:
    """Return whether a Dulwich status has no staged, unstaged, or untracked paths."""
    return (
        not status.untracked
        and not status.unstaged
        and all(not paths for paths in status.staged.values())
    )


def _nearest_existing_path(path: Path) -> Path:
    """Return ``path`` or its nearest existing parent."""
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _relative_to_repo(repo: Repo, path: Path) -> Path:
    """Return ``path`` relative to the Dulwich repository working tree."""
    repo_root = Path(repo.path).resolve()
    return path.resolve().relative_to(repo_root)


def _tree_prefixes(repo_root: Path, roots: Iterable[Path]) -> set[bytes]:
    """Return ``roots`` as tree paths; the repository root becomes ``b""``."""
    prefixes: set[bytes] = set()
    for root in roots:
        relative = root.resolve().relative_to(repo_root).as_posix()
        prefixes.add(b"" if relative == "." else relative.encode("utf-8"))
    return prefixes


def _under_any(tree_path: bytes, prefixes: set[bytes]) -> bool:
    return any(
        not prefix or tree_path == prefix or tree_path.startswith(prefix + b"/")
        for prefix in prefixes
    )


def _files_on_disk(repo: Repo, repo_root: Path, prefixes: set[bytes]) -> set[bytes]:
    """Return tree paths of the files below ``prefixes`` git would track."""
    ignore_manager = IgnoreFilterManager.from_repo(repo)
    found: set[bytes] = set()
    for prefix in prefixes:
        top = repo_root / prefix.decode("utf-8")
        if top.is_file() or top.is_symlink():
            candidates: Iterable[Path] = [top]
        elif top.is_dir():
            candidates = _walk_files(top)
        else:
            continue
        for candidate in candidates:
            relative = candidate.relative_to(repo_root).as_posix()
            if not ignore_manager.is_ignored(relative):
                found.add(relative.encode("utf-8"))
    return found


def _walk_files(top: Path) -> Iterable[Path]:
    for directory, subdirectories, files in os.walk(top):
        subdirectories[:] = [name for name in subdirectories if name != ".git"]
        for name in files + subdirectories:
            path = Path(directory) / name
            if name in files or path.is_symlink():
                yield path


def _head_tree(repo: Repo) -> ObjectID | None:
    try:
        return cast(Commit, repo[repo.head()]).tree
    except KeyError:
        return None


def _changes_under(
    store: BaseObjectStore,
    old_tree: ObjectID | None,
    new_tree: ObjectID,
    prefixes: set[bytes],
) -> Iterable[TreeChange]:
    for change in tree_changes(store, old_tree, new_tree):
        if _under_any(_changed_path(change), prefixes):
            yield change


def _changed_path(change: TreeChange) -> bytes:
    entry = change.new if change.type == CHANGE_ADD else change.old
    assert entry is not None and entry.path is not None
    return entry.path


def create_manifest_commit(
    output_dir: Path,
    version: str,
    config_remote: str,
    config_commit: str,
    config_subject: str,
    generated_files: set[Path],
    stage_paths: set[Path] | None = None,
    plugins_source: str | None = None,
) -> None:
    """
    Create a git commit in the output directory.

    Commits generated changes after the caller has reconciled the output tree.

    Args:
        output_dir: Directory to create commit in
        version: Version of manifest-builder
        config_remote: URL of the remote tracked by the config branch
        config_commit: Commit hash of the config directory
        config_subject: First line of the config commit message
        generated_files: Set of file paths that were generated in this run
        stage_paths: Paths under ``output_dir`` to stage. If omitted, the full
            output checkout is staged.
        plugins_source: Where plugins loaded from outside the config directory
            came from, recorded as a ``Plugins from:`` line.

    Raises:
        RuntimeError: If git operations fail
    """
    del generated_files
    try:
        repo = Repo.discover(output_dir)
        try:
            repo_root = Path(repo.path).resolve()
            prefixes = _tree_prefixes(repo_root, stage_paths or {output_dir})
            tracked = {
                tree_path
                for tree_path, _entry in repo.open_index().iteritems()
                if _under_any(tree_path, prefixes)
            }
            repo.get_worktree().stage(
                sorted(tracked | _files_on_disk(repo, repo_root, prefixes))
            )
            tree = commit_index(repo.object_store, repo.open_index())
            if not any(
                _changes_under(repo.object_store, _head_tree(repo), tree, prefixes)
            ):
                logger.info("There is nothing to commit.")
                return

            output_relative = _relative_to_repo(repo, output_dir)
            output_line = (
                f"Output path: {output_relative}\n"
                if output_relative != Path(".")
                else ""
            )
            plugins_line = (
                f"Plugins from: {plugins_source}\n"
                if plugins_source is not None
                else ""
            )
            commit_message = (
                f"Generated from: {config_subject}\n"
                f"\n"
                f"Config remote: {config_remote}\n"
                f"Config commit: {config_commit}\n"
                f"{plugins_line}"
                f"{output_line}"
                f"Tool version: {version}"
            )
            porcelain.commit(repo, message=commit_message.encode())
        finally:
            repo.close()
        logger.info("Created manifest commit in %s", output_dir)
    except Exception as e:
        raise RuntimeError(f"Failed to create git commit in {output_dir}: {e}") from e
