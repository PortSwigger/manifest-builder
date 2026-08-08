# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: The manifest-builder contributors
"""Command-line interface for manifest-builder."""

import logging
import sys
from pathlib import Path

import click
from yaml.scanner import ScannerError

from manifest_builder import __version__
from manifest_builder.api import generate
from manifest_builder.discovery import ExternalPlugins
from manifest_builder.generator import ManifestError, setup_logging
from manifest_builder.helm import get_helm_version

logger = logging.getLogger(__name__)


def _format_scanner_error(error: ScannerError, config_dir: Path) -> str:
    mark = error.problem_mark
    if mark is None or mark.name is None:
        return str(error)

    config_path = (Path.cwd() / config_dir).resolve()
    yaml_path = Path(mark.name).resolve()
    try:
        mark.name = str(yaml_path.relative_to(config_path))
    except ValueError:
        mark.name = str(yaml_path)
    return " ".join(str(error).split())


def _extra_plugins(extra_plugins: Path | None) -> ExternalPlugins | None:
    """Describe a --extra-plugins directory for generation."""
    if extra_plugins is None:
        return None
    return ExternalPlugins(path=extra_plugins, source="provided on command line")


def _format_manifest_error(error: ManifestError, output_dir: Path) -> str:
    message = str(error.cause)
    output_path = (Path.cwd() / output_dir).resolve()
    for raw_path in sorted({str(path) for path in output_path.rglob("*.yaml")}):
        try:
            display_path = str(Path(raw_path).resolve().relative_to(output_path))
        except ValueError:
            continue
        message = message.replace(raw_path, display_path)
    return f"Error processing {error.config_name}: {message}"


@click.command()
@click.version_option(version=__version__, prog_name="manifest-builder")
@click.option(
    "--config-dir",
    "-c",
    type=click.Path(exists=False, path_type=Path),
    default=Path("conf"),
    help="Configuration directory",
    show_default=True,
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(exists=False, path_type=Path),
    default=Path("output"),
    help="Output directory for generated manifests",
    show_default=True,
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed output",
)
@click.option(
    "--create-commit",
    is_flag=True,
    help="Create a git commit in the output directory with generated manifests",
)
@click.option(
    "--allow-dirty-config",
    is_flag=True,
    help="Allow creation of commit even if config directory has local changes",
)
@click.option(
    "--vars-from",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="TOML file with extra template variables to merge with [variables] in config.toml",
)
@click.option(
    "--namespace",
    type=str,
    default=None,
    metavar="NAMESPACE",
    help="Declare and generate only one namespace-owned output tree",
)
@click.option(
    "--target",
    type=str,
    default=None,
    metavar="TARGET",
    help="Target to generate, for a config directory that declares targets",
)
@click.option(
    "--extra-plugins",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    metavar="DIRECTORY",
    help="Further plugins directory to load config blocks from, "
    "for config that does not carry its own plugins",
)
def main(
    config_dir: Path,
    output_dir: Path,
    verbose: bool,
    create_commit: bool,
    allow_dirty_config: bool,
    vars_from: Path | None,
    namespace: str | None,
    target: str | None,
    extra_plugins: Path | None,
) -> None:
    """Generate Kubernetes manifests from configuration input."""
    setup_logging(verbose=verbose)

    try:
        # Log helm version
        helm_version = get_helm_version()
        logger.info(f"Using helm {helm_version}")
        generate(
            config_dir,
            output_dir,
            verbose=verbose,
            create_commit=create_commit,
            allow_dirty_config=allow_dirty_config,
            vars_from=vars_from,
            namespace=namespace,
            target=target,
            plugins=_extra_plugins(extra_plugins),
        )

    except ManifestError as e:
        click.echo(_format_manifest_error(e, output_dir), err=True)
        sys.exit(1)
    except ScannerError as e:
        logger.fatal(_format_scanner_error(e, config_dir))
        sys.exit(1)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"Runtime error: {e}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nInterrupted by user", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
