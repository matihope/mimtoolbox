"""Tests for the shared framework in mimtoolbox.core."""

from __future__ import annotations

import os
import subprocess

from mimtoolbox import core
from tests.support import IsolatedTestCase, captured_stdout, completed, make_ctx


class LoggingTest(IsolatedTestCase):
    """log/die and the program-name prefix."""

    def test_log_uses_prog_prefix(self) -> None:
        """Messages go to stderr, prefixed with the name set by set_prog."""
        core.set_prog("mimtoolbox print")
        core.log("hello")
        self.assertEqual(self.stderr.getvalue(), "mimtoolbox print: hello\n")

    def test_die_logs_and_exits_1(self) -> None:
        """die() logs the message and exits with status 1."""
        with self.assertExits(1):
            core.die("boom")
        self.assertIn("boom", self.stderr.getvalue())


class RunTest(IsolatedTestCase):
    """The subprocess wrapper."""

    def test_dry_run_prints_quoted_command(self) -> None:
        """A dry run prints the shell-quoted command and pretends it succeeded."""
        with captured_stdout() as out:
            res = core.run(["ssh", "host", "echo hi; rm x"], dry_run=True)
        self.assertEqual(out.getvalue(), "ssh host 'echo hi; rm x'\n")
        self.assertEqual(res.returncode, 0)

    def test_stdin_defaults_to_devnull(self) -> None:
        """Without `input`, stdin is /dev/null so commands can't hang waiting for it."""
        sub = self.patch("subprocess.run", return_value=completed())
        core.run(["true"])
        self.assertEqual(sub.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_input_leaves_stdin_alone(self) -> None:
        """With `input`, stdin isn't set (subprocess.run forbids both)."""
        sub = self.patch("subprocess.run", return_value=completed())
        core.run(["cat"], input="data")
        self.assertNotIn("stdin", sub.call_args.kwargs)

    def test_quiet_reports_success(self) -> None:
        """quiet() turns the exit status into a bool."""
        self.patch("subprocess.run", side_effect=[completed(0), completed(1)])
        self.assertTrue(core.quiet(["a"]))
        self.assertFalse(core.quiet(["b"]))


class ConfigFieldTest(IsolatedTestCase):
    """Platform-specific settings."""

    def test_applies_everywhere_by_default(self) -> None:
        """A field without a platform applies on any OS."""
        self.patch("sys.platform", "linux")
        self.assertTrue(core.ConfigField("x", "?").applies_here())

    def test_platform_prefix(self) -> None:
        """A platform-specific field applies only where sys.platform starts with it."""
        self.patch("sys.platform", "linux")
        self.assertTrue(core.ConfigField("x", "?", platform="linux").applies_here())
        self.assertFalse(core.ConfigField("x", "?", platform="darwin").applies_here())


class ContextTest(IsolatedTestCase):
    """Context properties."""

    def test_jump_is_fixed_host_with_user(self) -> None:
        """The jump destination is always students.mimuw.edu.pl, with the user's login."""
        self.assertEqual(make_ctx(lab_user="xy123").jump, "xy123@students.mimuw.edu.pl")

    def test_dirs_follow_xdg(self) -> None:
        """Cache and state dirs live under the XDG dirs."""
        ctx = make_ctx()
        self.assertEqual(ctx.cache_dir, self.tmp / "cache" / "mimtoolbox")
        self.assertEqual(ctx.state_dir, self.tmp / "state" / "mimtoolbox")

    def test_dirs_without_xdg(self) -> None:
        """Without XDG variables, ~/.cache and ~/.local/state are used."""
        del os.environ["XDG_CACHE_HOME"], os.environ["XDG_STATE_HOME"]
        ctx = make_ctx()
        self.assertEqual(ctx.cache_dir, self.tmp / ".cache" / "mimtoolbox")
        self.assertEqual(ctx.state_dir, self.tmp / ".local" / "state" / "mimtoolbox")


class SshOptionsTest(IsolatedTestCase):
    """The ssh command lines for reaching lab PCs."""

    def test_lab_ssh(self) -> None:
        """Lab PCs are reached through the jump host with a per-tool master connection."""
        cmd = core.lab_ssh(make_ctx("print"), "red01")
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-3:], ["-l", "ab123456", "red01"])
        self.assertIn("ProxyJump=ab123456@students.mimuw.edu.pl", cmd)
        self.assertIn("ControlPath=" + os.path.expanduser("~/.ssh/cm-mimtoolbox-print-%r@%h"), cmd)
        self.assertNotIn("BatchMode=yes", cmd)
        self.assertFalse(any(o.startswith("ConnectTimeout") for o in cmd))

    def test_batch_and_timeout(self) -> None:
        """batch adds BatchMode, connect_timeout adds ConnectTimeout."""
        opts = core.lab_ssh_opts(make_ctx(), batch=True, connect_timeout=8)
        self.assertIn("BatchMode=yes", opts)
        self.assertIn("ConnectTimeout=8", opts)

    def test_tools_get_separate_masters(self) -> None:
        """Each tool has its own ControlPath, so one closing its master can't break another."""
        self.assertNotEqual(core.control_path("print"), core.control_path("lab-display"))


