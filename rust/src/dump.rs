// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: The manifest-builder contributors
//! Byte-for-byte reimplementation of
//! `yaml.dump_all(docs, Dumper=CDumper, default_flow_style=False,
//! sort_keys=False, allow_unicode=True)` with manifest-builder's str
//! representer (literal style for multi-line, single quotes for number-like
//! strings). The emitter is a port of libyaml 0.2.5's emitter.c, restricted
//! to block style with empty flow collections, which is all that dump call
//! can produce. Anything else (other Python types, shared containers that
//! PyYAML would emit as anchors) raises `Fallback`.

use std::collections::HashSet;
use std::sync::LazyLock;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use regex::Regex;

use crate::load::{fallback, plain_resolves_to_str};

const BEST_INDENT: i32 = 2;
const BEST_WIDTH: i32 = 80;
/// Deeper documents go to PyYAML rather than risk this thread's stack.
const MAX_DEPTH: usize = 512;

static NUMBER_LIKE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[-+]?[0-9][0-9_]*(\.[0-9_]*)?([eE][-+]?[0-9]+)?\z").unwrap());

#[derive(Clone, Copy, PartialEq, Eq)]
enum Style {
    Any,
    Plain,
    Single,
    Double,
    Literal,
}

#[derive(Default)]
struct Analysis {
    multiline: bool,
    flow_plain_allowed: bool,
    block_plain_allowed: bool,
    single_quoted_allowed: bool,
    block_allowed: bool,
}

// Character classes from libyaml's yaml_private.h, on a NUL-padded buffer.
#[inline]
fn width(b: u8) -> usize {
    if b & 0x80 == 0x00 {
        1
    } else if b & 0xE0 == 0xC0 {
        2
    } else if b & 0xF0 == 0xE0 {
        3
    } else if b & 0xF8 == 0xF0 {
        4
    } else {
        0
    }
}
#[inline]
fn is_printable(s: &[u8], i: usize) -> bool {
    let c = s[i];
    c == 0x0A
        || (0x20..=0x7E).contains(&c)
        || (c == 0xC2 && s[i + 1] >= 0xA0)
        || (c > 0xC2 && c < 0xED)
        || (c == 0xED && s[i + 1] < 0xA0)
        || c == 0xEE
        || (c == 0xEF
            && !(s[i + 1] == 0xBB && s[i + 2] == 0xBF)
            && !(s[i + 1] == 0xBF && (s[i + 2] == 0xBE || s[i + 2] == 0xBF)))
}
#[inline]
fn is_space(s: &[u8], i: usize) -> bool {
    s[i] == b' '
}
#[inline]
fn is_blank(s: &[u8], i: usize) -> bool {
    s[i] == b' ' || s[i] == b'\t'
}
#[inline]
fn is_break(s: &[u8], i: usize) -> bool {
    s[i] == b'\r'
        || s[i] == b'\n'
        || (s[i] == 0xC2 && s[i + 1] == 0x85)
        || (s[i] == 0xE2 && s[i + 1] == 0x80 && s[i + 2] == 0xA8)
        || (s[i] == 0xE2 && s[i + 1] == 0x80 && s[i + 2] == 0xA9)
}
#[inline]
fn is_bom(s: &[u8], i: usize) -> bool {
    s[i] == 0xEF && s[i + 1] == 0xBB && s[i + 2] == 0xBF
}
#[inline]
fn is_blankz(s: &[u8], i: usize) -> bool {
    is_blank(s, i) || is_break(s, i) || s[i] == 0
}

