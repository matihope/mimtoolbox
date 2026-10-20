"""Tests for the `mimtoolbox.py` dispatcher (mimtoolbox.cli) and tool discovery."""

from __future__ import annotations

import pathlib
import subprocess

from mimtoolbox import cli, config, core, tools
from tests.support import IsolatedTestCase, captured_stdout


class DiscoveryTest(IsolatedTestCase):
    """Finding the tool modules."""

    def test_finds_all_tools(self) -> None:
        """Every module in mimtoolbox/tools with a COMMAND is a tool, sorted by command."""
        self.assertEqual([t.COMMAND for t in tools.discover()], ["lab-display", "print"])

    def test_tools_follow_the_contract(self) -> None:
        """Each tool has the attributes and functions the dispatcher relies on."""
        for tool in tools.discover():
            with self.subTest(tool=tool.COMMAND):
                self.assertTrue(tool.HELP)
                self.assertTrue(tool.__doc__)
                self.assertTrue(callable(tool.add_arguments) and callable(tool.main))
                for key, field in tool.CONFIG.items():
                    self.assertIsInstance(field, core.ConfigField, key)


class DispatchTest(IsolatedTestCase):
    """Parsing the command line and running tools."""

    def test_help_lists_commands(self) -> None:
        """--help lists setup and every tool."""
        with captured_stdout() as out, self.assertExits(0):
            cli.main(["--help"])
        for command in ("setup", "print", "lab-display"):
            self.assertIn(command, out.getvalue())

    def test_tool_help_shows_module_docstring(self) -> None:
        """A tool's --help ends with its module docstring (examples etc.)."""
        with captured_stdout() as out, self.assertExits(0):
            cli.main(["print", "--help"])
        self.assertIn("Printer defaults: A4", out.getvalue())

    def test_command_required(self) -> None:
        """Running without a command is a usage error."""
        with self.assertExits(2):
            cli.main([])

    def test_tool_without_config_asks_for_setup(self) -> None:
        """Tools refuse to run before setup, with the tool's name in the message."""
        with self.assertExits(1):
            cli.main(["print", "--quota"])
        self.assertIn("mimtoolbox print: lab_user not set", self.stderr.getvalue())

    def test_runs_tool_with_context(self) -> None:
        """With a config, the tool runs with its settings; its return value is the exit status."""
        self.write_config("[common]\nlab_user = xy123\n[print]\nprinter = drk\n")
        with captured_stdout() as out:
            status = cli.main(["print", "--status", "--dry-run"])
        self.assertEqual(status, 0)
        self.assertIn("xy123@students.mimuw.edu.pl", out.getvalue())
        self.assertIn("'lpstat -o drk'", out.getvalue())


class SafetyNetTest(IsolatedTestCase):
    """The test isolation itself."""

    def test_processes_are_blocked(self) -> None:
        """Tests can't start real processes, so nothing reaches students or the printer."""
        with self.assertRaises(AssertionError):
            subprocess.run(["true"])
        with self.assertRaises(AssertionError):
            subprocess.Popen(["true"])

    def test_home_is_isolated(self) -> None:
        """The config file and HOME point into the temp dir."""
        self.assertEqual(config.config_path(), self.config_file)
        self.assertEqual(pathlib.Path.home(), self.tmp)
