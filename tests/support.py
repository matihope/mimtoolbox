"""Shared test helpers: an isolated environment and shortcuts for building contexts and parsing arguments."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import pathlib
import subprocess
import tempfile
import unittest
from typing import Any, Iterator, Sequence
from unittest import mock

from mimtoolbox import cli, core, tools


def _no_processes(*args: Any, **kwargs: Any) -> Any:
    """Stand-in for subprocess.run/Popen that fails the test instead of starting anything."""
    raise AssertionError(f"tests must not start processes, got: {args[0] if args else kwargs}")


class IsolatedTestCase(unittest.TestCase):
    """A test case that can't touch the real machine or network.

    HOME, the config file and the XDG cache/state dirs point into a fresh temp dir, starting processes
    fails the test, and log output (stderr) is captured in `self.stderr`.
    """

    def setUp(self) -> None:
        """Create the temp dir, redirect the environment and block subprocesses."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.config_file = self.tmp / "mimtoolbox.config"
        env = {"HOME": str(self.tmp), "MIMTOOLBOX_CONFIG": str(self.config_file),
               "XDG_CACHE_HOME": str(self.tmp / "cache"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.patch_dict(os.environ, env)
        self.patch("subprocess.run", side_effect=_no_processes)
        self.patch("subprocess.Popen", side_effect=_no_processes)
        self.stderr = io.StringIO()
        self.patch("sys.stderr", self.stderr)
        core.set_prog("mimtoolbox")

    def patch(self, target: str, *args: Any, **kwargs: Any) -> Any:
        """`mock.patch(target, ...)` for the rest of the test; returns the replacement."""
        patcher = mock.patch(target, *args, **kwargs)
        replacement = patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def patch_dict(self, target: Any, values: dict[str, Any]) -> None:
        """`mock.patch.dict(target, values)` for the rest of the test."""
        patcher = mock.patch.dict(target, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, text: str) -> None:
        """Write the (isolated) config file."""
        self.config_file.write_text(text)

    def assertExits(self, status: int = 1) -> contextlib.AbstractContextManager[Any]:
        """Context manager asserting that the block calls sys.exit(status)."""
        return _Exits(self, status)


class _Exits:
    """Implementation of `IsolatedTestCase.assertExits`."""

    def __init__(self, case: unittest.TestCase, status: int) -> None:
        """Remember what to check."""
        self.case, self.status = case, status

    def __enter__(self) -> None:
        """Nothing to set up."""

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Check that SystemExit with the expected status was raised, and swallow it."""
        self.case.assertIs(exc_type, SystemExit, "expected sys.exit()")
        self.case.assertEqual(exc.code, self.status)
        return True


def make_ctx(tool: str = "print", lab_user: str = "ab123456", **settings: str) -> core.Context:
    """A context as the dispatcher would build it."""
    return core.Context(tool, lab_user, settings)


def parse(argv: Sequence[str]) -> argparse.Namespace:
    """Parse a `mimtoolbox.py` command line with the real parser."""
    return cli.build_parser(tools.discover()).parse_args(list(argv))


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """A finished process result, for stubbing `run`."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@contextlib.contextmanager
def captured_stdout() -> Iterator[io.StringIO]:
    """Capture what's printed to stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
