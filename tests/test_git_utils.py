# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for git utilities."""

import logging
from pathlib import Path
from typing import cast

import pytest
from conftest import init_test_repo
from dulwich import porcelain
from dulwich.objects import Commit
from dulwich.repo import Repo

from manifest_builder.git_utils import (
    create_manifest_commit,
    get_git_commit_subject,
    get_git_tracked_remote,
    is_git_checkout,
    is_git_dirty,
)


def _commit_all(path: Path, message: bytes = b"commit") -> bytes:
    """Commit all changes in a temporary Dulwich repository."""
    porcelain.add(path)
    return porcelain.commit(
        path,
        message=message,
        author=b"Test User <test@example.com>",
        committer=b"Test User <test@example.com>",
    )


def test_is_git_checkout_returns_false_for_non_checkout(tmp_path: Path) -> None:
    """Non-git directories are not valid commit output targets."""
    assert not is_git_checkout(tmp_path)


def test_is_git_checkout_accepts_nested_checkout_path(tmp_path: Path) -> None:
    """Output directories below a checkout root are valid commit targets."""
    init_test_repo(tmp_path)
    output_dir = tmp_path / "manifests" / "platform-dev"

    assert is_git_checkout(output_dir)


def test_is_git_dirty_accepts_tracked_subdirectory(tmp_path: Path) -> None:
    """A clean tracked config subdirectory can be checked from inside a repo."""
    init_test_repo(tmp_path)
    config_dir = tmp_path / ".deploy"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text("name = 'example'\n")
    _commit_all(tmp_path)

    assert not is_git_dirty(config_dir)


def test_get_git_tracked_remote_returns_current_branch_remote_url(
    tmp_path: Path,
) -> None:
    """The tracked remote URL is resolved from the current branch config."""
    repo = init_test_repo(tmp_path)
    config = repo.get_config()
    config.set((b"remote", b"origin"), b"url", b"https://example.com/config.git")
    config.set((b"branch", b"master"), b"remote", b"origin")
    config.set((b"branch", b"master"), b"merge", b"refs/heads/main")
    config.write_to_path()
    repo.close()

    assert get_git_tracked_remote(tmp_path) == "https://example.com/config.git"


def test_get_git_tracked_remote_uses_only_remote_for_detached_head(
    tmp_path: Path,
) -> None:
    """Detached HEAD checkouts use the sole configured remote URL."""
    repo = init_test_repo(tmp_path)
    config = repo.get_config()
    config.set((b"remote", b"upstream"), b"url", b"https://example.com/config.git")
    config.write_to_path()
    repo.close()
    (tmp_path / "config.toml").write_text("name = 'example'\n")
    commit = _commit_all(tmp_path)
    porcelain.update_head(tmp_path, commit, detached=True)

    assert get_git_tracked_remote(tmp_path) == "https://example.com/config.git"


def test_get_git_tracked_remote_prefers_origin_for_detached_head(
    tmp_path: Path,
) -> None:
    """Detached HEAD checkouts with several remotes prefer origin."""
    repo = init_test_repo(tmp_path)
    config = repo.get_config()
    config.set((b"remote", b"fork"), b"url", b"https://example.com/fork.git")
    config.set((b"remote", b"origin"), b"url", b"https://example.com/config.git")
    config.write_to_path()
    repo.close()
    (tmp_path / "config.toml").write_text("name = 'example'\n")
    commit = _commit_all(tmp_path)
    porcelain.update_head(tmp_path, commit, detached=True)

    assert get_git_tracked_remote(tmp_path) == "https://example.com/config.git"


def test_get_git_tracked_remote_fails_when_no_remotes_are_configured(
    tmp_path: Path,
) -> None:
    """A checkout without configured remotes fails with an explicit error."""
    init_test_repo(tmp_path)

    with pytest.raises(RuntimeError, match="No git remotes are configured"):
        get_git_tracked_remote(tmp_path)


def test_get_git_commit_subject_returns_first_message_line(tmp_path: Path) -> None:
    """The config commit subject comes from the first line of HEAD."""
    init_test_repo(tmp_path)
    (tmp_path / "config.toml").write_text("name = 'example'\n")
    _commit_all(
        tmp_path,
        message=(
            b"Update production config\n"
            b"\n"
            b"Switch the image tag for the production deployment."
        ),
    )

    assert get_git_commit_subject(tmp_path) == "Update production config"


