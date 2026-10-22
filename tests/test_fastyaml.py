# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Tests that the native YAML loader and dumper agree with PyYAML."""

import io
import logging
import math
from typing import Any

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from manifest_builder import output
from manifest_builder._fastyaml import Fallback, dump_all, load_all
from manifest_builder.output import (
    YAML_DUMPER,
    YAML_LOADER,
    dump_all_yaml,
    load_all_yaml,
)


def pyyaml_dump_all(docs: list[Any]) -> str:
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


# The native dumper reproduces libyaml's emitter, which PyYAML only uses when it
# was built with libyaml. Without it PyYAML's own emitter differs in edge cases
# such as astral characters and empty keys.
requires_libyaml = pytest.mark.skipif(
    not getattr(yaml, "__with_libyaml__", False),
    reason="PyYAML was built without libyaml",
)


def pyyaml_load_all(text: str) -> list[Any]:
    return list(yaml.load_all(text, Loader=YAML_LOADER))


def same(a: Any, b: Any) -> bool:
    """Compare loaded documents, telling 1, 1.0 and True apart."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, float) and math.isnan(a):
        return math.isnan(b)
    return a == b


def test_extension_is_in_use() -> None:
    assert output._fastyaml is not None


DUMP_CASES = [
    "x" * 200 + " y z " * 30,  # plain scalar folded at 80 columns
    " leading space",
    "trailing space ",
    "",
    "true",
    "yes",
    "null",
    "~",
    "0123",
    "1e5",
    "3.14",
    "1:30",
    "2001-12-14",
    "<<",
    "=",
    "a\nb\n",
    "a\nb",
    "a\n\n",
    "\n",
    "\n leading break",
    "line \nwith a space before the break",
    "tab\there",
    "emoji 😀",
    "nbsp x",
    "bom﻿x",
    "nul\x00x",
    "crlf\r\nx",
    "nel\x85x",
    "line separator x",
    "it's",
    "'" + "quoted word " * 20,
    "a\tb " + "word " * 30,  # double quoted and folded
    "- item",
    "-not an item",
    "a: b",
    "a:b",
    "#comment",
    "not # a comment",
    "x #comment",
    "---",
    "...",
    "?x",
    "? x",
    "http://example.com/path?q=1",
    "{not: flow}",
    "[1, 2]",
    "&anchor",
    "*alias",
    "!tag",
    "%directive",
    "@reserved",
    "`reserved",
    "|literal",
    ">folded",
    "  ",
    "word " * 25 + "\n" + "word " * 25,
]


@requires_libyaml
@pytest.mark.parametrize("value", DUMP_CASES)
def test_dump_matches_pyyaml_for_values(value: str) -> None:
    docs = [{"key": value, "list": [value], value: "as a key"}]
    assert dump_all(docs) == pyyaml_dump_all(docs)


@pytest.mark.parametrize(
    "docs",
    [
        [{"a": 1}, {"b": [1, 2]}, ["x"], "scalar document", None, {"c": "end\n\n"}],
        [{"k" * 130: "a key too long to be a simple key"}],
        [{"multi\nline key": 1}],
        [{2: "int key", True: "bool key", None: "null key", 1.5: "float key"}],
        [{"empty": {}, "also empty": [], "nested": [[], [{}], [[1, [2]]]]}],
        [[{"a": 1, "b": {"c": [1, {"d": 2}]}}]],
        [{"numbers": [0, -3, 2**70, -(2**70), 1.5, 1e20, 1e-7, -0.0]}],
        [{"floats": [float("inf"), float("-inf"), float("nan")]}],
        [{"bools": [True, False, None]}],
        [{"block": {"lines": "one\n  indented\nthree\n"}}],
    ],
)
@requires_libyaml
def test_dump_matches_pyyaml_for_documents(docs: list[Any]) -> None:
    assert dump_all(docs) == pyyaml_dump_all(docs)


@pytest.mark.parametrize(
    "docs",
    [
        [{"tuple": (1, 2)}],
        [{"set": {1, 2}}],
        [{"bytes": b"x"}],
        [{"shared": (shared := {"a": 1}), "again": shared}],
    ],
)
def test_dump_falls_back_to_pyyaml(docs: list[Any]) -> None:
    with pytest.raises(Fallback):
        dump_all(docs)
    assert dump_all_yaml(docs) == pyyaml_dump_all(docs)


LOAD_CASES = [
    "a: 1\nb: -2\nc: +3\nd: 0o17\ne: 017\nf: 0x1F\ng: 0b101\nh: 1_000\n",
    "a: 1.5\nb: 1.\nc: .5\nd: 1.5e+3\ne: 1e3\nf: .inf\ng: -.Inf\nh: .NaN\n",
    "a: 123456789012345678901234567890\nb: -0\n",
    "a: [yes, No, ON, off, true, FALSE, y, n]\n",
    "a: ~\nb: null\nc: Null\nd:\ne: ''\n",
    "a: !!str 1\nb: '2'\nc: \"3\"\n",
    "a: &x {k: v}\nb: *x\nc: &y [1, 2]\nd: *y\ne: &z s\nf: *z\n",
    "a: |\n  one\n  two\nb: >\n  folded\n  text\nc: |-\n  kept\n",
    "- a\n- - b\n  - c\n- {d: e, f: [g, h]}\n",
    "---\na: 1\n---\n---\n- b\n...\n",
    "",
    "# just a comment\n",
    "a: 1\na: 2\n",
    "1: int key\ntrue: bool key\n~: null key\n",
    "a: 'it''s'\nb: \"tab\\tnewline\\nunicode\\u00e9\"\n",
    "a: plain with:colon\nb: http://x\n",
]


@pytest.mark.parametrize("text", LOAD_CASES)
def test_load_matches_pyyaml(text: str) -> None:
    assert same(load_all(text), pyyaml_load_all(text))


@pytest.mark.parametrize(
    "text",
    [
        "base: &b {x: 1}\nderived:\n  <<: *b\n  y: 2\n",
        "when: 2001-12-14\n",
        "when: 2001-12-14t21:59:43.10-05:00\n",
        "a: 1:30\n",
        "a: !!binary aGVsbG8=\n",
        "a: !custom x\n",
        "&key: value\n",
        "a: &x:y 1\nb: *x:y\n",
        'rules:\n  - resources: [\n      "pods"\n    ]\n    verbs: [get]\n',
    ],
)
def test_load_falls_back_to_pyyaml(text: str) -> None:
    with pytest.raises(Fallback):
        load_all(text)
    try:
        expected = [d for d in pyyaml_load_all(text) if d]
    except yaml.YAMLError as e:
        with pytest.raises(type(e)):
            load_all_yaml(text)
    else:
        assert same(load_all_yaml(text), expected)


def test_load_errors_still_come_from_pyyaml() -> None:
    with pytest.raises(yaml.YAMLError):
        load_all_yaml("a: [unclosed\n")


TRICKY = list(" \n\t\r:#-'\"!&*?|>{}[],%@`~=<0123456789.eE+_abcfilnorstuy\x85  ﻿😀é")
WORDS = ["word", "longerword", "x", "-", ":", "#", "'", '"', "true", "1", "0.5", "a:b"]

scalar_texts = st.one_of(
    st.text(alphabet=TRICKY, max_size=40),
    st.text(max_size=20),
    st.lists(st.sampled_from(WORDS), max_size=60).map(" ".join),
    st.sampled_from(
        ["true", "null", "~", "yes", "0o1", "1e5", "1.5", "---", "...", "<<", "="]
    ),
)
scalars = st.one_of(
    scalar_texts,
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=-(2**80), max_value=2**80),
    st.floats(),
)
keys = st.one_of(scalar_texts, st.integers(), st.booleans(), st.none())
documents = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(keys, children, max_size=5),
    ),
    max_leaves=40,
)


@requires_libyaml
@settings(max_examples=500, deadline=None)
@given(st.lists(documents, min_size=1, max_size=3))
def test_dump_matches_pyyaml_for_generated_documents(docs: list[Any]) -> None:
    assert dump_all(docs) == pyyaml_dump_all(docs)


@settings(max_examples=500, deadline=None)
@given(st.lists(documents, min_size=1, max_size=3))
def test_load_matches_pyyaml_for_generated_documents(docs: list[Any]) -> None:
    text = pyyaml_dump_all(docs)
    # Read as line breaks by PyYAML's YAML 1.1 and as content by YAML 1.2.
    assume(not any(c in text for c in "\x85\u2028\u2029"))
    try:
        loaded = load_all(text)
    except Fallback:
        return
    assert same(loaded, pyyaml_load_all(text))


def test_load_reads_yaml_1_1_line_breaks_as_content() -> None:
    # Unlike PyYAML, which follows YAML 1.1, the native loader follows YAML 1.2
    # here. Nothing in a manifest relies on either reading.
    assert load_all("a: 'next line'\n") == [{"a": "next line"}]


def test_load_fallback_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="manifest_builder.output"):
        load_all_yaml("when: 2001-12-14\n")
    assert "PyYAML instead of the native loader: timestamp" in caplog.text


def test_dump_fallback_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    doc = {"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"x": (1, 2)}}
    with caplog.at_level(logging.DEBUG, logger="manifest_builder.output"):
        dump_all_yaml([doc])
    assert "Dumping ConfigMap/c with PyYAML instead of the native dumper" in (
        caplog.text
    )


def test_missing_extension_is_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "_fastyaml", None)
    output._warn_native_yaml_unavailable.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="manifest_builder.output"):
            load_all_yaml("a: 1\n")
            dump_all_yaml([{"a": 1}])
    finally:
        output._warn_native_yaml_unavailable.cache_clear()
    assert caplog.text.count("without its native YAML extension") == 1
