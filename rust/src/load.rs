// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: The manifest-builder contributors
//! A PyYAML SafeLoader-compatible `load_all`, for the subset of YAML that
//! Kubernetes manifests use. Anything outside that subset (merge keys,
//! timestamps, explicit tags other than !!str, parse errors) raises
//! `Fallback`, so the caller can hand the stream to PyYAML instead.
//!
//! Whitespace follows saphyr, which implements YAML 1.2, rather than PyYAML's
//! YAML 1.1: NEL, LS and PS are content rather than line breaks, and saphyr
//! is more lenient about some block scalars. Manifests are unaffected.

use std::collections::HashMap;
use std::sync::LazyLock;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString};
use regex::Regex;
use saphyr_parser::{Event, Parser, ScalarStyle};

pyo3::create_exception!(manifest_builder._fastyaml, Fallback, PyException);

pub fn fallback(msg: impl Into<String>) -> PyErr {
    Fallback::new_err(msg.into())
}

static FLOAT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\A(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\z",
    )
    .unwrap()
});
static INT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\A(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)\z",
    )
    .unwrap()
});
static TIMESTAMP_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\A(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)\z",
    )
    .unwrap()
});

#[derive(PartialEq, Eq, Clone, Copy)]
pub enum Tag {
    Null,
    Bool(bool),
    Int,
    Float,
    Merge,
    Timestamp,
    Value,
    Str,
}

/// Resolve a plain scalar the way PyYAML's implicit resolvers would, in the
/// same order: bool, float, int, merge, null, timestamp, value.
pub fn plain_tag(v: &str) -> Tag {
    let Some(&c) = v.as_bytes().first() else {
        return Tag::Null;
    };
    if matches!(
        c,
        b'y' | b'Y' | b'n' | b'N' | b't' | b'T' | b'f' | b'F' | b'o' | b'O'
    ) {
        match v {
            "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => {
                return Tag::Bool(true)
            }
            "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => {
                return Tag::Bool(false)
            }
            _ => {}
        }
    }
    if matches!(c, b'-' | b'+' | b'0'..=b'9' | b'.') && FLOAT_RE.is_match(v) {
        return Tag::Float;
    }
    if matches!(c, b'-' | b'+' | b'0'..=b'9') && INT_RE.is_match(v) {
        return Tag::Int;
    }
    if c == b'<' && v == "<<" {
        return Tag::Merge;
    }
    if matches!(c, b'~' | b'n' | b'N') && matches!(v, "~" | "null" | "Null" | "NULL") {
        return Tag::Null;
    }
    if c.is_ascii_digit() && TIMESTAMP_RE.is_match(v) {
        return Tag::Timestamp;
    }
    if c == b'=' && v == "=" {
        return Tag::Value;
    }
    Tag::Str
}

pub fn plain_resolves_to_str(v: &str) -> bool {
    plain_tag(v) == Tag::Str
}

fn resolve_plain<'py>(py: Python<'py>, v: &str) -> PyResult<Bound<'py, PyAny>> {
    match plain_tag(v) {
        Tag::Null => Ok(py.None().into_bound(py)),
        Tag::Bool(b) => Ok(b.into_pyobject(py)?.to_owned().into_any()),
        Tag::Float => construct_float(py, v),
        Tag::Int => construct_int(py, v),
        Tag::Merge => Err(fallback("merge key")),
        Tag::Timestamp => Err(fallback("timestamp")),
        Tag::Value => Err(fallback("value tag")),
        Tag::Str => Ok(PyString::new(py, v).into_any()),
    }
}

fn construct_float<'py>(py: Python<'py>, v: &str) -> PyResult<Bound<'py, PyAny>> {
    let mut s: String = v
        .chars()
        .filter(|&c| c != '_')
        .collect::<String>()
        .to_lowercase();
    let mut sign = 1.0;
    if s.starts_with('-') {
        sign = -1.0;
    }
    if s.starts_with('-') || s.starts_with('+') {
        s.remove(0);
    }
    let f = if s == ".inf" {
        f64::INFINITY * sign
    } else if s == ".nan" {
        f64::NAN
    } else if s.contains(':') {
        return Err(fallback("sexagesimal float"));
    } else {
        sign * s.parse::<f64>().map_err(|_| fallback("float"))?
    };
    Ok(PyFloat::new(py, f).into_any())
}

fn construct_int<'py>(py: Python<'py>, v: &str) -> PyResult<Bound<'py, PyAny>> {
    let mut s: String = v.chars().filter(|&c| c != '_').collect();
    let neg = s.starts_with('-');
    if s.starts_with('-') || s.starts_with('+') {
        s.remove(0);
    }
    let (digits, radix) = if s == "0" {
        ("0", 10)
    } else if let Some(r) = s.strip_prefix("0b") {
        (r, 2)
    } else if let Some(r) = s.strip_prefix("0x") {
        (r, 16)
    } else if s.starts_with('0') {
        (s.as_str(), 8)
    } else if s.contains(':') {
        return Err(fallback("sexagesimal int"));
    } else {
        (s.as_str(), 10)
    };
    match i64::from_str_radix(digits, radix) {
        Ok(n) => Ok(PyInt::new(py, if neg { -n } else { n }).into_any()),
        Err(_) => {
            // Too big for i64 (or empty after stripping): let Python do it.
            let builtins = py.import("builtins")?;
            let n = builtins
                .getattr("int")?
                .call1((digits, radix))
                .map_err(|_| fallback("int"))?;
            if neg {
                n.neg()
            } else {
                Ok(n)
            }
        }
    }
}

