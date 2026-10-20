"""Tests for the per-machine config file and `mimtoolbox.py setup` (mimtoolbox.config)."""

from __future__ import annotations

import configparser
import os
import unittest
from typing import Any

from mimtoolbox import config, tools
from mimtoolbox.tools import printer
from tests.support import IsolatedTestCase, captured_stdout


class LoadTest(IsolatedTestCase):
    """Reading the config file."""

    def test_missing_file_is_empty(self) -> None:
        """No file means an empty config, not an error."""
        self.assertEqual(config.load().sections(), [])

    def test_reads_file(self) -> None:
        """Values are read from $MIMTOOLBOX_CONFIG."""
        self.write_config("[common]\nlab_user = xy123\n")
        self.assertEqual(config.load().get("common", "lab_user"), "xy123")

    def test_default_location(self) -> None:
        """Without $MIMTOOLBOX_CONFIG, the file is ~/.mimtoolbox.config."""
        self.patch_dict(os.environ, {"MIMTOOLBOX_CONFIG": ""})
        self.assertEqual(config.config_path(), self.tmp / ".mimtoolbox.config")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read any file")
    def test_unreadable_file_exits(self) -> None:
        """A file that exists but can't be read is an error, not a missing config."""
        self.write_config("[common]\n")
        self.config_file.chmod(0)
        self.addCleanup(self.config_file.chmod, 0o600)
        with self.assertExits(1):
            config.load()
        self.assertIn("can't read config file", self.stderr.getvalue())

    def test_directory_exits(self) -> None:
        """A directory in place of the file is reported."""
        self.config_file.mkdir()
        with self.assertExits(1):
            config.load()
        self.assertIn("can't read config file", self.stderr.getvalue())

    def test_malformed_file_exits(self) -> None:
        """A file that isn't valid INI is reported."""
        self.write_config("lab_user = xy123\n")
        with self.assertExits(1):
            config.load()
        self.assertIn("bad config file", self.stderr.getvalue())


class SaveTest(IsolatedTestCase):
    """Writing the config file."""

    def test_writes_header_and_values(self) -> None:
        """The file starts with an explanatory comment and reads back the same."""
        cp = configparser.ConfigParser()
        cp["common"] = {"lab_user": "xy123"}
        config.save(cp)
        text = self.config_file.read_text()
        self.assertTrue(text.startswith("# mimtoolbox settings"))
        self.assertEqual(config.load().get("common", "lab_user"), "xy123")

    def test_unwritable_path_exits(self) -> None:
        """A path in a missing directory is reported instead of crashing."""
        self.patch_dict(os.environ, {"MIMTOOLBOX_CONFIG": str(self.tmp / "missing" / "cfg")})
        with self.assertExits(1):
            config.save(configparser.ConfigParser())
        self.assertIn("can't write config file", self.stderr.getvalue())


class ContextTest(IsolatedTestCase):
    """Building the context for a tool from the config."""

    def test_missing_lab_user_asks_for_setup(self) -> None:
        """Without lab_user, tools refuse to run and point to setup."""
        with self.assertExits(1):
            config.common_context(config.load(), printer)
        self.assertIn("run `mimtoolbox.py setup` first", self.stderr.getvalue())

    def test_tool_settings_with_defaults(self) -> None:
        """A tool gets its own section, with defaults for unset keys."""
        self.write_config("[common]\nlab_user = xy123\n[print]\nprinter = drk\n")
        ctx = config.common_context(config.load(), printer)
        self.assertEqual((ctx.tool, ctx.lab_user, dict(ctx.settings)), ("print", "xy123", {"printer": "drk"}))
        self.write_config("[common]\nlab_user = xy123\n")
        self.assertEqual(config.common_context(config.load(), printer).settings, {"printer": ""})


