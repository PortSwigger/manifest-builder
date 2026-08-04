# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Configuration parsing and validation for manifest-builder."""

import tomllib
from collections.abc import Collection
from dataclasses import dataclass
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

#: Names a section directory's config file may take. ``section.toml`` is the
#: canonical one, and says plainly that the file is part of a larger config
#: directory rather than the top of one. The top-level names are also accepted,
#: since a section holds what a top-level config file used to.
SECTION_FILE_NAMES = ("section.toml", *CONFIG_FILE_NAMES)

#: Config directory layout in which the top-level file declares config blocks
#: directly. Assumed when the top-level file states no ``version``.
BLOCKS_VERSION = 1

#: Config directory layout in which the top-level file declares targets, and the
#: config blocks live in per-section subdirectories.
TARGETS_VERSION = 2


@dataclass(frozen=True)
class Target:
    """A named set of sections rendered together with one set of variables.

    Targets let one config directory describe several deployments of the same
    sections: each names the section directories it is built from, and carries
    the variables those sections are rendered with.
    """

    name: str
    sections: tuple[str, ...]
    variables: dict[str, TemplateValue]


def find_config_file(
    config_dir: Path, names: Collection[str] = CONFIG_FILE_NAMES
) -> Path:
    """Return the first of ``names`` that exists in a config directory.

    Raises:
        FileNotFoundError: If the directory holds none of ``names``.
    """
    for name in names:
        candidate = config_dir / name
        if candidate.exists():
            return candidate

    expected = " or ".join(str(config_dir / name) for name in names)
    raise FileNotFoundError(f"Configuration file not found: {expected}")


def config_version(data: dict, source_file: Path) -> int:
    """Return the declared config directory layout version.

    A file that states no ``version`` declares config blocks directly, the
    layout that predates targets.
    """
    version = data.get("version", BLOCKS_VERSION)
    # bool is an int subclass, and `version = true` is not a version.
    if isinstance(version, bool) or version not in (BLOCKS_VERSION, TARGETS_VERSION):
        raise ValueError(
            f"Unsupported config version {version!r} in {source_file}: "
            f"expected {BLOCKS_VERSION} or {TARGETS_VERSION}"
        )
    return version


def parse_targets(data: dict, source_file: Path) -> list[Target]:
    """Parse the ``[[target]]`` entries of a targets-style top-level config."""
    raw_targets = data.get("target")
    if raw_targets is None:
        raise ValueError(f"No [[target]] entries found in {source_file}")
    if not isinstance(raw_targets, list):
        raise ValueError(f"'target' must be a list of tables in {source_file}")

    targets: list[Target] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise ValueError(f"Each [[target]] entry must be a table in {source_file}")
        target = _parse_target(item, source_file, index)
        if target.name in seen:
            raise ValueError(f"Duplicate target '{target.name}' in {source_file}")
        seen.add(target.name)
        targets.append(target)

    return targets


def _parse_target(data: dict, source_file: Path, table_index: int) -> Target:
    """Parse one ``[[target]]`` entry."""
    validate_known_fields(
        "[[target]]", data, {"name", "sections", "vars"}, source_file, table_index
    )

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"Each [[target]] entry must set a non-empty 'name' in {source_file}"
        )

    return Target(
        name=name,
        sections=_parse_sections(data.get("sections"), name, source_file),
        variables=parse_variables(data.get("vars"), source_file),
    )


def _parse_sections(
    data: object, target_name: str, source_file: Path
) -> tuple[str, ...]:
    """Parse a target's ``sections`` field into validated section names."""
    if data is None:
        raise ValueError(f"Target '{target_name}' in {source_file} must set 'sections'")

    raw_names = [data] if isinstance(data, str) else data
    if not isinstance(raw_names, list):
        raise ValueError(
            f"'sections' must be a string or list of strings for target "
            f"'{target_name}' in {source_file}"
        )

    names: list[str] = []
    seen: set[str] = set()
    for name in raw_names:
        if not isinstance(name, str):
            raise ValueError(
                f"'sections' must be a string or list of strings for target "
                f"'{target_name}' in {source_file}"
            )
        # A section names a directory in the config directory, so anything that
        # could reach outside it, or hide, is rejected rather than resolved.
        if not name or name.startswith(".") or Path(name).parts != (name,):
            raise ValueError(
                f"Invalid section name {name!r} for target '{target_name}' in "
                f"{source_file}: a section is a directory in the config directory"
            )
        if name in seen:
            raise ValueError(
                f"Target '{target_name}' in {source_file} lists section "
                f"'{name}' more than once"
            )
        seen.add(name)
        names.append(name)

    if not names:
        raise ValueError(
            f"Target '{target_name}' in {source_file} must list at least one section"
        )

    return tuple(names)


def select_target(targets: list[Target], name: str | None, source_file: Path) -> Target:
    """Pick the requested target, reporting what is on offer when it is absent."""
    available = ", ".join(repr(target.name) for target in targets)
    if name is None:
        raise ValueError(
            f"{source_file} declares targets, so one must be selected. "
            f"Available targets: {available}"
        )

    for target in targets:
        if target.name == name:
            return target

    raise ValueError(
        f"Unknown target '{name}' in {source_file}. Available targets: {available}"
    )


def find_section_config_file(
    config_dir: Path, section: str, target_name: str, source_file: Path
) -> Path:
    """Return the config file of one section directory."""
    section_dir = config_dir / section
    if not section_dir.is_dir():
        raise FileNotFoundError(
            f"Section directory not found for target '{target_name}': {section_dir} "
            f"(referenced in {source_file})"
        )

    try:
        return find_config_file(section_dir, SECTION_FILE_NAMES)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"{e} for section '{section}'") from e