/// Whether `text` might hold an anchor or alias whose name YAML 1.2 reads
/// differently from YAML 1.1. YAML 1.1 names are made of `[A-Za-z0-9_-]` and
/// end at the first other character, where YAML 1.2 takes everything up to
/// whitespace or a flow indicator, so `&a: x` is an anchor named `a` on a
/// mapping to PyYAML but one named `a:` on a scalar to saphyr. This errs
/// towards false positives, such as `*` or `&` inside a scalar, which only
/// cost a fallback, and so only runs on streams that have anchors.
fn has_yaml_1_2_only_anchor_names(text: &str) -> bool {
    let b = text.as_bytes();
    let ends_name =
        |c: u8| c.is_ascii_whitespace() || matches!(c, b'[' | b']' | b'{' | b'}' | b',');
    for (i, &c) in b.iter().enumerate() {
        if (c == b'&' || c == b'*') && (i == 0 || ends_name(b[i - 1])) {
            let name = &b[i + 1..];
            let len = name
                .iter()
                .position(|&c| ends_name(c))
                .unwrap_or(name.len());
            let name = &name[..len];
            if name.is_empty()
                || !name
                    .iter()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
            {
                return true;
            }
        }
    }
    false
}

enum Frame<'py> {
    Seq(Vec<Bound<'py, PyAny>>, usize),
    Map(Bound<'py, PyDict>, Option<Bound<'py, PyAny>>),
}

#[pyfunction]
pub fn load_all<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyList>> {
    let docs = PyList::empty(py);
    // Anything YAML 1.1 reads as an anchor, YAML 1.2 does too, so the names only
    // need checking once saphyr reports one.
    let mut anchor_names_checked = false;
    let mut check_anchor_names = || -> PyResult<()> {
        if !anchor_names_checked {
            anchor_names_checked = true;
            if has_yaml_1_2_only_anchor_names(text) {
                return Err(fallback("anchor name YAML 1.1 reads differently"));
            }
        }
        Ok(())
    };
    let mut stack: Vec<Frame<'py>> = Vec::new();
    let mut anchors: HashMap<usize, Bound<'py, PyAny>> = HashMap::new();
    let mut doc: Option<Bound<'py, PyAny>> = None;

    // Attach a finished node to whatever contains it.
    fn push<'py>(
        stack: &mut [Frame<'py>],
        doc: &mut Option<Bound<'py, PyAny>>,
        node: Bound<'py, PyAny>,
    ) -> PyResult<()> {
        match stack.last_mut() {
            None => *doc = Some(node),
            Some(Frame::Seq(items, _)) => items.push(node),
            Some(Frame::Map(dict, key)) => match key.take() {
                None => *key = Some(node),
                Some(k) => dict
                    .set_item(k, node)
                    .map_err(|e| fallback(format!("bad key: {e}")))?,
            },
        }
        Ok(())
    }

    for ev in Parser::new_from_str(text) {
        let (ev, _span) = ev.map_err(|e| fallback(format!("parse error: {e}")))?;
        match ev {
            Event::StreamStart | Event::StreamEnd | Event::Nothing => {}
            Event::DocumentStart(_) => {
                doc = None;
                anchors.clear();
            }
            Event::DocumentEnd => {
                docs.append(doc.take().unwrap_or_else(|| py.None().into_bound(py)))?;
            }
            Event::Alias(id) => {
                check_anchor_names()?;
                let node = anchors
                    .get(&id)
                    .cloned()
                    .ok_or_else(|| fallback("unknown alias"))?;
                push(&mut stack, &mut doc, node)?;
            }
            Event::Scalar(value, style, anchor, tag) => {
                let node = match (&tag, style) {
                    (Some(t), _) => {
                        if t.handle == "tag:yaml.org,2002:" && t.suffix == "str" {
                            PyString::new(py, &value).into_any()
                        } else {
                            return Err(fallback(format!("tag {}{}", t.handle, t.suffix)));
                        }
                    }
                    (None, ScalarStyle::Plain) => resolve_plain(py, &value)?,
                    (None, _) => PyString::new(py, &value).into_any(),
                };
                if anchor != 0 {
                    check_anchor_names()?;
                    anchors.insert(anchor, node.clone());
                }
                push(&mut stack, &mut doc, node)?;
            }
            Event::SequenceStart(anchor, tag) => {
                if tag.is_some() {
                    return Err(fallback("tagged sequence"));
                }
                if anchor != 0 {
                    check_anchor_names()?;
                }
                stack.push(Frame::Seq(Vec::new(), anchor));
            }
            Event::MappingStart(anchor, tag) => {
                if tag.is_some() {
                    return Err(fallback("tagged mapping"));
                }
                let dict = PyDict::new(py);
                // Register before children so a (pathological) self-alias works.
                if anchor != 0 {
                    check_anchor_names()?;
                    anchors.insert(anchor, dict.clone().into_any());
                }
                stack.push(Frame::Map(dict, None));
            }
            Event::SequenceEnd => {
                let Some(Frame::Seq(items, anchor)) = stack.pop() else {
                    return Err(fallback("unbalanced sequence"));
                };
                let list = PyList::new(py, items)?.into_any();
                if anchor != 0 {
                    anchors.insert(anchor, list.clone());
                }
                push(&mut stack, &mut doc, list)?;
            }
            Event::MappingEnd => {
                let Some(Frame::Map(dict, _)) = stack.pop() else {
                    return Err(fallback("unbalanced mapping"));
                };
                push(&mut stack, &mut doc, dict.into_any())?;
            }
        }
    }
    Ok(docs)
}