fn analyze(value: &str, readable: bool) -> Analysis {
    let len = value.len();
    if len == 0 {
        return Analysis {
            multiline: false,
            flow_plain_allowed: false,
            block_plain_allowed: true,
            single_quoted_allowed: true,
            block_allowed: false,
        };
    }
    let mut buf = Vec::with_capacity(len + 4);
    buf.extend_from_slice(value.as_bytes());
    buf.extend_from_slice(&[0, 0, 0, 0]);
    let s = &buf[..];

    let mut block_indicators = false;
    let mut flow_indicators = false;
    let mut line_breaks = false;
    let mut special_characters = false;
    let mut leading_space = false;
    let mut leading_break = false;
    let mut trailing_space = false;
    let mut trailing_break = false;
    let mut break_space = false;
    let mut space_break = false;
    let mut previous_space = false;
    let mut previous_break = false;

    if (s[0] == b'-' && s[1] == b'-' && s[2] == b'-')
        || (s[0] == b'.' && s[1] == b'.' && s[2] == b'.')
    {
        block_indicators = true;
        flow_indicators = true;
    }
    let mut preceded_by_whitespace = true;
    let mut followed_by_whitespace = is_blankz(s, width(s[0]));

    let mut p = 0;
    while p != len {
        let c = s[p];
        if p == 0 {
            if matches!(
                c,
                b'#' | b','
                    | b'['
                    | b']'
                    | b'{'
                    | b'}'
                    | b'&'
                    | b'*'
                    | b'!'
                    | b'|'
                    | b'>'
                    | b'\''
                    | b'"'
                    | b'%'
                    | b'@'
                    | b'`'
            ) {
                flow_indicators = true;
                block_indicators = true;
            }
            if c == b'?' || c == b':' {
                flow_indicators = true;
                if followed_by_whitespace {
                    block_indicators = true;
                }
            }
            if c == b'-' && followed_by_whitespace {
                flow_indicators = true;
                block_indicators = true;
            }
        } else {
            if matches!(c, b',' | b'?' | b'[' | b']' | b'{' | b'}') {
                flow_indicators = true;
            }
            if c == b':' {
                flow_indicators = true;
                if followed_by_whitespace {
                    block_indicators = true;
                }
            }
            if c == b'#' && preceded_by_whitespace {
                flow_indicators = true;
                block_indicators = true;
            }
        }
        // allow_unicode=True, so only unprintable characters are special.
        if !is_printable(s, p) {
            special_characters = true;
        }
        if is_break(s, p) {
            line_breaks = true;
        }
        let w = width(c);
        if is_space(s, p) {
            if p == 0 {
                leading_space = true;
            }
            if p + w == len {
                trailing_space = true;
            }
            if previous_break {
                break_space = true;
            }
            previous_space = true;
            previous_break = false;
        } else if is_break(s, p) {
            if p == 0 {
                leading_break = true;
            }
            if p + w == len {
                trailing_break = true;
            }
            if previous_space {
                space_break = true;
            }
            previous_space = false;
            previous_break = true;
        } else {
            previous_space = false;
            previous_break = false;
        }
        preceded_by_whitespace = is_blankz(s, p);
        p += w.max(1);
        if p != len {
            followed_by_whitespace = is_blankz(s, p + width(s[p]));
        }
    }

    let mut a = Analysis {
        multiline: line_breaks,
        flow_plain_allowed: true,
        block_plain_allowed: true,
        single_quoted_allowed: true,
        block_allowed: true,
    };
    if leading_space || leading_break || trailing_space || trailing_break {
        a.flow_plain_allowed = false;
        a.block_plain_allowed = false;
    }
    // Trailing spaces are valid block content. Keep libyaml's conservative
    // choice for hashing, but permit lossless literal blocks in file output.
    if trailing_space && !readable {
        a.block_allowed = false;
    }
    if break_space {
        a.flow_plain_allowed = false;
        a.block_plain_allowed = false;
        a.single_quoted_allowed = false;
    }
    if space_break || special_characters {
        a.flow_plain_allowed = false;
        a.block_plain_allowed = false;
        a.single_quoted_allowed = false;
        if special_characters || !readable {
            a.block_allowed = false;
        }
    }
    if line_breaks {
        a.flow_plain_allowed = false;
        a.block_plain_allowed = false;
    }
    if flow_indicators {
        a.flow_plain_allowed = false;
    }
    if block_indicators {
        a.block_plain_allowed = false;
    }
    a
}

/// A scalar as the representer hands it to the emitter.
struct Scalar {
    value: String,
    style: Style,
    plain_implicit: bool,
    quoted_implicit: bool,
}

