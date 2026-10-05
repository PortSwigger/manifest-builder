# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for YAML serialization."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from manifest_builder.output import (
    ARGO_SYNC_OPTIONS_ANNOTATION,
    LAST_APPLIED_ANNOTATION,
    MANIFEST_ID_ANNOTATION,
    dump_all_yaml,
    reset_written_paths,
    write_documents,
    write_manifests,
)


@pytest.mark.parametrize(
    "value",
    [
        "032445865269",  # go-yaml reads the bare form as a float
        "016624044925",
        "031692804905",
        "132827254700",  # PyYAML reads the bare form as an int
        "0123",
        "1.0",
        "1e5",
        "-7",
    ],
)
def test_number_like_strings_are_quoted(value: str) -> None:
    assert f"a: '{value}'" in dump_all_yaml([{"a": value}])


@pytest.mark.parametrize("value", ["v1.0", "032445865269-a", "abc", "1:30", ""])
def test_other_strings_are_left_alone(value: str) -> None:
    assert yaml.safe_load(dump_all_yaml([{"a": value}]))["a"] == value


def test_multiline_strings_still_use_a_block_scalar() -> None:
    assert "|" in dump_all_yaml([{"a": "one\ntwo\n"}])


def test_numbers_are_not_turned_into_strings() -> None:
    assert yaml.safe_load(dump_all_yaml([{"a": 132827254700}]))["a"] == 132827254700


@pytest.fixture
def crd() -> dict:
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.com"},
        "spec": {
            "group": "example.com",
            "names": {"kind": "Widget", "plural": "widgets"},
            "scope": "Namespaced",
            "versions": [
                {
                    "name": "v1",
                    "served": True,
                    "storage": True,
                    "schema": {
                        "openAPIV3Schema": {"type": "object", "description": ""}
                    },
                }
            ],
        },
    }


def _set_description(crd: dict, value: str) -> None:
    crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["description"] = value


def _write_and_read(doc: dict, tmp_path: Path) -> dict:
    """Write a copy of ``doc`` and read it back without its manifest-id.

    Callers write the same object again to check the result is stable, so each
    write starts a fresh run.
    """
    reset_written_paths()
    paths = write_documents([copy.deepcopy(doc)], tmp_path, "default")
    written = yaml.safe_load(next(iter(paths)).read_text())
    annotations = written["metadata"]["annotations"]
    del annotations[MANIFEST_ID_ANNOTATION]
    if not annotations:
        del written["metadata"]["annotations"]
    return written


def _configmap(name: str, value: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "data": {"key": value},
    }


def _manifest_id(path: Path) -> str:
    return yaml.safe_load(path.read_text())["metadata"]["annotations"][
        MANIFEST_ID_ANNOTATION
    ]


def test_written_objects_carry_a_manifest_id_of_their_content(tmp_path: Path) -> None:
    first = write_documents([_configmap("a", "one")], tmp_path / "first", "default")
    again = write_documents([_configmap("a", "one")], tmp_path / "again", "default")
    other = write_documents([_configmap("a", "two")], tmp_path / "other", "default")

    [first_id] = [_manifest_id(path) for path in first]
    [again_id] = [_manifest_id(path) for path in again]
    [other_id] = [_manifest_id(path) for path in other]
    assert first_id == again_id
    assert first_id != other_id
    assert len(first_id) == 16


def test_an_empty_annotations_key_is_stamped_where_it_is(tmp_path: Path) -> None:
    """A chart's ``annotations:`` with nothing under it keeps its place."""
    doc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "hubble-relay",
            "annotations": None,
            "labels": {"k8s-app": "hubble-relay"},
        },
    }

    [path] = write_documents([doc], tmp_path, "kube-system")

    keys = [line for line in path.read_text().splitlines() if line.startswith("  ")]
    assert keys[1:4] == [
        "  annotations:",
        f"    {MANIFEST_ID_ANNOTATION}: {_manifest_id(path)}",
        "  labels:",
    ]


@pytest.mark.parametrize("delta, expected", [(-1, False), (0, True), (1, True)])
def test_crd_annotation_budget_boundary(
    crd: dict, tmp_path: Path, delta: int, expected: bool
) -> None:
    # Include the last-applied key, its JSON value and terminating newline.
    crd["metadata"]["annotations"] = {}
    overhead = (
        len(json.dumps(crd, separators=(",", ":")).encode())
        + len(LAST_APPLIED_ANNOTATION)
        + 1
    )
    _set_description(crd, "x" * (240 * 1024 - overhead + delta))
    result = _write_and_read(crd, tmp_path)
    assert (
        ARGO_SYNC_OPTIONS_ANNOTATION in result["metadata"].get("annotations", {})
    ) is expected


@pytest.mark.parametrize("annotations", [None, {}, {"example.com/note": "keep"}])
def test_large_crd_gets_ssa(
    crd: dict, tmp_path: Path, annotations: dict | None
) -> None:
    crd["metadata"]["annotations"] = annotations
    _set_description(crd, "x" * 262144)
    result = _write_and_read(crd, tmp_path)
    assert (
        result["metadata"]["annotations"][ARGO_SYNC_OPTIONS_ANNOTATION]
        == "ServerSideApply=true"
    )
    if annotations and "example.com/note" in annotations:
        assert result["metadata"]["annotations"]["example.com/note"] == "keep"


