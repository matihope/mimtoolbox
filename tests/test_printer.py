"""Tests for the `print` tool (mimtoolbox.tools.printer). Nothing is ever sent to a printer."""

from __future__ import annotations

import shlex
from typing import Any, Collection, Sequence

from mimtoolbox import core
from mimtoolbox.tools import printer
from tests.support import IsolatedTestCase, captured_stdout, completed, make_ctx, parse


def lp(argv: Sequence[str]) -> str:
    """The lp command (as a shell string) for a `print` command line."""
    return shlex.join(printer.lp_args(parse(["print", *argv])))


class LpArgsTest(IsolatedTestCase):
    """Translating options into an lp command."""

    def test_no_options(self) -> None:
        """Without options, lp runs with the printer's defaults."""
        self.assertEqual(lp([]), "lp")

    def test_matches_old_mimprint(self) -> None:
        """Same lp command as the original mimprint.py for the same options."""
        self.assertEqual(lp(["-N", "2", "-t", "some.pdf"]), "lp -t some.pdf -o number-up=2")
        self.assertEqual(lp(["-1", "-n", "2", "-c", "-o", "foo=bar", "-Q", "high", "-l"]),
                         "lp -n 2 -o sides=one-sided -o Duplex=None -o orientation-requested=4 -o collate=true "
                         "-o print-quality=5 -o foo=bar")

    def test_sides_set_driver_duplex_too(self) -> None:
        """--sides sets both the IPP option and the driver's Duplex, which the printer actually obeys."""
        self.assertEqual(lp(["-s", "short"]), "lp -o sides=two-sided-short-edge -o Duplex=DuplexTumble")
        self.assertEqual(lp(["-s", "long"]), "lp -o sides=two-sided-long-edge -o Duplex=DuplexNoTumble")

    def test_job_options(self) -> None:
        """Printer, copies, pages, title and priority map to lp's own flags."""
        self.assertEqual(lp(["-d", "drk", "-n", "3", "-P", "1-4,7", "-t", "My doc", "-q", "50"]),
                         "lp -d drk -n 3 -P 1-4,7 -t 'My doc' -q 50")

    def test_layout_and_text_options(self) -> None:
        """The remaining options become -o options."""
        self.assertEqual(
            lp(["--number-up-layout", "tblr", "--page-border", "single", "-m", "A3", "-f", "--page-set", "odd",
                "-r", "--mirror", "--cpi", "12", "--lpi", "8", "--prettyprint", "-O", "reverse-portrait"]),
            "lp -o number-up-layout=tblr -o page-border=single -o orientation-requested=6 -o media=A3 "
            "-o fit-to-page -o page-set=odd -o outputorder=reverse -o mirror -o cpi=12.0 -o lpi=8.0 -o prettyprint")