struct Emitter {
    readable: bool,
    out: String,
    column: i32,
    whitespace: bool,
    indention: bool,
    indent: i32,
    indents: Vec<i32>,
    open_ended: u8,
    mapping_context: bool,
    simple_key_context: bool,
    depth: usize,
}

impl Emitter {
    fn new(readable: bool) -> Self {
        Emitter {
            readable,
            out: String::new(),
            column: 0,
            whitespace: true,
            indention: true,
            indent: -1,
            indents: Vec::new(),
            open_ended: 0,
            mapping_context: false,
            simple_key_context: false,
            depth: 0,
        }
    }

    #[inline]
    fn put(&mut self, c: char) {
        self.out.push(c);
        self.column += 1;
    }
    #[inline]
    fn put_break(&mut self) {
        self.out.push('\n');
        self.column = 0;
    }
    /// WRITE: copy one character from `s` at `*p`.
    #[inline]
    fn write(&mut self, s: &[u8], p: &mut usize) {
        let w = width(s[*p]).max(1);
        // SAFETY: s is valid UTF-8 up to its padding, and we copy whole chars.
        self.out
            .push_str(unsafe { std::str::from_utf8_unchecked(&s[*p..*p + w]) });
        *p += w;
        self.column += 1;
    }
    /// WRITE_BREAK
    #[inline]
    fn write_break(&mut self, s: &[u8], p: &mut usize) {
        if s[*p] == b'\n' {
            self.put_break();
            *p += 1;
        } else {
            let w = width(s[*p]).max(1);
            self.out
                .push_str(unsafe { std::str::from_utf8_unchecked(&s[*p..*p + w]) });
            *p += w;
            self.column = 0;
        }
    }

    fn increase_indent(&mut self, flow: bool, indentless: bool) {
        self.indents.push(self.indent);
        if self.indent < 0 {
            self.indent = if flow { BEST_INDENT } else { 0 };
        } else if !indentless {
            self.indent += BEST_INDENT;
        }
    }
    fn pop_indent(&mut self) {
        self.indent = self.indents.pop().unwrap();
    }

    fn write_indent(&mut self) {
        let indent = self.indent.max(0);
        if !self.indention || self.column > indent || (self.column == indent && !self.whitespace) {
            self.put_break();
        }
        while self.column < indent {
            self.put(' ');
        }
        self.whitespace = true;
        self.indention = true;
    }

    fn write_indicator(
        &mut self,
        ind: &str,
        need_whitespace: bool,
        is_whitespace: bool,
        is_indention: bool,
    ) {
        if need_whitespace && !self.whitespace {
            self.put(' ');
        }
        self.out.push_str(ind);
        self.column += ind.chars().count() as i32;
        self.whitespace = is_whitespace;
        self.indention = self.indention && is_indention;
    }

    // ---- node emission ----

    fn emit_node(
        &mut self,
        obj: &Bound<'_, PyAny>,
        mapping: bool,
        simple_key: bool,
        seen: &mut HashSet<usize>,
    ) -> PyResult<()> {
        if self.depth >= MAX_DEPTH {
            return Err(fallback("document too deep"));
        }
        self.depth += 1;
        let result = self.emit_node_inner(obj, mapping, simple_key, seen);
        self.depth -= 1;
        result
    }