def merge_variables(
    base: dict[str, TemplateValue],
    base_description: str,
    extra: dict[str, TemplateValue],
    extra_description: str,
) -> dict[str, TemplateValue]:
    """Merge two variable tables, rejecting names that both define.

    Neither source wins a name defined twice: a variable with two competing
    values is a config mistake worth reporting rather than resolving.
    """
    overlap = sorted(set(base) & set(extra))
    if overlap:
        names = ", ".join(repr(name) for name in overlap)
        suffix = "s" if len(overlap) != 1 else ""
        raise ValueError(
            f"Variable{suffix} {names} defined in both {base_description} "
            f"and {extra_description}"
        )
    return {**base, **extra}


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
    target: str | None = None,
) -> "Sequence[ConfigHandler]":
    """
    Load app configurations from the config directory.

    The top-level TOML config file may be named ``config.toml`` or
    ``manifest-builder.toml``. It comes in two layouts, told apart by its
    ``version`` field:

    ``version = 1``, or no ``version`` at all, declares config blocks directly
    as top-level tables owned by the supplied config handlers.

    ``version = 2`` instead declares ``[[target]]`` entries. Each target names
    the sections it is built from and the variables they are rendered with, and
    each section is a subdirectory of ``config_dir`` holding a config file of
    blocks. Since a section's blocks are read from the section's own config
    file, the paths they reference resolve inside the section directory.

    Args:
        config_dir: Directory containing TOML configuration files
        handlers: Config handlers to populate
        extra_variables: Additional template variables merged into the
            variables a config block is rendered with. Keys that overlap with
            variables from the config directory are rejected with ValueError.
        default_namespace: Namespace to use when a config entry omits its
            ``namespace`` field.
        default_image: Image override passed to config handlers in namespace mode.
        target: Name of the target to load, for a ``version = 2`` config
            directory. Required for those, and rejected for the older layout,
            which has no targets to choose between.

    Returns:
        Handlers populated with the config items they own

    Raises:
        FileNotFoundError: If config_dir, a top-level config file, or a
            referenced section doesn't exist
        ValueError: If TOML is invalid or missing required fields
    """
    if not config_dir.exists():
        raise FileNotFoundError(f"Configuration directory not found: {config_dir}")

    if not config_dir.is_dir():
        raise ValueError(f"Configuration path is not a directory: {config_dir}")

    toml_file = find_config_file(config_dir)
    data = load_toml_file(toml_file)

    handler_by_name: dict[str, ConfigHandler] = {}
    for handler in handlers:
        name = handler.top_level_config_name()
        if name in handler_by_name:
            raise ValueError(f"Duplicate config handler for top-level key '{name}'")
        handler_by_name[name] = handler
    if not handler_by_name:
        raise ValueError("No config handlers registered")

    if config_version(data, toml_file) == TARGETS_VERSION:
        _load_target_sections(
            config_dir,
            toml_file,
            data,
            handler_by_name,
            target,
            extra_variables,
            default_namespace,
            default_image,
        )
        return handlers

    if target is not None:
        raise ValueError(
            f"Cannot select target '{target}': {toml_file} declares config blocks "
            f"directly rather than version = {TARGETS_VERSION} targets"
        )

    variables = merge_variables(
        parse_variables(data.get("variables"), toml_file),
        str(toml_file),
        extra_variables or {},
        "the --vars-from file",
    )
    _load_config_blocks(
        toml_file,
        data,
        handler_by_name,
        variables,
        default_namespace,
        default_image,
        extra_allowed_fields={"version"},
    )

    return handlers


def _load_target_sections(
    config_dir: Path,
    toml_file: Path,
    data: dict,
    handler_by_name: "dict[str, ConfigHandler]",
    target: str | None,
    extra_variables: dict[str, TemplateValue] | None,
    default_namespace: str | None,
    default_image: str | None,
) -> None:
    """Load the config blocks of every section a selected target names."""
    unknown_top_level = sorted(set(data) - {"version", "target"})
    if unknown_top_level:
        fields = ", ".join(
            _format_field_location(field, toml_file) for field in unknown_top_level
        )
        suffix = "s" if len(unknown_top_level) != 1 else ""
        raise ValueError(
            f"Unknown top-level field{suffix}: {fields} in {toml_file}. A "
            f"version = {TARGETS_VERSION} config file declares only targets; "
            "config blocks belong in a section directory"
        )

    selected = select_target(parse_targets(data, toml_file), target, toml_file)
    target_description = f"target '{selected.name}' in {toml_file}"

    for section in selected.sections:
        section_file = find_section_config_file(
            config_dir, section, selected.name, toml_file
        )
        section_data = load_toml_file(section_file)

        variables = merge_variables(
            selected.variables,
            target_description,
            parse_variables(section_data.get("variables"), section_file),
            str(section_file),
        )
        variables = merge_variables(
            variables,
            "the config directory",
            extra_variables or {},
            "the --vars-from file",
        )

        _load_config_blocks(
            section_file,
            section_data,
            handler_by_name,
            variables,
            default_namespace,
            default_image,
        )


def _load_config_blocks(
    toml_file: Path,
    data: dict,
    handler_by_name: "dict[str, ConfigHandler]",
    variables: dict[str, TemplateValue],
    default_namespace: str | None,
    default_image: str | None,
    extra_allowed_fields: Collection[str] = (),
) -> None:
    """Hand the config blocks in one TOML file to the handlers that own them."""
    allowed_top_level = set(handler_by_name) | {"variables"} | set(extra_allowed_fields)
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

    # Hand handlers the resolved variables, so a block is rendered with its
    # target's and section's variables regardless of which file declared them.
    data["variables"] = variables
    for name in present_handler_names:
        handler_by_name[name].load_config(
            data[name], toml_file, data, default_namespace, default_image
        )


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