class BootedPcsTest(IsolatedTestCase):
    """Parsing lk_booted_pcs."""

    TABLE = ("+----------+------------+\n| Name     | Address    |\n+----------+------------+\n"
             "| black00  | 10.1.1.233 |\n| red16    | 10.1.1.36  |\n+----------+------------+\n")

    def test_parses_table(self) -> None:
        """Host names are taken from the first column, skipping the header."""
        run = self.patch("mimtoolbox.core.run", return_value=completed(stdout=self.TABLE))
        self.assertEqual(core.booted_pcs(make_ctx()), ["black00", "red16"])
        self.assertEqual(run.call_args.args[0], ["ssh", "ab123456@students.mimuw.edu.pl", "lk_booted_pcs"])

    def test_none_booted_exits(self) -> None:
        """An empty list (or a failed ssh) is an error, reported with ssh's message."""
        self.patch("mimtoolbox.core.run", return_value=completed(1, stderr="Permission denied"))
        with self.assertExits(1):
            core.booted_pcs(make_ctx())
        self.assertIn("Permission denied", self.stderr.getvalue())


class DesktopPcsTest(IsolatedTestCase):
    """Finding PCs with your desktop login from ~/.Xauthority."""

    HISTORY = ("brown09/unix:0\nstudents/unix:98\nstudents.mimuw.edu.pl:98\nwhite02/unix:0\nred01/unix:0\n"
               "orange02/unix:0\nwhite02/unix:0\n")

    def test_booted_logins_newest_first(self) -> None:
        """Lab PCs from the history that are booted, latest login first, without duplicates or students."""
        out = self.HISTORY + "=== lk_booted_pcs ===\n" + BootedPcsTest.TABLE.replace("black00", "white02").replace(
            "red16", "brown09") + "| red01    | 10.1.1.21  |\n"
        run = self.patch("mimtoolbox.core.run", return_value=completed(stdout=out))
        self.assertEqual(core.desktop_pcs(make_ctx()), ["white02", "red01", "brown09"])
        remote = run.call_args.args[0]
        self.assertEqual(remote[:2], ["ssh", "ab123456@students.mimuw.edu.pl"])
        self.assertIn("xauth -f ~/.Xauthority list", remote[2])
        self.assertIn("lk_booted_pcs", remote[2])

    def test_no_xauthority(self) -> None:
        """Without a login history there are no candidates."""
        self.patch("mimtoolbox.core.run", return_value=completed(stdout="=== lk_booted_pcs ===\n" + BootedPcsTest.TABLE))
        self.assertEqual(core.desktop_pcs(make_ctx()), [])

    def test_ssh_failure_exits(self) -> None:
        """Failing to reach students is reported."""
        self.patch("mimtoolbox.core.run", return_value=completed(255, stderr="Connection refused"))
        with self.assertExits(1):
            core.desktop_pcs(make_ctx())
        self.assertIn("Connection refused", self.stderr.getvalue())