class MainTest(IsolatedTestCase):
    """Argument checks and the dry run of a whole print job."""

    def setUp(self) -> None:
        """Create two files to print."""
        super().setUp()
        self.pdf = self.tmp / "some.pdf"
        self.pdf.write_text("%PDF-1.4\n")
        self.txt = self.tmp / "b.txt"
        self.txt.write_text("x")

    def run_main(self, argv: Sequence[str], **settings: str) -> list[list[str]]:
        """Run `print` with argv; return the dry-run commands, split into words."""
        settings.setdefault("printer", "")
        with captured_stdout() as out:
            printer.main(parse(["print", *argv]), make_ctx(**settings))
        return [shlex.split(line) for line in out.getvalue().splitlines()]

    def test_dry_run_job(self) -> None:
        """A job makes a job dir, copies the files, runs lp, cleans up and closes the master."""
        cmds = self.run_main([str(self.pdf), str(self.txt), "-N", "2", "--dry-run"])
        self.assertEqual([c[0] for c in cmds], ["ssh", "scp", "ssh", "ssh", "ssh"])
        self.assertEqual(cmds[0][-1], "mkdir -p prints && mktemp -d prints/job.XXXXXX")
        self.assertEqual(cmds[1][-3:], [str(self.pdf), str(self.txt), "ab123456@<lab-pc>:prints/job.XXXXXX/"])
        self.assertEqual(cmds[2][-1], "lp -o number-up=2 -- prints/job.XXXXXX/some.pdf prints/job.XXXXXX/b.txt")
        self.assertEqual(cmds[3][-1], "rm -rf -- prints/job.XXXXXX; rmdir prints 2>/dev/null; true")
        self.assertIn("-O", cmds[4])
        self.assertIn("ProxyJump=ab123456@students.mimuw.edu.pl", cmds[0])

    def test_single_file_titles_job(self) -> None:
        """With one file, the job is titled after it."""
        cmds = self.run_main([str(self.pdf), "--dry-run"])
        self.assertEqual(cmds[2][-1], "lp -t some.pdf -- prints/job.XXXXXX/some.pdf")

    def test_printer_from_config(self) -> None:
        """The config's printer is used unless -d is given."""
        self.assertIn("lp -d drk -t some.pdf", self.run_main([str(self.pdf), "--dry-run"], printer="drk")[2][-1])
        self.assertIn("lp -d other", self.run_main([str(self.pdf), "-d", "other", "--dry-run"], printer="drk")[2][-1])

    def test_queries(self) -> None:
        """--quota asks students; --status and --list-options ask a lab PC."""
        self.assertEqual(self.run_main(["--quota", "--dry-run"]),
                         [["ssh", "ab123456@students.mimuw.edu.pl", "lk_print_quota"]])
        self.assertEqual(self.run_main(["--status", "-d", "drk", "--dry-run"])[0][-1], "lpstat -o drk")
        self.assertEqual(self.run_main(["--list-options", "-d", "drk", "--dry-run"])[0][-1], "lpoptions -l -p drk")

    def test_rejects_bad_input(self) -> None:
        """Missing files, duplicate names and zero copies are refused before connecting."""
        other = self.tmp / "sub"
        other.mkdir()
        (other / "some.pdf").write_text("")
        for argv, message in [([], "no files given"),
                              ([str(self.tmp / "nope.pdf")], "not a file"),
                              ([str(self.pdf), str(other / "some.pdf")], "distinct names"),
                              ([str(self.pdf), "-n", "0"], "--copies must be at least 1")]:
            with self.subTest(argv=argv), self.assertExits(1):
                self.run_main([*argv, "--dry-run"])
            self.assertIn(message, self.stderr.getvalue())


