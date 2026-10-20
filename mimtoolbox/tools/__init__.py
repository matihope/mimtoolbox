"""The mimtoolbox subcommands: every module here that defines COMMAND (see `mimtoolbox.core.Tool`)."""

from __future__ import annotations

import importlib
import pkgutil
from typing import cast

from mimtoolbox.core import Tool


def discover() -> list[Tool]:
    """Import all tool modules in this package, sorted by command name."""
    tools = []
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(module, "COMMAND"):
            tools.append(cast(Tool, module))
    return sorted(tools, key=lambda t: t.COMMAND)