class SetupTest(IsolatedTestCase):
    """The interactive `setup` command."""

    def setUp(self) -> None:
        """Run as on macOS, with key login already working (so no key question)."""
        super().setUp()
        self.patch("sys.platform", "darwin")
        self.key_works = self.patch("mimtoolbox.config.key_login_works", return_value=True)
        self.copy_key = self.patch("mimtoolbox.config.copy_ssh_key")

    def run_setup(self, *answers: str) -> list[str]:
        """Run setup with the given answers; return the prompts it showed."""
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            """Record the prompt and give the next answer (EOF when they run out)."""
            prompts.append(prompt)
            if not remaining:
                raise EOFError
            return remaining.pop(0)

        remaining = list(answers)
        self.patch("builtins.input", side_effect=answer)
        with captured_stdout():
            self.assertEqual(config.setup(tools.discover()), 0)
        return prompts

    def saved(self) -> dict[str, dict[str, str]]:
        """The config file's contents as nested dicts."""
        cp = config.load()
        return {s: dict(cp[s]) for s in cp.sections()}

    def test_fresh_setup_on_macos(self) -> None:
        """Asks lab_user and the macOS settings (not the Linux encoder), and writes them."""
        prompts = self.run_setup("xy123", "", "", "", "")
        self.assertEqual(len(prompts), 5)
        self.assertTrue(any("HiDPI" in p for p in prompts))
        self.assertFalse(any("GStreamer" in p for p in prompts))
        self.assertEqual(self.saved(), {
            "common": {"lab_user": "xy123"},
            "lab-display": {"fps": "30", "bitrate": "8M", "hidpi": "off"},
            "print": {"printer": ""}})

    def test_linux_asks_encoder_not_hidpi(self) -> None:
        """On Linux the macOS-only setting is skipped and the Linux one asked."""
        self.patch("sys.platform", "linux")
        prompts = self.run_setup("xy123", "", "", "x264enc", "")
        self.assertFalse(any("HiDPI" in p for p in prompts))
        self.assertEqual(self.saved()["lab-display"]["encoder"], "x264enc")

    def test_lab_user_is_required(self) -> None:
        """An empty lab_user is asked again."""
        prompts = self.run_setup("", "xy123", "", "", "", "")
        self.assertEqual(sum("MIMUW login" in p for p in prompts), 2)
        self.assertEqual(self.saved()["common"]["lab_user"], "xy123")

    def test_enter_keeps_and_dash_clears(self) -> None:
        """Enter keeps the current value (shown in brackets), `-` clears an optional one."""
        self.write_config("[common]\nlab_user = xy123\n[print]\nprinter = drk\n[lab-display]\nfps = 60\n")
        prompts = self.run_setup("", "", "", "", "-")
        self.assertIn("[xy123]", prompts[0])
        self.assertEqual(self.saved()["lab-display"]["fps"], "60")
        self.assertEqual(self.saved()["print"]["printer"], "")

    def test_keeps_other_platform_values_drops_unknown_keys(self) -> None:
        """A Linux-only value survives setup on macOS; keys no setting defines are removed."""
        self.write_config("[common]\nlab_user = xy123\njump = old.example\n[lab-display]\nencoder = x264enc\n")
        self.run_setup("", "", "", "", "")
        saved = self.saved()
        self.assertNotIn("jump", saved["common"])
        self.assertEqual(saved["lab-display"]["encoder"], "x264enc")

    def test_skips_key_question_when_key_works(self) -> None:
        """No key question when key login to students already works."""
        prompts = self.run_setup("xy123", "", "", "", "")
        self.assertFalse(any("SSH key" in p for p in prompts))
        self.key_works.assert_called_once_with("xy123@students.mimuw.edu.pl")
        self.assertIn("already works", self.stderr.getvalue())

    def test_offers_key_copy_when_needed(self) -> None:
        """Without key login, setup offers to copy the key; 'y' runs ssh-copy-id, Enter skips it."""
        self.key_works.return_value = False
        self.run_setup("xy123", "", "", "", "", "y")
        self.copy_key.assert_called_once_with("xy123@students.mimuw.edu.pl")
        self.copy_key.reset_mock()
        self.run_setup("", "", "", "", "", "")
        self.copy_key.assert_not_called()

    def test_eof_aborts_without_writing(self) -> None:
        """Running out of input aborts before anything is written."""
        def eof(prompt: Any) -> str:
            """Simulate Ctrl-D."""
            raise EOFError

        self.patch("builtins.input", side_effect=eof)
        with captured_stdout(), self.assertExits(1):
            config.setup(tools.discover())
        self.assertFalse(self.config_file.exists())