    fn emit_node_inner(
        &mut self,
        obj: &Bound<'_, PyAny>,
        mapping: bool,
        simple_key: bool,
        seen: &mut HashSet<usize>,
    ) -> PyResult<()> {
        self.mapping_context = mapping;
        self.simple_key_context = simple_key;
        if let Ok(d) = obj.cast_exact::<PyDict>() {
            if !seen.insert(obj.as_ptr() as usize) {
                return Err(fallback("shared mapping (anchor)"));
            }
            if d.is_empty() {
                self.write_indicator("{", true, true, false);
                self.write_indicator("}", false, false, false);
                return Ok(());
            }
            self.increase_indent(false, false);
            for (k, v) in d.iter() {
                self.write_indent();
                let key = represent_scalar(&k)?;
                let ka = analyze(&key.value, self.readable);
                if !ka.multiline && key.value.len() <= 128 {
                    self.mapping_context = true;
                    self.simple_key_context = true;
                    self.emit_scalar(&key, &ka)?;
                    self.write_indicator(":", false, false, false);
                } else {
                    self.write_indicator("?", true, false, true);
                    self.mapping_context = true;
                    self.simple_key_context = false;
                    self.emit_scalar(&key, &ka)?;
                    self.write_indent();
                    self.write_indicator(":", true, false, true);
                }
                self.emit_node(&v, true, false, seen)?;
            }
            self.pop_indent();
            return Ok(());
        }
        if let Ok(l) = obj.cast_exact::<PyList>() {
            if !seen.insert(obj.as_ptr() as usize) {
                return Err(fallback("shared sequence (anchor)"));
            }
            if l.is_empty() {
                self.write_indicator("[", true, true, false);
                self.write_indicator("]", false, false, false);
                return Ok(());
            }
            let indentless = self.mapping_context && !self.indention;
            self.increase_indent(false, indentless);
            for item in l.iter() {
                self.write_indent();
                self.write_indicator("-", true, false, true);
                self.emit_node(&item, false, false, seen)?;
            }
            self.pop_indent();
            return Ok(());
        }
        let sc = represent_scalar(obj)?;
        let a = analyze(&sc.value, self.readable);
        self.emit_scalar(&sc, &a)
    }

    fn emit_scalar(&mut self, sc: &Scalar, a: &Analysis) -> PyResult<()> {
        // select_scalar_style, with flow_level always 0 and no tags.
        if !sc.plain_implicit && !sc.quoted_implicit {
            return Err(fallback("scalar needs a tag"));
        }
        let mut style = if sc.style == Style::Any {
            Style::Plain
        } else {
            sc.style
        };
        if self.simple_key_context && a.multiline {
            style = Style::Double;
        }
        if style == Style::Plain {
            if !a.block_plain_allowed {
                style = Style::Single;
            }
            if sc.value.is_empty() && self.simple_key_context {
                style = Style::Single;
            }
            if !sc.plain_implicit {
                style = Style::Single;
            }
        }
        if style == Style::Single && !a.single_quoted_allowed {
            style = Style::Double;
        }
        if style == Style::Literal && (!a.block_allowed || self.simple_key_context) {
            style = Style::Double;
        }
        if !sc.quoted_implicit && style != Style::Plain {
            return Err(fallback("scalar needs a ! tag"));
        }

        self.increase_indent(true, false);
        let allow_breaks = !self.simple_key_context;
        match style {
            Style::Plain => self.write_plain(&sc.value, allow_breaks),
            Style::Single => self.write_single(&sc.value, allow_breaks),
            Style::Double => self.write_double(&sc.value, allow_breaks),
            Style::Literal => self.write_literal(&sc.value),
            Style::Any => unreachable!(),
        }
        self.pop_indent();
        Ok(())
    }

    fn write_plain(&mut self, value: &str, allow_breaks: bool) {
        let len = value.len();
        let buf = padded(value);
        let s = &buf[..];
        let mut spaces = false;
        let mut breaks = false;
        if !self.whitespace && len > 0 {
            self.put(' ');
        }
        let mut p = 0;
        while p != len {
            if is_space(s, p) {
                if allow_breaks && !spaces && self.column > BEST_WIDTH && !is_space(s, p + 1) {
                    self.write_indent();
                    p += 1;
                } else {
                    self.write(s, &mut p);
                }
                spaces = true;
            } else if is_break(s, p) {
                if !breaks && s[p] == b'\n' {
                    self.put_break();
                }
                self.write_break(s, &mut p);
                self.indention = true;
                breaks = true;
            } else {
                if breaks {
                    self.write_indent();
                }
                self.write(s, &mut p);
                self.indention = false;
                spaces = false;
                breaks = false;
            }
        }
        self.whitespace = false;
        self.indention = false;
    }

