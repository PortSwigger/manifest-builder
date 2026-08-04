# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Config block implementations.

Each module here owns one top-level TOML key: its config dataclass, the
parser that builds it, its validation, and the ConfigHandler subclass that
ties them together. Modules in this package depend on the shared toolkit
(config, handlers, k8s, output) and never on each other.
"""
