# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Configuration parsing and validation for manifest-builder."""

import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from manifest_builder.helmfile import Helmfile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from manifest_builder.handlers import ConfigHandler

DEFAULT_REPLICA_COUNT = 2
TemplateValue = str | int | float | bool


def load_toml_file(path: Path) -> dict:
    """Parse a TOML file, reporting the file name on a syntax error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Failed to parse TOML file {path}: {e}") from e


def validate_known_fields(
    table_name: str,
    data: dict,
    allowed_fields: Collection[str],
    source_file: Path,
    table_index: int = 0,
) -> None:
    """Raise if a parsed TOML table contains fields the parser does not know."""
    unknown = sorted(set(data) - set(allowed_fields))
    if not unknown:
        return

    fields = ", ".join(
        _format_field_location(field, source_file, table_name, table_index)
        for field in unknown
    )
    suffix = "s" if len(unknown) != 1 else ""
    raise ValueError(
        f"Unknown field{suffix} in {table_name}: {fields} in {source_file}"
    )


def _format_field_location(
    field: str,
    source_file: Path,
    table_name: str | None = None,
    table_index: int = 0,
) -> str:
    line_number = _find_field_line(source_file, field, table_name, table_index)
    if line_number is None:
        return repr(field)
    return f"{field!r} on line {line_number}"


def _find_field_line(
    source_file: Path,
    field: str,
    table_name: str | None = None,
    table_index: int = 0,
) -> int | None:
    lines = source_file.read_text().splitlines()
    if table_name is None:
        return _find_top_level_field_line(lines, field)

    in_table = False
    current_index = -1
    for line_number, line in enumerate(lines, start=1):
        stripped = _strip_toml_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped == table_name:
                current_index += 1
                in_table = current_index == table_index
                continue
            if in_table:
                return None
            continue
        if in_table and _line_defines_toml_key(stripped, field):
            return line_number

    return None


def _find_top_level_field_line(lines: list[str], field: str) -> int | None:
    in_table = False
    for line_number, line in enumerate(lines, start=1):
        stripped = _strip_toml_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped in {f"[{field}]", f"[[{field}]]"}:
                return line_number
            in_table = True
            continue
        if not in_table and _line_defines_toml_key(stripped, field):
            return line_number
    return None


def _strip_toml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return line[:index]
    return line


def _line_defines_toml_key(stripped_line: str, field: str) -> bool:
    if "=" not in stripped_line:
        return False
    key = stripped_line.split("=", 1)[0].strip()
    return key == field


class ManifestConfig(Protocol):
    """What the generation orchestrator needs from any config block entry.

    Structural rather than a union of the known config types, so a config
    handler can bring its own dataclass without this module knowing about it.
    Declared as read-only properties to let a config narrow ``namespace`` to
    ``str`` when it always targets one.
    """

    @property
    def name(self) -> str:
        """Name of the config entry, used in logging and error messages."""

    @property
    def namespace(self) -> str | None:
        """Target namespace, or None for configs that only emit cluster scope."""


CONFIG_FILE_NAMES = ("config.toml", "manifest-builder.toml")


def load_images(config_dir: Path) -> dict[str, str]:
    """
    Load container image definitions from images.toml in the config directory.

    The images.toml file should have the format:
        [git]
        repo = "alpine/git"
        version = "2.47.2"

        [hugo]
        repo = "floryn90/hugo"
        version = "0.155.3-alpine"

    Returns a dict mapping template variable names to image references and
    image versions, e.g.:
        {
            "git_image": "alpine/git:2.47.2",
            "git_version": "2.47.2",
            "hugo_image": "floryn90/hugo:0.155.3-alpine",
            "hugo_version": "0.155.3-alpine",
        }

    If images.toml is absent, returns an empty dict so image overrides remain optional.

    Args:
        config_dir: Directory containing images.toml

    Returns:
        Dict mapping image variable names to full image references (repo:version)
        and version variable names to image versions

    Raises:
        ValueError: If images.toml is invalid or missing required fields
    """
    images_file = config_dir / "images.toml"
    if not images_file.exists():
        return {}

    data = load_toml_file(images_file)

    if not data:
        raise ValueError(f"images.toml is empty in {config_dir}")

    result = {}
    for key, image_def in data.items():
        if (
            not isinstance(image_def, dict)
            or "repo" not in image_def
            or "version" not in image_def
        ):
            raise ValueError(
                f"Each image in images.toml must have 'repo' and 'version' fields. "
                f"Invalid entry: {key}"
            )
        name = key.replace("-", "_")
        result[f"{name}_image"] = f"{image_def['repo']}:{image_def['version']}"
        result[f"{name}_version"] = image_def["version"]

    return result


def load_owned_namespaces(
    config_dir: Path, *, exclude_owner_files: set[str] | None = None
) -> set[str]:
    """Load the set of output roots owned by other services or pipelines.

    Reads ``<config_dir>/owners/*.toml``. Each file may declare ownership via an
    ``owned`` string or list of strings.
    Returns an empty set if the ``owners`` directory does not exist.
    """
    owners_dir = config_dir / "owners"
    if not owners_dir.is_dir():
        return set()

    excluded = exclude_owner_files or set()
    owned: set[str] = set()
    for toml_file in sorted(owners_dir.glob("*.toml")):
        if toml_file.name in excluded:
            continue
        data = load_toml_file(toml_file)

        owner_roots = data.get("owned")
        if owner_roots is None:
            continue
        if isinstance(owner_roots, str):
            owned.add(owner_roots)
            continue
        if not isinstance(owner_roots, list) or not all(
            isinstance(root, str) for root in owner_roots
        ):
            raise ValueError(
                f"'owned' must be a string or list of strings in {toml_file}"
            )
        owned.update(owner_roots)

    return owned


def load_extra_variables(path: Path) -> dict[str, TemplateValue]:
    """Load template variables from a standalone TOML file with top-level keys.

    The file is expected to declare each variable as a top-level key=value pair
    (no ``[variables]`` table), since the whole file is dedicated to variables.

    Args:
        path: Path to the TOML file.

    Returns:
        Dict mapping variable names to their scalar values.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the file contains nested tables or non-scalar values.
    """
    if not path.exists():
        raise FileNotFoundError(f"Variables file not found: {path}")

    data = load_toml_file(path)

    variables: dict[str, TemplateValue] = {}
    for key, value in data.items():
        if not isinstance(value, str | int | float | bool):
            raise ValueError(
                f"Variable '{key}' in {path} must be a string, number, or boolean"
            )
        variables[key] = value
    return variables


def parse_variables(
    data: object,
    source_file: Path,
) -> dict[str, TemplateValue]:
    """Parse the top-level ``[variables]`` table used for template rendering."""
    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError(f"'variables' must be a table in {source_file}")

    variables: dict[str, TemplateValue] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"Variable keys in {source_file} must be strings")
        if not isinstance(value, str | int | float | bool):
            raise ValueError(
                f"Variable '{key}' in {source_file} must be a string, number, or boolean"
            )
        variables[key] = value

    return variables


def load_configs(
    config_dir: Path,
    handlers: "Sequence[ConfigHandler]",
    extra_variables: dict[str, TemplateValue] | None = None,
    default_namespace: str | None = None,
    default_image: str | None = None,
) -> "Sequence[ConfigHandler]":
    """
    Load app configurations from the config directory.

    The top-level TOML config file may be named ``config.toml`` or
    ``manifest-builder.toml`` and may contain top-level tables owned by the
    supplied config handlers.

    Args:
        config_dir: Directory containing TOML configuration files
        handlers: Config handlers to populate
        extra_variables: Additional template variables merged into the
            ``[variables]`` table from the config file. Keys that overlap with
            ``[variables]`` in the config file are rejected with ValueError.
        default_namespace: Namespace to use when a config entry omits its
            ``namespace`` field.
        default_image: Image to use for namespace-mode simple and website
            config entries that omit their ``image`` field.

    Returns:
        Handlers populated with the config items they own

    Raises:
        FileNotFoundError: If config_dir or a top-level config file doesn't exist
        ValueError: If TOML is invalid or missing required fields
    """
    if not config_dir.exists():
        raise FileNotFoundError(f"Configuration directory not found: {config_dir}")

    if not config_dir.is_dir():
        raise ValueError(f"Configuration path is not a directory: {config_dir}")

    toml_file = next(
        (
            config_dir / name
            for name in CONFIG_FILE_NAMES
            if (config_dir / name).exists()
        ),
        None,
    )
    if toml_file is None:
        expected = " or ".join(str(config_dir / name) for name in CONFIG_FILE_NAMES)
        raise FileNotFoundError(f"Configuration file not found: {expected}")

    data = load_toml_file(toml_file)

    if extra_variables:
        existing = data.get("variables", {})
        if not isinstance(existing, dict):
            raise ValueError(f"'variables' must be a table in {toml_file}")
        overlap = sorted(set(existing) & set(extra_variables))
        if overlap:
            names = ", ".join(repr(name) for name in overlap)
            suffix = "s" if len(overlap) != 1 else ""
            raise ValueError(
                f"Variable{suffix} {names} defined in both {toml_file} "
                "and the --vars-from file"
            )
        data["variables"] = {**existing, **extra_variables}

    handler_by_name: dict[str, ConfigHandler] = {}
    for handler in handlers:
        name = handler.top_level_config_name()
        if name in handler_by_name:
            raise ValueError(f"Duplicate config handler for top-level key '{name}'")
        handler_by_name[name] = handler
    if not handler_by_name:
        raise ValueError("No config handlers registered")

    allowed_top_level = set(handler_by_name) | {"variables"}
    unknown_top_level = sorted(set(data) - allowed_top_level)
    if unknown_top_level:
        fields = ", ".join(
            _format_field_location(field, toml_file) for field in unknown_top_level
        )
        suffix = "s" if len(unknown_top_level) != 1 else ""
        raise ValueError(f"Unknown top-level field{suffix}: {fields} in {toml_file}")

    present_handler_names = sorted(name for name in handler_by_name if name in data)
    if not present_handler_names:
        expected = ", ".join(f"[[{name}]]" for name in sorted(handler_by_name))
        raise ValueError(f"No {expected} entries found in {toml_file}")

    for name in present_handler_names:
        handler_by_name[name].load_config(
            data[name], toml_file, data, default_namespace, default_image
        )

    return handlers


def resolve_configs(
    handlers: "Sequence[ConfigHandler]",
    helmfile: Helmfile | None,
) -> "Sequence[ConfigHandler]":
    """
    Resolve helmfile release references, filling in chart/repo/version.

    Non-Helm configs and Helm configs without a release reference are returned
    unchanged.

    Args:
        handlers: Config handlers populated by load_configs()
        helmfile: Parsed releases.yaml, or None if not present

    Returns:
        Handlers with all release references resolved

    Raises:
        ValueError: If a release reference cannot be resolved
    """
    for handler in handlers:
        handler.resolve(helmfile)
    return handlers