class ConnectTest(IsolatedTestCase):
    """Choosing the lab PC: remembered PCs that take the key first, else a password login."""

    def scenario(self, known: Sequence[str], booted: Sequence[str], key_ok: Collection[str],
                 password_ok: bool = True) -> list[str]:
        """Stub the network; return the list that records what was done, in order.

        Entries: 'booted' (asked students), 'key:<pc>' (tried the key), 'random<pcs>' (picked randomly
        among pcs), 'password:<pc>' (logged in, possibly with a password).
        """
        k = core.KnownPCs(make_ctx())
        k.hosts = list(known)
        k._save()
        calls: list[str] = []

        def booted_pcs(ctx: core.Context) -> list[str]:
            """Stub of lk_booted_pcs; dies like the real one when nothing is booted."""
            calls.append("booted")
            if not booted:
                core.die("no booted PCs reported by lk_booted_pcs")
            return list(booted)

        def can_login(ctx: core.Context, host: str) -> bool:
            """Stub of the key-only login check."""
            calls.append(f"key:{host}")
            return host in key_ok

        def choice(seq: Sequence[str]) -> str:
            """Deterministic stand-in for random.choice."""
            calls.append(f"random{sorted(seq)}")
            return seq[0]

        def run(cmd: Sequence[str], dry_run: bool = False, **kw: Any) -> Any:
            """Stub of the password-capable login (ssh ... true)."""
            calls.append(f"password:{cmd[-2]}")
            return completed(0 if password_ok else 255, stderr="denied")

        self.patch("mimtoolbox.tools.printer.booted_pcs", booted_pcs)
        self.patch("mimtoolbox.tools.printer.can_login", can_login)
        self.patch("mimtoolbox.tools.printer.random.choice", choice)
        self.patch("mimtoolbox.tools.printer.run", run)
        return calls

    def known_now(self) -> list[str]:
        """The remembered PCs after the scenario."""
        return core.KnownPCs(make_ctx()).hosts

    def test_nothing_known(self) -> None:
        """With no remembered PCs, a random booted one is used with a password, then remembered."""
        calls = self.scenario([], ["a", "b"], key_ok=())
        self.assertEqual(printer.connect(make_ctx(), None).host, "a")
        self.assertEqual(calls, ["booted", "random['a', 'b']", "password:a"])
        self.assertEqual(self.known_now(), ["a"])

    def test_most_recent_booted_known_pc(self) -> None:
        """The most recently used remembered PC that is booted and takes the key wins, without a password."""
        calls = self.scenario(["a", "b"], ["a", "b"], key_ok={"a"})
        lab = printer.connect(make_ctx(), None)
        self.assertEqual((lab.host, lab.batch), ("a", True))
        self.assertEqual(calls, ["booted", "key:a"])

    def test_switched_off_skipped_rejecting_forgotten(self) -> None:
        """Switched-off PCs aren't tried but stay known; a booted one that rejects the key is forgotten."""
        calls = self.scenario(["a", "b", "c"], ["b", "c"], key_ok={"c"})
        self.assertEqual(printer.connect(make_ctx(), None).host, "c")
        self.assertEqual(calls, ["booted", "key:b", "key:c"])
        self.assertEqual(self.known_now(), ["c", "a"])

    def test_only_booted_pc_rejects_key(self) -> None:
        """Regression: the only booted PC rejecting the key used to crash; now it gets a password login."""
        calls = self.scenario(["a"], ["a"], key_ok=())
        self.assertEqual(printer.connect(make_ctx(), None).host, "a")
        self.assertEqual(calls, ["booted", "key:a", "random['a']", "password:a"])
        self.assertEqual(self.known_now(), ["a"])

    def test_limited_key_tries(self) -> None:
        """At most MAX_KEY_TRIES remembered PCs are tried before falling back to a password."""
        calls = self.scenario(["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e", "x"], key_ok={"e"})
        printer.connect(make_ctx(), None)
        self.assertEqual(printer.MAX_KEY_TRIES, 3)
        self.assertEqual(calls, ["booted", "key:a", "key:b", "key:c", "random['a', 'b', 'c', 'd', 'e', 'x']",
                                 "password:a"])
        self.assertEqual(self.known_now(), ["a", "d", "e"])

    def test_nothing_booted(self) -> None:
        """With no booted PCs the tool exits and the remembered PCs are kept."""
        self.scenario(["a", "b"], [], key_ok={"a"})
        with self.assertExits(1):
            printer.connect(make_ctx(), None)
        self.assertEqual(self.known_now(), ["a", "b"])

    def test_explicit_host(self) -> None:
        """--host skips the search and is remembered once the login works."""
        calls = self.scenario(["a"], ["a"], key_ok={"a"})
        self.assertEqual(printer.connect(make_ctx(), "z").host, "z")
        self.assertEqual(calls, ["password:z"])
        self.assertEqual(self.known_now(), ["z", "a"])

    def test_failed_login_not_remembered(self) -> None:
        """A failed password login exits and leaves the PC off the list."""
        self.scenario([], ["a"], key_ok=(), password_ok=False)
        with self.assertExits(1):
            printer.connect(make_ctx(), None)
        self.assertEqual(self.known_now(), [])
        self.assertIn("ssh to a failed: denied", self.stderr.getvalue())

    def test_ssh_failure_mid_job_forgets_pc(self) -> None:
        """If ssh to a PC fails later in the job, the tool exits and the PC is forgotten."""
        self.scenario(["a", "b"], ["a", "b"], key_ok={"a"})
        self.patch("mimtoolbox.tools.printer.run", return_value=completed(255, stderr="Connection reset"))
        with self.assertExits(1):
            printer.Lab(make_ctx(), "a").ssh("lp x")
        self.assertEqual(self.known_now(), ["b"])
        self.assertIn("ssh to a failed: Connection reset", self.stderr.getvalue())

    def test_command_failure_keeps_pc(self) -> None:
        """A failing remote command (not ssh itself) doesn't count against the PC."""
        self.scenario(["a"], ["a"], key_ok={"a"})
        self.patch("mimtoolbox.tools.printer.run", return_value=completed(1, stderr="lp: error"))
        self.assertEqual(printer.Lab(make_ctx(), "a").ssh("lp x").returncode, 1)
        self.assertEqual(self.known_now(), ["a"])

    def test_dry_run_connects_nowhere(self) -> None:
        """A dry run neither asks students nor logs in anywhere."""
        calls = self.scenario(["a"], ["a"], key_ok={"a"})
        self.assertEqual(printer.connect(make_ctx(), None, dry_run=True).host, "<lab-pc>")
        self.assertEqual(calls, [])
