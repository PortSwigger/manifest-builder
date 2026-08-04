# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Finding the config handlers available for a generation run.

Built-in handlers are discovered by walking :mod:`manifest_builder.blocks`.
A config directory may supply more of its own in a ``plugins`` subdirectory,
letting a config repository add a config block without changing this package.
"""

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
from collections.abc import Iterator
from pathlib import Path
from threading import Lock
from types import ModuleType
from typing import Any

from manifest_builder.handlers import ConfigHandler

logger = logging.getLogger(__name__)

# Loading plugins mutates process-wide state: sys.modules and the bytecode
# writing flag. Serialize it so concurrent generation from two config
# directories cannot interleave one load with another.
_PLUGIN_LOAD_LOCK = Lock()

BLOCKS_PACKAGE = "manifest_builder.blocks"

#: Subdirectory of a config directory scanned for plugin modules.
PLUGINS_DIR_NAME = "plugins"

#: Synthetic package plugin modules are imported under. Keeping them in their
#: own namespace stops a plugin file from shadowing an installed module, and
#: gives it a parent package so sibling imports (``from . import helpers``)
#: resolve inside the plugins directory.
PLUGINS_PACKAGE = "manifest_builder_plugins"


def discover_handlers(config_dir: Path | None = None) -> list[ConfigHandler[Any]]:
    """Return one instance of every available config handler.

    Args:
        config_dir: Config directory to scan for a ``plugins`` subdirectory.
            When None, only built-in handlers are returned.

    Returns:
        Handler instances ordered by the top-level TOML key they own, so a run
        is reproducible regardless of filesystem or import order.

    Raises:
        ValueError: If two handlers claim the same top-level key.
    """
    classes = list(_builtin_handler_classes())
    plugin_classes: list[type[ConfigHandler[Any]]] = []
    plugin_count = 0
    if config_dir is not None:
        plugin_count, plugin_classes = _load_plugins(config_dir)
        classes.extend(plugin_classes)

    handlers: dict[str, ConfigHandler[Any]] = {}
    for handler_class in classes:
        handler = handler_class()
        key = handler.top_level_config_name()
        existing = handlers.get(key)
        if existing is not None:
            raise ValueError(
                f"Config handlers {type(existing).__name__} and "
                f"{handler_class.__name__} both claim the top-level key '{key}'"
            )
        handlers[key] = handler

    if plugin_count:
        from_plugins = set(plugin_classes)
        plugin_keys = sorted(
            key for key, handler in handlers.items() if type(handler) in from_plugins
        )
        logger.info(
            "Found %d plugin%s, now handling config block type%s %s",
            plugin_count,
            "" if plugin_count == 1 else "s",
            "" if len(plugin_keys) == 1 else "s",
            _quoted_list(plugin_keys),
        )

    return [handlers[key] for key in sorted(handlers)]


def _quoted_list(names: list[str]) -> str:
    """Render names as a quoted, comma-separated English list."""
    quoted = [f'"{name}"' for name in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return f"{', '.join(quoted[:-1])}, and {quoted[-1]}"


def _builtin_handler_classes() -> Iterator[type[ConfigHandler[Any]]]:
    """Yield handler classes defined in the bundled blocks package."""
    package = importlib.import_module(BLOCKS_PACKAGE)
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{BLOCKS_PACKAGE}.{module_info.name}")
        yield from _handler_classes_in(module)


def _load_plugins(
    config_dir: Path,
) -> tuple[int, list[type[ConfigHandler[Any]]]]:
    """Import plugin modules under ``<config_dir>/plugins``.

    Returns:
        The number of plugin modules that contributed at least one handler,
        and the handler classes they define.
    """
    plugins_dir = config_dir / PLUGINS_DIR_NAME
    if not plugins_dir.is_dir():
        return 0, []

    classes: list[type[ConfigHandler[Any]]] = []
    contributing = 0

    with _PLUGIN_LOAD_LOCK:
        _register_plugins_package(plugins_dir)

        # Writing no bytecode keeps __pycache__ out of the config checkout, and
        # removes the one way a re-import could still see stale code: .pyc
        # validity is judged on source mtime truncated to the second plus size,
        # which two checkouts in the same second could match.
        write_bytecode = not sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            for name in _plugin_module_names(plugins_dir):
                qualified = f"{PLUGINS_PACKAGE}.{name}"
                try:
                    module = importlib.import_module(qualified)
                except Exception as e:
                    raise ValueError(
                        f"Failed to load plugin '{name}' from {plugins_dir}: {e}"
                    ) from e
                found = list(_handler_classes_in(module))
                if found:
                    contributing += 1
                    logger.debug(
                        "Loaded plugin %s: %s",
                        name,
                        ", ".join(sorted(cls.__name__ for cls in found)),
                    )
                classes.extend(found)
        finally:
            if write_bytecode:
                sys.dont_write_bytecode = False

    return contributing, classes


def _is_test_module(name: str) -> bool:
    """Whether a module name looks like tests rather than a config block."""
    return (
        name in {"conftest", "test", "tests"}
        or name.startswith("test_")
        or name.endswith("_test")
    )


def _plugin_module_names(plugins_dir: Path) -> list[str]:
    """Return importable top-level module and package names, in a stable order.

    Dotted and underscored names are treated as private, and test modules are
    left alone so a plugin directory can keep its tests beside the code.
    """
    names: set[str] = set()
    for entry in plugins_dir.iterdir():
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.suffix == ".py":
            names.add(entry.stem)
    return sorted(name for name in names if not _is_test_module(name))


def forget_plugin_modules() -> None:
    """Drop every imported plugin module.

    Called before each plugin scan, so a caller that generates repeatedly does
    not need to invoke this itself.
    """
    for name in [
        name
        for name in sys.modules
        if name == PLUGINS_PACKAGE or name.startswith(f"{PLUGINS_PACKAGE}.")
    ]:
        del sys.modules[name]


def _register_plugins_package(plugins_dir: Path) -> None:
    """Install a fresh synthetic parent package rooted at ``plugins_dir``.

    Previously imported plugin modules are always discarded rather than reused.
    A long-running process may generate from a config directory that has been
    checked out again since the last run, possibly at the very same path, so a
    module cached in ``sys.modules`` cannot be assumed to match what is now on
    disk. Re-importing a few small modules per run is cheap next to the cost of
    silently generating from stale plugin code.
    """
    forget_plugin_modules()
    # Directory listings are cached per path, so a plugin added or removed since
    # the last run is only seen once those caches are dropped.
    importlib.invalidate_caches()

    spec = importlib.util.spec_from_loader(
        PLUGINS_PACKAGE, loader=None, is_package=True
    )
    assert spec is not None
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(plugins_dir.resolve())]
    sys.modules[PLUGINS_PACKAGE] = package


def _handler_classes_in(module: ModuleType) -> Iterator[type[ConfigHandler[Any]]]:
    """Yield concrete ConfigHandler subclasses a module defines itself."""
    for _, member in inspect.getmembers(module, inspect.isclass):
        if not issubclass(member, ConfigHandler) or member is ConfigHandler:
            continue
        # Skip classes merely imported into the module, and abstract bases.
        if member.__module__ != module.__name__ or inspect.isabstract(member):
            continue
        yield member
