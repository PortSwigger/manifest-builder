# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests for YAML serialization."""

import pytest
import yaml

from manifest_builder.output import dump_all_yaml


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
