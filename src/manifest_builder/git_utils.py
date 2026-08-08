# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Git utilities for manifest generation and versioning."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, Commit, Tree
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


def get_git_manifest_changes(path: Path) -> GitManifestChanges:
    """Return changed YAML files below ``path`` using Dulwich status."""
    try:
        repo = Repo.discover(path)
        try:
            repo_root = Path(repo.path).resolve()
            output_root = path.resolve()
            status = porcelain.status(repo)
            changes = GitManifestChanges()

            for raw_path in status.staged.get("add", []):
                _add_status_path(changes.added, repo_root, output_root, raw_path)
            for raw_path in status.staged.get("modify", []):
                _add_status_path(changes.modified, repo_root, output_root, raw_path)
            for raw_path in status.staged.get("delete", []):
                _add_status_path(changes.deleted, repo_root, output_root, raw_path)

            for raw_path in status.untracked:
                _add_status_path(changes.added, repo_root, output_root, raw_path)

            for raw_path in status.unstaged:
                absolute_path = repo_root / raw_path.decode("utf-8")
                if absolute_path.exists():
                    _add_status_path(changes.modified, repo_root, output_root, raw_path)
                else:
                    _add_status_path(changes.deleted, repo_root, output_root, raw_path)

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


def _staged_status_has_path_under_any(
    status: porcelain.GitStatus, repo_root: Path, roots: set[Path]
) -> bool:
    """Return whether staged status contains a path below any root."""
    for paths in status.staged.values():
        for raw_path in paths:
            absolute_path = repo_root / raw_path.decode("utf-8")
            for root in roots:
                try:
                    absolute_path.relative_to(root)
                except ValueError:
                    continue
                return True
    return False


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
            output_root = output_dir.resolve()
            roots = {output_root}
            if stage_paths is None:
                pathspecs = [str(_relative_to_repo(repo, output_dir))]
            else:
                roots = {path.resolve() for path in stage_paths}
                pathspecs = _relative_stage_paths(repo, stage_paths)

            porcelain.add(repo, paths=pathspecs)
            status = porcelain.status(repo)
            if not _staged_status_has_path_under_any(status, repo_root, roots):
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


def _relative_stage_paths(repo: Repo, stage_paths: set[Path]) -> list[str]:
    relative_paths: list[str] = []
    for path in sorted(stage_paths):
        try:
            relative_paths.append(str(_relative_to_repo(repo, path)))
        except ValueError:
            relative_paths.append(str(path))
    return relative_paths
