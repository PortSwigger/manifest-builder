# Manifest Builder

Generate materialized Kubernetes manifests from various types of configuration.

## Installation

To install or upgrade to the latest version:

```bash
uv pip install --upgrade --pre --extra-index-url https://packages.buildkite.com/nresare/python/pypi/simple manifest-builder
```

This includes nightly builds of development versions (e.g., `0.2.1.dev6+...`).

## Development

This project is using [uv](https://docs.astral.sh/uv/) for development. To set up your dev environment,
run `uv sync`. Tests and checks can be run with the following commands:

- `uv run ruff check`
- `uv run ruff format --check`
- `uv run ty check`
- `uv run pytest`

## Requirements

- Python 3.14+
- Helm 3.x (must be installed and available in PATH)
- Git (required for `--create-commit` feature)

## Python API

Use `manifest_builder.generate` to generate manifests from Python:

```python
from pathlib import Path

from manifest_builder import generate

written_paths = generate(Path("conf"), Path("output"))
```

Extra template variables can be supplied directly from Python. They are merged
with the `[variables]` table in `config.toml` just like values loaded with
`--vars-from`:

```python
generate(Path("conf"), Path("output"), vars={"domain": "example.com"})
```

## Targets and sections

A configuration directory comes in one of two layouts, told apart by the
`version` field of its top-level `config.toml`.

By default, or with `version = 1`, `config.toml` declares the config blocks
directly.

With `version = 2`, `config.toml` declares **targets** instead. A target names
the **sections** it is built from, and carries the variables those sections are
rendered with. This lets one configuration directory describe several
deployments of the same sections:

```toml
version = 2

[[target]]
name = "platform-dev"
sections = ["base", "platform"]
[target.vars]
cluster_name = "platform-dev"
vanity_domain = "portswigger.com"

[[target]]
name = "platform-prod"
sections = ["base", "platform"]
[target.vars]
cluster_name = "platform-prod"
vanity_domain = "portswigger.net"
```

A section is a subdirectory of the configuration directory holding a
`section.toml` of blocks — the same content a `version = 1` `config.toml` would
hold:

```
conf/
├── config.toml          # targets only
├── base/
│   ├── section.toml     # [[helm]], [[simple]], … blocks
│   └── argocd/
│       └── values.yaml
├── platform/
│   ├── section.toml
│   └── idcat/
│       └── idcat.toml
├── images.toml
├── releases.yaml
├── owners/
└── plugins/
```

Which target to generate is selected with `--target`, and is required for a
`version = 2` directory:

```bash
manifest-builder --config-dir conf --output-dir output --target platform-dev
```

From Python it is the `target` argument of `generate`:

```python
generate(Path("conf"), Path("output"), target="platform-dev")
```

Notes:

- A section's blocks are read from that section's own `section.toml`, so the
  paths they reference resolve inside the section directory. In the layout
  above, `base/section.toml` refers to its values file as `argocd/values.yaml`.
  Two sections can therefore use the same relative path without colliding.
- A section file may also be named `config.toml` or `manifest-builder.toml`, so
  moving an existing top-level config file into a section directory works
  unchanged. `section.toml` wins if more than one is present.
- A section may add a `[variables]` table of its own. It is merged with the
  target's `vars`, as are variables from `--vars-from` and `generate(vars=...)`.
  A variable defined by more than one of these is an error rather than one
  silently winning.
- `images.toml`, `releases.yaml`, `owners/`, and `plugins/` stay at the top of
  the configuration directory and are shared by every target.
- Only the sections the selected target names are loaded.

## Image template variables

Shared container image definitions can be placed in `images.toml` in the
configuration directory:

```toml
[git]
repo = "alpine/git"
version = "2.47.2"

[static-web-server]
repo = "ghcr.io/static-web-server/static-web-server"
version = "2.36.1"
```

Each entry is made available to Mustache templates as both the full image
reference and the version. Dashes in image names are converted to underscores:

- `{{git_image}}` renders as `alpine/git:2.47.2`
- `{{git_version}}` renders as `2.47.2`
- `{{static_web_server_image}}` renders as `ghcr.io/static-web-server/static-web-server:2.36.1`
- `{{static_web_server_version}}` renders as `2.36.1`

## Config block plugins

The built-in config blocks (`[[helm]]`, `[[simple]]`, and `[[copy]]`)
each live in one module under `manifest_builder/blocks/` and are discovered at
startup. A configuration directory can add blocks of its own by dropping modules
into a `plugins/` subdirectory:

```
conf/
├── config.toml
└── plugins/
    ├── public_repo.py                  # defines PublicRepoConfigHandler
    ├── templates/
    │   └── public_repo/                # namespaced per block
    │       └── repository.yaml
    └── tests/
        └── test_public_repo.py
```

Every module in `plugins/` is imported, and any concrete `ConfigHandler`
subclass it defines is registered under the top-level TOML key its
`top_level_config_name()` returns. A plugin block is used exactly like a
built-in one:

```toml
[[public-repo]]
name = "idcat"
```

A plugin module implements the same interface as a built-in block:

```python
from manifest_builder.handlers import ConfigHandler, GenerationContext
from manifest_builder.output import write_documents


class PublicRepoConfigHandler(ConfigHandler[PublicRepoConfig]):
    def top_level_config_name(self) -> str:
        return "public-repo"

    ...
```

Notes:

- Modules whose names start with `_` or `.` are skipped, as are test modules
  (`test_*.py`, `*_test.py`, `conftest.py`) and `tests/` directories, so a
  plugin directory can keep its own tests beside the code.
- Plugins are imported under the `manifest_builder_plugins` package rather than
  onto `sys.path`, so a plugin named `json.py` cannot shadow an installed
  module. Sibling modules are reachable with a relative import
  (`from .helpers import ...`).
- Handlers are registered in a stable order, sorted by their top-level key, so
  a run does not depend on filesystem order. Two handlers claiming the same key
  is an error.
- Bundled templates should be resolved relative to the plugin module, for
  example `Path(__file__).parent / "templates" / "public_repo"`.

Plugin modules are imported from the configuration directory, so that directory
is trusted to the same degree as the manifest-builder installation itself.

### Plugins in a long-running process

A process that calls `generate()` repeatedly may be pointed at a configuration
directory that has been checked out again between calls, possibly at the same
path. Every call re-reads the plugins rather than reusing what it imported
before, so no cache invalidation is needed from the caller:

- Plugin modules are dropped from `sys.modules` and imported afresh on each
  call, so replaced module source takes effect.
- Import-system directory caches are invalidated, so plugins added or removed
  since the last call are seen.
- No `__pycache__` is written for plugin modules. This keeps the configuration
  checkout clean, and avoids the one case where a re-import could still pick up
  stale code, since bytecode validity is judged on source mtime truncated to the
  second plus file size.
- Templates should be read at generation time rather than cached at import, as
  in the example above; combined with the module reload, they then always come
  from the current checkout.

Plugin loading is serialized with a lock, so concurrent generation from two
different configuration directories cannot interleave one load with another.
`manifest_builder.discovery.forget_plugin_modules()` is available for a caller
that wants to release imported plugin modules itself, but calling it is not
required.

## Externally-owned output roots

When the output repository is shared with other services or pipelines that
make their own commits, manifest-builder can be told which top-level output
directories it does not own. Files in those directories are left alone during
cleanup, and generation fails fast if any output would land in one of them.

To declare ownership, add an `owners/` directory to your config directory and
drop one or more TOML files into it. Each file may set:

```toml
# A single output root owned by another pipeline:
owned = "team-a"

# Or a list of output roots:
owned = ["cluster", "monitoring", "logging"]
```

Entries from all `owners/*.toml` files are merged into a single set of
externally-owned output roots.

## License

MIT