class KnownPCsTest(IsolatedTestCase):
    """The shared list of lab PCs that took the key."""

    def test_starts_empty(self) -> None:
        """Without a cache file there are no known PCs."""
        self.assertEqual(core.KnownPCs(make_ctx()).hosts, [])

    def test_remember_moves_to_front_and_persists(self) -> None:
        """remember() puts the PC first, without duplicates, and saves the list."""
        known = core.KnownPCs(make_ctx())
        for h in ("a", "b", "a"):
            known.remember(h)
        self.assertEqual(core.KnownPCs(make_ctx()).hosts, ["a", "b"])

    def test_shared_between_tools(self) -> None:
        """All tools read the same list."""
        core.KnownPCs(make_ctx("print")).remember("a")
        self.assertEqual(core.KnownPCs(make_ctx("lab-display")).hosts, ["a"])

    def test_limit(self) -> None:
        """Only the LIMIT most recent PCs are kept."""
        known = core.KnownPCs(make_ctx())
        for i in range(core.KnownPCs.LIMIT + 5):
            known.remember(f"pc{i}")
        self.assertEqual(len(core.KnownPCs(make_ctx()).hosts), core.KnownPCs.LIMIT)
        self.assertEqual(known.hosts[0], f"pc{core.KnownPCs.LIMIT + 4}")

    def test_forget(self) -> None:
        """forget() removes a PC; forgetting an unknown one is harmless."""
        known = core.KnownPCs(make_ctx())
        known.remember("a")
        known.remember("b")
        known.forget("a")
        known.forget("zzz")
        self.assertEqual(core.KnownPCs(make_ctx()).hosts, ["b"])

    def test_unwritable_cache_is_ignored(self) -> None:
        """A cache that can't be written doesn't break the tool."""
        (self.tmp / "cache").write_text("a file where the cache dir should be")
        core.KnownPCs(make_ctx()).remember("a")  # must not raise


class PickPcTest(IsolatedTestCase):
    """Choosing a lab PC interactively without fzf."""

    def setUp(self) -> None:
        """Pretend fzf isn't installed."""
        super().setUp()
        self.patch("shutil.which", return_value=None)

    def test_by_number_or_name(self) -> None:
        """The user can answer with the list number or the host name; invalid answers are asked again."""
        self.patch("builtins.input", side_effect=["2"])
        self.assertEqual(core.pick_pc_interactive(["a", "b"]), "b")
        self.patch("builtins.input", side_effect=["9", "nope", "a"])
        self.assertEqual(core.pick_pc_interactive(["a", "b"]), "a")

    def test_eof_exits(self) -> None:
        """Ctrl-D cancels."""
        self.patch("builtins.input", side_effect=EOFError)
        with self.assertExits(1):
            core.pick_pc_interactive(["a"])


class SshKeyTest(IsolatedTestCase):
    """Checking and installing the SSH key."""

    def test_key_login_works_uses_batch_mode(self) -> None:
        """The check never prompts for a password."""
        quiet = self.patch("mimtoolbox.core.quiet", return_value=True)
        self.assertTrue(core.key_login_works("u@h"))
        self.assertIn("BatchMode=yes", quiet.call_args.args[0])

    def test_copy_ssh_key_failure_exits(self) -> None:
        """A failing ssh-copy-id is reported."""
        self.patch("mimtoolbox.core.run", return_value=completed(1))
        with self.assertExits(1):
            core.copy_ssh_key("u@h")
        self.assertIn("ssh-copy-id to u@h failed", self.stderr.getvalue())