@pytest.mark.parametrize(
    "options",
    [
        "Prune=false",
        "Prune=false,ServerSideApply=false",
        "Prune=false, ServerSideApply=true,ServerSideApply=false",
    ],
)
def test_large_crd_preserves_other_options_and_is_idempotent(
    crd: dict, tmp_path: Path, options: str
) -> None:
    crd["metadata"]["annotations"] = {ARGO_SYNC_OPTIONS_ANNOTATION: options}
    _set_description(crd, "x" * 262144)
    result = _write_and_read(crd, tmp_path)
    assert (
        result["metadata"]["annotations"][ARGO_SYNC_OPTIONS_ANNOTATION]
        == "Prune=false,ServerSideApply=true"
    )
    assert _write_and_read(result, tmp_path) == result


def test_existing_annotations_count_in_addition_to_json(
    crd: dict, tmp_path: Path
) -> None:
    crd["metadata"]["annotations"] = {"example.com/note": "x" * (125 * 1024)}
    assert len(json.dumps(crd).encode()) < 240 * 1024
    assert (
        _write_and_read(crd, tmp_path)["metadata"]["annotations"][
            ARGO_SYNC_OPTIONS_ANNOTATION
        ]
        == "ServerSideApply=true"
    )


@pytest.mark.parametrize("text", ["界" * 90000, "<>&" * 20000, '\\"\n' * 60000])
def test_crd_size_accounts_for_unicode_and_json_escaping(
    crd: dict, tmp_path: Path, text: str
) -> None:
    _set_description(crd, text)
    assert (
        _write_and_read(crd, tmp_path)["metadata"]["annotations"][
            ARGO_SYNC_OPTIONS_ANNOTATION
        ]
        == "ServerSideApply=true"
    )


def test_small_crd_keeps_explicit_options_and_ignores_previous_last_applied(
    crd: dict, tmp_path: Path
) -> None:
    crd["metadata"]["annotations"] = {
        LAST_APPLIED_ANNOTATION: "x" * 250000,
        ARGO_SYNC_OPTIONS_ANNOTATION: "ServerSideApply=false,Prune=false",
    }
    assert _write_and_read(crd, tmp_path) == crd


def test_small_crd_does_not_gain_annotations(crd: dict, tmp_path: Path) -> None:
    assert "annotations" not in _write_and_read(crd, tmp_path)["metadata"]


@pytest.mark.parametrize(
    "kind, api_version",
    [("ConfigMap", "v1"), ("CustomResourceDefinition", "example.com/v1")],
)
def test_large_non_crd_is_unchanged(
    tmp_path: Path, kind: str, api_version: str
) -> None:
    doc = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": "test"},
        "data": {"payload": "x" * 262144},
    }
    assert _write_and_read(doc, tmp_path) == doc


def test_yaml_list_crd_is_annotated_after_helm_metadata_is_stripped(
    crd: dict, tmp_path: Path
) -> None:
    _set_description(crd, "x" * 262144)
    crd["metadata"]["annotations"] = {"helm.sh/resource-policy": "keep"}
    content = dump_all_yaml([{"apiVersion": "v1", "kind": "List", "items": [crd]}])
    paths = write_manifests(content, tmp_path, "default")
    path = next(iter(paths))
    assert path.parent.name == "cluster"
    annotations = yaml.safe_load(path.read_text())["metadata"]["annotations"]
    assert annotations[ARGO_SYNC_OPTIONS_ANNOTATION] == "ServerSideApply=true"
    assert set(annotations) == {ARGO_SYNC_OPTIONS_ANNOTATION, MANIFEST_ID_ANNOTATION}


def test_stripped_helm_annotations_do_not_trigger_ssa(
    crd: dict, tmp_path: Path
) -> None:
    crd["metadata"]["annotations"] = {"helm.sh/example": "x" * 262144}
    assert "annotations" not in _write_and_read(crd, tmp_path)["metadata"]


def _role(api_version: str, name: str = "agent") -> dict:
    return {
        "apiVersion": api_version,
        "kind": "Role",
        "metadata": {"name": name, "namespace": "teleport"},
    }


def test_same_kind_and_name_in_different_api_groups_is_an_error(
    tmp_path: Path,
) -> None:
    documents = [
        _role("rbac.authorization.k8s.io/v1"),
        _role("iam.aws.m.upbound.io/v1beta1"),
    ]

    with pytest.raises(ValueError, match=r"role-agent\.yaml.*iam\.aws\.m"):
        write_documents(documents, tmp_path, "teleport")


def test_an_identical_duplicate_is_an_error_too(tmp_path: Path) -> None:
    documents = [_role("rbac.authorization.k8s.io/v1")] * 2

    with pytest.raises(ValueError, match=r"role-agent\.yaml would be written twice"):
        write_documents(documents, tmp_path, "teleport")


def test_different_names_do_not_collide(tmp_path: Path) -> None:
    documents = [
        _role("rbac.authorization.k8s.io/v1"),
        _role("iam.aws.m.upbound.io/v1beta1", name="agent-irsa"),
    ]

    assert len(write_documents(documents, tmp_path, "teleport")) == 2


def test_a_collision_across_separate_calls_is_an_error(tmp_path: Path) -> None:
    """A helm release writes its chart and then its extra resources separately."""
    write_documents([_role("rbac.authorization.k8s.io/v1")], tmp_path, "teleport")

    with pytest.raises(ValueError, match=r"role-agent\.yaml"):
        write_documents([_role("iam.aws.m.upbound.io/v1beta1")], tmp_path, "teleport")