    fn write_single(&mut self, value: &str, allow_breaks: bool) {
        let len = value.len();
        let buf = padded(value);
        let s = &buf[..];
        let mut spaces = false;
        let mut breaks = false;
        self.write_indicator("'", true, false, false);
        let mut p = 0;
        while p != len {
            if is_space(s, p) {
                if allow_breaks
                    && !spaces
                    && self.column > BEST_WIDTH
                    && p != 0
                    && p != len - 1
                    && !is_space(s, p + 1)
                {
                    self.write_indent();
                    p += 1;
                } else {
                    self.write(s, &mut p);
                }
                spaces = true;
            } else if is_break(s, p) {
                if !breaks && s[p] == b'\n' {
                    self.put_break();
                }
                self.write_break(s, &mut p);
                self.indention = true;
                breaks = true;
            } else {
                if breaks {
                    self.write_indent();
                }
                if s[p] == b'\'' {
                    self.put('\'');
                }
                self.write(s, &mut p);
                self.indention = false;
                spaces = false;
                breaks = false;
            }
        }
        if breaks {
            self.write_indent();
        }
        self.write_indicator("'", false, false, false);
        self.whitespace = false;
        self.indention = false;
    }

    fn write_double(&mut self, value: &str, allow_breaks: bool) {
        let len = value.len();
        let buf = padded(value);
        let s = &buf[..];
        let mut spaces = false;
        self.write_indicator("\"", true, false, false);
        let mut p = 0;
        while p != len {
            if !is_printable(s, p)
                || is_bom(s, p)
                || is_break(s, p)
                || s[p] == b'"'
                || s[p] == b'\\'
            {
                let w = width(s[p]);
                let ch = value[p..].chars().next().unwrap() as u32;
                p += w;
                self.put('\\');
                match ch {
                    0x00 => self.put('0'),
                    0x07 => self.put('a'),
                    0x08 => self.put('b'),
                    0x09 => self.put('t'),
                    0x0A => self.put('n'),
                    0x0B => self.put('v'),
                    0x0C => self.put('f'),
                    0x0D => self.put('r'),
                    0x1B => self.put('e'),
                    0x22 => self.put('"'),
                    0x5C => self.put('\\'),
                    0x85 => self.put('N'),
                    0xA0 => self.put('_'),
                    0x2028 => self.put('L'),
                    0x2029 => self.put('P'),
                    _ => {
                        let (c, digits) = if ch <= 0xFF {
                            ('x', 2)
                        } else if ch <= 0xFFFF {
                            ('u', 4)
                        } else {
                            ('U', 8)
                        };
                        self.put(c);
                        for k in (0..digits).rev() {
                            let d = (ch >> (k * 4)) & 0xF;
                            self.put(std::char::from_digit(d, 16).unwrap().to_ascii_uppercase());
                        }
                    }
                }
                spaces = false;
            } else if is_space(s, p) {
                if allow_breaks && !spaces && self.column > BEST_WIDTH && p != 0 && p != len - 1 {
                    self.write_indent();
                    if is_space(s, p + 1) {
                        self.put('\\');
                    }
                    p += 1;
                } else {
                    self.write(s, &mut p);
                }
                spaces = true;
            } else {
                self.write(s, &mut p);
                spaces = false;
            }
        }
        self.write_indicator("\"", false, false, false);
        self.whitespace = false;
        self.indention = false;
    }

    fn write_literal(&mut self, value: &str) {
        let len = value.len();
        let buf = padded(value);
        let s = &buf[..];
        self.write_indicator("|", true, false, false);
        // write_block_scalar_hints
        if is_space(s, 0) || is_break(s, 0) {
            let hint = char::from(b'0' + BEST_INDENT as u8).to_string();
            self.write_indicator(&hint, false, false, false);
        }
        self.open_ended = 0;
        let mut chomp: Option<&str> = None;
        if len == 0 {
            chomp = Some("-");
        } else {
            let mut q = len;
            loop {
                q -= 1;
                if s[q] & 0xC0 != 0x80 {
                    break;
                }
            }
            if !is_break(s, q) {
                chomp = Some("-");
            } else if q == 0 {
                chomp = Some("+");
                self.open_ended = 2;
            } else {
                loop {
                    q -= 1;
                    if s[q] & 0xC0 != 0x80 {
                        break;
                    }
                }
                if is_break(s, q) {
                    chomp = Some("+");
                    self.open_ended = 2;
                }
            }
        }
        if let Some(c) = chomp {
            self.write_indicator(c, false, false, false);
        }
        self.put_break();
        self.indention = true;
        self.whitespace = true;
        let mut breaks = true;
        let mut p = 0;
        while p != len {
            if is_break(s, p) {
                self.write_break(s, &mut p);
                self.indention = true;
                breaks = true;
            } else {
                if breaks {
                    self.write_indent();
                }
                self.write(s, &mut p);
                self.indention = false;
                breaks = false;
            }
        }
    }
}

