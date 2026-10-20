"""Shared framework for mimtoolbox tools: logging, subprocesses, ssh to the lab PCs, the tool contract."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, NoReturn, Protocol, Sequence

# Prefix of log messages; the dispatcher sets it to the running tool's name.
_prog: str = "mimtoolbox"


def set_prog(name: str) -> None:
    """Set the name that prefixes log messages."""
    global _prog
    _prog = name


def log(msg: str) -> None:
    """Print a message to stderr, prefixed with the running tool's name."""
    print(f"{_prog}: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> NoReturn:
    """Log a message and exit with status 1."""
    log(msg)
    sys.exit(1)


def run(cmd: Sequence[str], dry_run: bool = False, **kw: Any) -> subprocess.CompletedProcess[str]:
    """Run a command in text mode, with stdin from /dev/null unless `input` or `stdin` is given.

    Args:
        cmd: The command and its arguments.
        dry_run: Print the command (shell-quoted) instead of running it, and pretend it succeeded.
        **kw: Passed on to `subprocess.run`.
    """
    if dry_run:
        print(shlex.join(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    if "input" not in kw:
        kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, text=True, **kw)


def quiet(cmd: Sequence[str]) -> bool:
    """Run a command with its output discarded and tell whether it succeeded."""
    return run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


JUMP_HOST: str = "students.mimuw.edu.pl"
"""The host that is reachable from outside and can reach the lab PCs."""


class ConfigField(NamedTuple):
    """One setting in ~/.mimtoolbox.config, as offered by `mimtoolbox.py setup`."""

    default: str | None
    """Value used when the config file doesn't set it; None means the user must provide one."""
    prompt: str
    """Question asked by `setup`."""
    platform: str | None = None
    """`sys.platform` prefix (e.g. 'darwin', 'linux') the setting matters on; None = everywhere."""

    def applies_here(self) -> bool:
        """Whether the setting matters on this machine's platform."""
        return self.platform is None or sys.platform.startswith(self.platform)


@dataclass(frozen=True)
class Context:
    """Everything a tool gets from the framework besides its parsed arguments."""

    tool: str
    """Command name of the running tool; keeps its cache/state files and ssh masters apart."""
    lab_user: str
    """The user's MIMUW login, the same on students and on the lab PCs."""
    settings: Mapping[str, str]
    """The tool's own config section, with defaults filled in for keys the file doesn't set."""

    @property
    def jump(self) -> str:
        """The jump host (students) as an ssh destination (`user@host`)."""
        return f"{self.lab_user}@{JUMP_HOST}"

    @property
    def cache_dir(self) -> pathlib.Path:
        """Directory for data that may be lost at any time (not created)."""
        return pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") / "mimtoolbox"

    @property
    def state_dir(self) -> pathlib.Path:
        """Directory for data that should persist between runs (not created)."""
        return pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local" / "state") \
            / "mimtoolbox"


class Tool(Protocol):
    """What a module in `mimtoolbox.tools` must define to become a `mimtoolbox.py` subcommand.

    The module docstring is shown at the end of the subcommand's --help.
    """

    COMMAND: str
    """Subcommand name."""
    HELP: str
    """One-line description for the help listings."""
    CONFIG: Mapping[str, ConfigField]
    """The tool's settings, stored in the config section named after COMMAND."""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Register the tool's command-line options on its subparser."""

    def main(self, args: argparse.Namespace, ctx: Context) -> int | None:
        """Run the tool; the return value (None = 0) is the exit status."""


def control_path(tool: str) -> str:
    """ssh ControlPath for a tool's master connections.

    Separate per tool, so a tool closing its masters doesn't cut off another tool running meanwhile.
    """
    return os.path.expanduser(f"~/.ssh/cm-mimtoolbox-{tool}-%r@%h")


def lab_ssh_opts(ctx: Context, batch: bool = False, connect_timeout: int | None = None) -> list[str]:
    """ssh/scp options for reaching a lab PC through the jump host.

    One master connection per lab PC is kept for a minute, so a password (if needed at all) is typed
    only once per run.

    Args:
        batch: Fail instead of asking for a password, e.g. on a PC that doesn't take our key yet.
        connect_timeout: Seconds to wait for the connection; ssh's default when None.
    """
    opts = ["-o", f"ProxyJump={ctx.jump}", "-o", "StrictHostKeyChecking=accept-new"]
    if connect_timeout is not None:
        opts += ["-o", f"ConnectTimeout={connect_timeout}"]
    opts += ["-o", "ControlMaster=auto", "-o", f"ControlPath={control_path(ctx.tool)}",
             "-o", "ControlPersist=60"]
    return opts + (["-o", "BatchMode=yes"] if batch else [])


def lab_ssh(ctx: Context, host: str, batch: bool = False, connect_timeout: int | None = None) -> list[str]:
    """The ssh command (without a remote command) for logging in to a lab PC; see `lab_ssh_opts`."""
    return ["ssh", *lab_ssh_opts(ctx, batch, connect_timeout), "-l", ctx.lab_user, host]


def close_master(ctx: Context, host: str, dry_run: bool = False) -> None:
    """Close this tool's master connection to a lab PC, if there is one."""
    run(["ssh", "-o", f"ControlPath={control_path(ctx.tool)}", "-O", "exit", "-l", ctx.lab_user, host],
        dry_run=dry_run, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def booted_pcs(ctx: Context) -> list[str]:
    """Names of the lab PCs that are currently running Linux, as reported by `lk_booted_pcs`."""
    res = run(["ssh", ctx.jump, "lk_booted_pcs"], capture_output=True)
    hosts = re.findall(r"^\|\s*([a-z]+\d+)\s*\|", res.stdout, re.M)
    if not hosts:
        die("no booted PCs reported by lk_booted_pcs" + (f" ({res.stderr.strip()})" if res.stderr else ""))
    return hosts


def pick_pc_interactive(hosts: Sequence[str]) -> str:
    """Let the user choose one of `hosts`, with fzf if installed, else from a numbered list.

    Exits if the user cancels.
    """
    if shutil.which("fzf"):
        res = run(["fzf", "--prompt=lab pc> "], input="\n".join(hosts), stdout=subprocess.PIPE)
        if res.returncode != 0:
            sys.exit(1)
        return res.stdout.strip()
    for i, h in enumerate(hosts, 1):
        print(f"{i:3}) {h}", file=sys.stderr)
    while True:
        try:
            choice = input("lab pc #? ").strip()
        except EOFError:
            sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(hosts):
            return hosts[int(choice) - 1]
        if choice in hosts:
            return choice


def key_login_works(target: str) -> bool:
    """Whether ssh to `target` (`user@host`) gets in with a key, without asking for a password."""
    return quiet(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                  target, "true"])


def copy_ssh_key(target: str) -> None:
    """Add your SSH public key to `target`'s authorized_keys with ssh-copy-id (asks for the password)."""
    log(f"copying key to {target}")
    if run(["ssh-copy-id", "-o", "StrictHostKeyChecking=accept-new", target], stdin=None).returncode != 0:
        die(f"ssh-copy-id to {target} failed")
