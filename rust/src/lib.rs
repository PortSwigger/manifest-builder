// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: The manifest-builder contributors
//! Native YAML loading and dumping for manifest-builder.
//!
//! Both functions cover the YAML that Kubernetes manifests use and raise
//! `Fallback` for anything else, so that the Python side can hand the input to
//! PyYAML instead. The results are the same as PyYAML's either way.

use pyo3::prelude::*;

mod dump;
mod load;

#[pymodule]
fn _fastyaml(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(load::load_all, m)?)?;
    m.add_function(wrap_pyfunction!(dump::dump_all, m)?)?;
    m.add("Fallback", m.py().get_type::<load::Fallback>())?;
    Ok(())
}