fn padded(value: &str) -> Vec<u8> {
    let mut buf = Vec::with_capacity(value.len() + 4);
    buf.extend_from_slice(value.as_bytes());
    buf.extend_from_slice(&[0, 0, 0, 0]);
    buf
}

/// What PyYAML's Representer (plus manifest-builder's str representer)
/// turns a Python scalar into.
fn represent_scalar(obj: &Bound<'_, PyAny>) -> PyResult<Scalar> {
    if let Ok(s) = obj.cast_exact::<PyString>() {
        let value = s
            .to_str()
            .map_err(|_| fallback("unencodable str"))?
            .to_owned();
        let style = if value.contains('\n') {
            Style::Literal
        } else if NUMBER_LIKE.is_match(&value) {
            Style::Single
        } else {
            Style::Any
        };
        let plain_implicit = plain_resolves_to_str(&value);
        return Ok(Scalar {
            value,
            style,
            plain_implicit,
            quoted_implicit: true,
        });
    }
    if obj.is_none() {
        return Ok(Scalar {
            value: "null".into(),
            style: Style::Any,
            plain_implicit: true,
            quoted_implicit: false,
        });
    }
    if let Ok(b) = obj.cast_exact::<PyBool>() {
        let value = if b.is_true() { "true" } else { "false" };
        return Ok(Scalar {
            value: value.into(),
            style: Style::Any,
            plain_implicit: true,
            quoted_implicit: false,
        });
    }
    if obj.cast_exact::<PyInt>().is_ok() {
        let value = match obj.extract::<i64>() {
            Ok(n) => n.to_string(),
            Err(_) => obj.str()?.to_str()?.to_owned(),
        };
        return Ok(Scalar {
            value,
            style: Style::Any,
            plain_implicit: true,
            quoted_implicit: false,
        });
    }
    if let Ok(f) = obj.cast_exact::<PyFloat>() {
        let x = f.value();
        let value = if x.is_nan() {
            ".nan".to_owned()
        } else if x == f64::INFINITY {
            ".inf".to_owned()
        } else if x == f64::NEG_INFINITY {
            "-.inf".to_owned()
        } else {
            let mut v = obj.repr()?.to_str()?.to_lowercase();
            if !v.contains('.') && v.contains('e') {
                v = v.replacen('e', ".0e", 1);
            }
            v
        };
        return Ok(Scalar {
            value,
            style: Style::Any,
            plain_implicit: true,
            quoted_implicit: false,
        });
    }
    Err(fallback(format!(
        "unsupported type {}",
        obj.get_type().name()?
    )))
}

#[pyfunction]
#[pyo3(signature = (docs, *, readable=false))]
pub fn dump_all(docs: &Bound<'_, PyAny>, readable: bool) -> PyResult<String> {
    let mut e = Emitter::new(readable);
    let mut first = true;
    for doc in docs.try_iter()? {
        let doc = doc?;
        // emit_document_start: only the first document is implicit.
        e.open_ended = 0;
        if !first {
            e.write_indent();
            e.write_indicator("---", true, false, false);
        }
        first = false;
        let mut seen = HashSet::new();
        e.emit_node(&doc, false, false, &mut seen)?;
        // emit_document_end (implicit)
        e.write_indent();
        if e.open_ended == 0 {
            e.open_ended = 1;
        }
    }
    if e.open_ended == 2 {
        e.write_indicator("...", true, false, false);
        e.open_ended = 0;
        e.write_indent();
    }
    Ok(e.out)
}