def test_create_manifest_commit_stages_full_output_by_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Commit creation stages the full output checkout when no scope is given."""
    init_test_repo(tmp_path)
    manifest = tmp_path / "surreal3" / "namespace-surreal3.yaml"
    manifest.parent.mkdir()
    manifest.write_text("apiVersion: v1\nkind: Namespace\n")
    first_commit = _commit_all(tmp_path)

    manifest.write_text("apiVersion: v1\nkind: Namespace\nmetadata: {}\n")
    with caplog.at_level(logging.INFO, logger="manifest_builder.git_utils"):
        create_manifest_commit(
            output_dir=tmp_path,
            version="1.2.3",
            config_remote="https://example.com/config.git",
            config_commit="abc123",
            config_subject="Update production config",
            generated_files={manifest},
        )

    assert porcelain.status(tmp_path).unstaged == []
    assert porcelain.status(tmp_path).untracked == []
    with Repo.discover(tmp_path) as repo:
        assert repo.head() != first_commit
        commit = cast(Commit, repo[repo.head()])
    assert commit.message == (
        b"Generated from: Update production config\n"
        b"\n"
        b"Config remote: https://example.com/config.git\n"
        b"Config commit: abc123\n"
        b"Tool version: 1.2.3"
    )
    assert f"Created manifest commit in {tmp_path}" in caplog.messages


def test_create_manifest_commit_records_the_plugin_source(tmp_path: Path) -> None:
    """Plugins loaded from elsewhere are named in the commit message."""
    init_test_repo(tmp_path)
    manifest = tmp_path / "surreal3" / "namespace-surreal3.yaml"
    manifest.parent.mkdir()
    manifest.write_text("apiVersion: v1\nkind: Namespace\n")

    create_manifest_commit(
        output_dir=tmp_path,
        version="1.2.3",
        config_remote="https://example.com/config.git",
        config_commit="abc123",
        config_subject="Update production config",
        generated_files={manifest},
        plugins_source="https://example.com/plugins.git@def456",
    )

    with Repo.discover(tmp_path) as repo:
        commit = cast(Commit, repo[repo.head()])
    assert commit.message == (
        b"Generated from: Update production config\n"
        b"\n"
        b"Config remote: https://example.com/config.git\n"
        b"Config commit: abc123\n"
        b"Plugins from: https://example.com/plugins.git@def456\n"
        b"Tool version: 1.2.3"
    )


def test_create_manifest_commit_uses_parent_checkout_for_nested_output(
    tmp_path: Path,
) -> None:
    """Commit creation stages nested output paths from their parent checkout."""
    init_test_repo(tmp_path)
    output_dir = tmp_path / "manifests" / "platform-dev"
    stale = output_dir / "default" / "configmap-stale.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("apiVersion: v1\nkind: ConfigMap\n")
    first_commit = _commit_all(tmp_path)

    stale.unlink()
    fresh = output_dir / "default" / "configmap-fresh.yaml"
    fresh.write_text("apiVersion: v1\nkind: ConfigMap\n")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep me out of the manifest commit\n")

    create_manifest_commit(
        output_dir=output_dir,
        version="1.2.3",
        config_remote="https://example.com/config.git",
        config_commit="abc123",
        config_subject="Update production config",
        generated_files={fresh},
        stage_paths={fresh.parent},
    )

    status = porcelain.status(tmp_path)
    assert status.staged == {"add": [], "delete": [], "modify": []}
    assert status.unstaged == []
    assert status.untracked == [b"notes.txt"]
    with Repo.discover(output_dir) as repo:
        assert repo.head() != first_commit
        commit = cast(Commit, repo[repo.head()])
    assert fresh.exists()
    assert not stale.exists()
    assert b"Generated from: Update production config" in commit.message
    assert b"Output path: manifests/platform-dev\n" in commit.message


def test_create_manifest_commit_noop_ignores_untracked_parent_files(
    tmp_path: Path,
) -> None:
    """Untracked files outside a nested output directory do not force a commit."""
    init_test_repo(tmp_path)
    output_dir = tmp_path / "manifests" / "platform-dev"
    manifest = output_dir / "default" / "configmap-fresh.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("apiVersion: v1\nkind: ConfigMap\n")
    first_commit = _commit_all(tmp_path)
    (tmp_path / "notes.txt").write_text("keep me out of the manifest commit\n")

    create_manifest_commit(
        output_dir=output_dir,
        version="1.2.3",
        config_remote="https://example.com/config.git",
        config_commit="abc123",
        config_subject="Update production config",
        generated_files={manifest},
    )

    with Repo.discover(output_dir) as repo:
        assert repo.head() == first_commit


def test_create_manifest_commit_stages_only_requested_paths(
    tmp_path: Path,
) -> None:
    """Scoped commits leave pre-existing deletions outside the scope unstaged."""
    init_test_repo(tmp_path)
    target = tmp_path / "team-a" / "deployment-app.yaml"
    protected = tmp_path / "cluster" / "clusterrole-system:metrics-server.yaml"
    target.parent.mkdir()
    protected.parent.mkdir()
    target.write_text("apiVersion: apps/v1\nkind: Deployment\n")
    protected.write_text(
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\n"
    )
    first_commit = _commit_all(tmp_path)

    protected.unlink()
    target.write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata: {}\n")

    create_manifest_commit(
        output_dir=tmp_path,
        version="1.2.3",
        config_remote="https://example.com/config.git",
        config_commit="abc123",
        config_subject="Update production config",
        generated_files={target},
        stage_paths={tmp_path / "team-a"},
    )

    with Repo.discover(tmp_path) as repo:
        assert repo.head() != first_commit
    assert porcelain.status(tmp_path).unstaged == [
        b"cluster/clusterrole-system:metrics-server.yaml"
    ]
