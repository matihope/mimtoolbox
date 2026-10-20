"""Per-machine settings in ~/.mimtoolbox.config (INI), and the `mimtoolbox.py setup` command that writes them."""

from __future__ import annotations

import configparser
import os
import pathlib
from typing import Mapping, Sequence

from mimtoolbox.core import ConfigField, Context, Tool, copy_ssh_key, die, key_login_works, log

COMMON_SECTION: str = "common"
COMMON: Mapping[str, ConfigField] = {
    "lab_user": ConfigField(None, "your MIMUW login (e.g. ab123456)"),
}
HEADER: str = "# mimtoolbox settings for this machine; edit by hand or re-run `mimtoolbox.py setup`.\n\n"


def config_path() -> pathlib.Path:
    """Location of the config file: $MIMTOOLBOX_CONFIG, else ~/.mimtoolbox.config."""
    return pathlib.Path(os.environ.get("MIMTOOLBOX_CONFIG") or pathlib.Path.home() / ".mimtoolbox.config")


def load() -> configparser.ConfigParser:
    """Read the config file; an empty config if it doesn't exist.

    Exits if the file exists but can't be read or parsed.
    """
    path = config_path()
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path) as f:
            cp.read_file(f)
    except FileNotFoundError:
        pass
    except OSError as e:
        die(f"can't read config file {path}: {e.strerror}")
    except configparser.Error as e:
        die(f"bad config file {path}: {e}")
    return cp


def save(cp: configparser.ConfigParser) -> None:
    """Write the config file; exits if it can't be written."""
    path = config_path()
    try:
        with open(path, "w") as f:
            f.write(HEADER)
            cp.write(f)
    except OSError as e:
        die(f"can't write config file {path}: {e.strerror}")
    log(f"wrote {path}")


def resolve(cp: configparser.ConfigParser, section: str, fields: Mapping[str, ConfigField]) -> dict[str, str]:
    """Values of `fields` in `section`, falling back to their defaults ('' for a required one that is unset)."""
    return {key: cp.get(section, key, fallback="") or field.default or "" for key, field in fields.items()}


def common_context(cp: configparser.ConfigParser, tool: Tool | None = None) -> Context:
    """Build the context for running `tool` (or `setup` when None) from the config.

    Exits asking for `mimtoolbox.py setup` if a required common setting is missing.
    """
    common = resolve(cp, COMMON_SECTION, COMMON)
    missing = [key for key, value in common.items() if not value]
    if missing:
        die(f"{', '.join(missing)} not set in {config_path()} - run `mimtoolbox.py setup` first")
    if tool is None:
        return Context("setup", common["lab_user"], {})
    return Context(tool.COMMAND, common["lab_user"], resolve(cp, tool.COMMAND, tool.CONFIG))


def _input(prompt: str) -> str:
    """`input()` that exits cleanly when stdin runs out."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        die("setup aborted")


def _ask(field: ConfigField, current: str) -> str:
    """Ask for one setting; Enter keeps `current`, `-` clears an optional one."""
    while True:
        answer = _input(f"  {field.prompt} [{current}]: ")
        if answer == "-" and field.default is not None:
            return ""
        value = answer or current
        if value or field.default is not None:
            return value
        print("  this one is required")


def _yes(prompt: str) -> bool:
    """Ask a yes/no question that defaults to no."""
    return _input(f"{prompt} [y/N]: ").lower() in ("y", "yes")


def setup(tools: Sequence[Tool]) -> int:
    """Interactively fill in the config file, then offer to set up SSH key logins.

    Only values that differ from the default are stored, so a changed default reaches existing
    configs. Settings for another platform (e.g. macOS-only ones on Linux) are neither asked for nor
    written; values already in the file are kept. Keys that no setting defines (any more) are removed.

    Returns:
        The exit status.
    """
    cp = load()
    sections: list[tuple[str, Mapping[str, ConfigField]]] = [(COMMON_SECTION, COMMON)]
    sections += [(t.COMMAND, t.CONFIG) for t in tools]
    print(f"Settings for {config_path()} (Enter keeps the value in brackets, - clears it).")
    for section, all_fields in sections:
        for key in cp.options(section) if cp.has_section(section) else []:
            if key not in all_fields:  # a setting that no longer exists
                cp.remove_option(section, key)
        fields = {key: field for key, field in all_fields.items() if field.applies_here()}
        if not fields:
            continue
        print(f"[{section}]")
        current = resolve(cp, section, fields)
        if not cp.has_section(section):
            cp.add_section(section)
        for key, field in fields.items():
            value = _ask(field, current[key])
            if value == field.default:  # not stored, so a changed default applies
                cp.remove_option(section, key)
            else:
                cp.set(section, key, value)
        if not cp.options(section):
            cp.remove_section(section)
    save(cp)

    ctx = common_context(cp)
    # The lab PCs share /home (and so ~/.ssh/authorized_keys) with the jump host, so this covers them too.
    if key_login_works(ctx.jump):
        log(f"SSH key login to {ctx.jump} (and so the lab PCs) already works")
    elif _yes(f"Set up SSH key login to {ctx.jump} and the lab PCs?"):
        copy_ssh_key(ctx.jump)
    return 0
