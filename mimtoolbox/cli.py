"""The `mimtoolbox.py` command line: one subcommand per tool, plus `setup`."""

from __future__ import annotations

import argparse
from typing import Sequence

from mimtoolbox import config, tools
from mimtoolbox.core import Tool, set_prog


def build_parser(tool_list: Sequence[Tool]) -> argparse.ArgumentParser:
    """Create the top-level parser with a subparser per tool; the chosen tool ends up in `args._tool`."""
    ap = argparse.ArgumentParser(
        prog="mimtoolbox.py",
        description="Tools for using the MIMUW lab computers from your own machine.",
        epilog=f"Run `mimtoolbox.py setup` once on each machine; settings live in {config.config_path()}.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")
    setup = sub.add_parser("setup", help="create or update the per-machine config, set up SSH keys",
                           description="Create or update the per-machine config and set up SSH key logins.")
    setup.set_defaults(_tool=None)
    for tool in tool_list:
        p = sub.add_parser(tool.COMMAND, help=tool.HELP, description=tool.HELP, epilog=tool.__doc__,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        tool.add_arguments(p)
        p.set_defaults(_tool=tool)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the chosen tool.

    Returns:
        The exit status.
    """
    tool_list = tools.discover()
    args = build_parser(tool_list).parse_args(argv)
    tool: Tool | None = args._tool
    if tool is None:
        set_prog("mimtoolbox setup")
        return config.setup(tool_list)
    set_prog(f"mimtoolbox {tool.COMMAND}")
    ctx = config.common_context(config.load(), tool)
    return tool.main(args, ctx) or 0
