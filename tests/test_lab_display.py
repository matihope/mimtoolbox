"""Tests for the `lab-display` tool (mimtoolbox.tools.lab_display) that don't need a lab PC or a display."""

from __future__ import annotations

import os
import re
import signal
import sys
import time
import types
from typing import Any, Sequence
from unittest import mock

from mimtoolbox.core import KnownPCs
from mimtoolbox.tools import lab_display
from tests.support import IsolatedTestCase, completed, make_ctx, parse


def stream(res: str = "1920x1080", encoder: str = "", priority: str = "latency") -> lab_display.Stream:
    """Video settings with the priority's preset fps and bitrate."""
    preset = lab_display.PRESETS[priority]
    return lab_display.Stream(res, preset.fps, preset.bitrate, "off", encoder, priority)


ENCODERS_WITH_X264 = " V....D libx264              libx264 H.264\n V....D h264_videotoolbox    VideoToolbox\n"
ENCODERS_WITHOUT_X264 = " V....D h264_videotoolbox    VideoToolbox\n"


class HelpersTest(IsolatedTestCase):
    """Small pure helpers."""

    def test_bits_per_second(self) -> None:
        """Bitrates accept plain numbers and k/M suffixes, in either case."""
        for rate, bps in [("8M", 8_000_000), ("4500k", 4_500_000), ("1.5m", 1_500_000), ("300", 300)]:
            with self.subTest(rate=rate):
                self.assertEqual(lab_display.bits_per_second(rate), bps)

    def test_bad_bitrate_exits(self) -> None:
        """Anything else is refused."""
        for rate in ("8G", "fast", "", "-1M"):
            with self.subTest(rate=rate), self.assertExits(1):
                lab_display.bits_per_second(rate)

    def test_stream_size(self) -> None:
        """The resolution splits into width and height."""
        s = stream("2560x1440")
        self.assertEqual((s.width, s.height), ("2560", "1440"))

    def test_remote_cleanup_targets_our_player_only(self) -> None:
        """Cleanup on the lab PC kills our player (mpv or ffplay), but not other windows or itself."""
        cmd = lab_display.remote_cleanup_cmd()
        self.assertIn('-u "$USER"', cmd)
        pattern = re.compile(cmd.split("-f ")[1].strip("'"))
        for player in lab_display.PLAYERS:
            self.assertTrue(pattern.search(lab_display.player_cmd(player)), player)
        self.assertIsNone(pattern.search(cmd), "the pattern would match (and kill) the shell running it")
        self.assertIsNone(pattern.search("mpv --title=Other video.mkv"))

    def test_config_platforms(self) -> None:
        """hidpi is a macOS setting, encoder a Linux one, the rest apply everywhere."""
        platforms = {key: field.platform for key, field in lab_display.CONFIG.items()}
        self.assertEqual(platforms, {"priority": None, "fps": None, "bitrate": None, "hidpi": "darwin",
                                     "encoder": "linux"})


class PriorityTest(IsolatedTestCase):
    """Choosing between low latency and picture quality."""

    DEFAULTS = {key: field.default or "" for key, field in lab_display.CONFIG.items()}

    def settings(self, argv: Sequence[str], **config: str) -> tuple[str, str, str]:
        """video_settings() for a command line and config values (on top of the defaults)."""
        return lab_display.video_settings(parse(["lab-display", *argv]), {**self.DEFAULTS, **config})

    def test_latency_is_default(self) -> None:
        """Without options, the latency preset is used: 60 fps, 16M."""
        self.assertEqual(self.settings([]), ("latency", "60", "16M"))

    def test_quality_preset(self) -> None:
        """--quality / --priority quality / the config switch to 30 fps, 12M."""
        for argv, config in [(["--quality"], {}), (["--priority", "quality"], {}), ([], {"priority": "quality"})]:
            with self.subTest(argv=argv, config=config):
                self.assertEqual(self.settings(argv, **config), ("quality", "30", "12M"))

    def test_command_line_beats_config(self) -> None:
        """--latency overrides the config's priority."""
        self.assertEqual(self.settings(["--latency"], priority="quality")[0], "latency")

    def test_explicit_values_override_preset(self) -> None:
        """fps and bitrate from the command line or config win over the preset."""
        self.assertEqual(self.settings(["--fps", "45"], bitrate="20M"), ("latency", "45", "20M"))
        self.assertEqual(self.settings(["--quality", "--bitrate", "4M"]), ("quality", "30", "4M"))

    def test_bad_priority(self) -> None:
        """An unknown priority in the config is reported."""
        with self.assertExits(1):
            self.settings([], priority="fast")
        self.assertIn("bad priority: fast", self.stderr.getvalue())

    def test_latency_and_quality_exclusive(self) -> None:
        """--latency and --quality can't be combined."""
        with self.assertExits(2):
            parse(["lab-display", "--latency", "--quality"])

    def test_mac_encoder_args(self) -> None:
        """Latency uses x264 zerolatency (VideoToolbox if x264 is missing); quality uses VideoToolbox."""
        latency = lab_display.mac_encoder_args(stream(), have_libx264=True)
        self.assertEqual(latency[:6], ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"])
        self.assertIn("-bf", latency)
        fallback = lab_display.mac_encoder_args(stream(), have_libx264=False)
        self.assertEqual(fallback[:6], ["-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "1"])
        quality = lab_display.mac_encoder_args(stream(priority="quality"), have_libx264=True)
        self.assertEqual(quality[:6], ["-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "0"])
        for args in (latency, fallback, quality):
            self.assertEqual(args[args.index("-bf") + 1], "0")  # B-frames would add delay

    def test_x264_gets_the_frame_rate(self) -> None:
        """Regression: x264 must be told the frame rate, which screen capture doesn't report.

        Without it x264 took the capture's 1/1000000 time base as the rate, spread the bitrate over a
        million frames a second and sent a gray, almost empty picture.
        """
        args = lab_display.mac_encoder_args(stream(), have_libx264=True)
        self.assertEqual(args[args.index("-x264-params") + 1], "fps=60")
        args = lab_display.mac_encoder_args(lab_display.Stream("1920x1080", "45", "8M", "off", "", "latency"), True)
        self.assertEqual(args[args.index("-x264-params") + 1], "fps=45")

    def test_players_show_frames_immediately(self) -> None:
        """mpv shows frames untimed; ffplay paces by the video instead of slowing to an external clock."""
        mpv = lab_display.player_cmd("mpv")
        for opt in ("--profile=low-latency", "--untimed", "--fs", "--input-terminal=no", " -"):
            self.assertIn(opt, mpv)
        ffplay = lab_display.player_cmd("ffplay")
        for opt in ("-sync video", "-fflags nobuffer", "-flags low_delay", "-framedrop", "-i -"):
            self.assertIn(opt, ffplay)


class BackendTest(IsolatedTestCase):
    """Picking and constructing the platform backend."""

    def test_unsupported_platform(self) -> None:
        """Only macOS and Linux are supported."""
        self.patch("sys.platform", "win32")
        with self.assertExits(1):
            lab_display.make_backend(make_ctx("lab-display"), stream())
        self.assertIn("unsupported platform win32", self.stderr.getvalue())

    def test_mac_needs_betterdisplay(self) -> None:
        """On macOS, a missing BetterDisplay is reported."""
        self.patch("sys.platform", "darwin")
        self.patch("os.path.exists", return_value=False)
        with self.assertExits(1):
            lab_display.make_backend(make_ctx("lab-display"), stream())
        self.assertIn("BetterDisplay is not installed", self.stderr.getvalue())

    def test_mac_needs_ffmpeg(self) -> None:
        """On macOS, a missing ffmpeg is reported."""
        self.patch("sys.platform", "darwin")
        self.patch("os.path.exists", return_value=True)
        self.patch("shutil.which", return_value=None)
        with self.assertExits(1):
            lab_display.make_backend(make_ctx("lab-display"), stream())
        self.assertIn("ffmpeg not found", self.stderr.getvalue())

    def test_mac_backend(self) -> None:
        """With both installed, the macOS backend is used, encoding with x264 for low latency."""
        self.patch("sys.platform", "darwin")
        self.patch("os.path.exists", return_value=True)
        self.patch("shutil.which", return_value="/usr/bin/ffmpeg")
        self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(stdout=ENCODERS_WITH_X264))
        backend = lab_display.make_backend(make_ctx("lab-display"), stream())
        assert isinstance(backend, lab_display.MacBackend)
        self.assertEqual(backend.encoder_args[1], "libx264")

    def test_mac_without_x264_falls_back(self) -> None:
        """An ffmpeg without libx264 falls back to VideoToolbox, with a note about the extra delay."""
        self.patch("sys.platform", "darwin")
        self.patch("os.path.exists", return_value=True)
        self.patch("shutil.which", return_value="/usr/bin/ffmpeg")
        self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(stdout=ENCODERS_WITHOUT_X264))
        backend = lab_display.make_backend(make_ctx("lab-display"), stream())
        assert isinstance(backend, lab_display.MacBackend)
        self.assertEqual(backend.encoder_args[1], "h264_videotoolbox")
        self.assertIn("no libx264", self.stderr.getvalue())

    def test_linux_needs_wayland(self) -> None:
        """On Linux, an X11 session is refused."""
        self.patch("sys.platform", "linux")
        self.patch_dict(os.environ, {"WAYLAND_DISPLAY": ""})
        with self.assertExits(1):
            lab_display.make_backend(make_ctx("lab-display"), stream())
        self.assertIn("needs a Wayland session", self.stderr.getvalue())

    def pretend_linux(self) -> None:
        """A Wayland session with GStreamer, PyGObject and some encoders installed."""
        self.patch("sys.platform", "linux")
        self.patch_dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"})
        self.patch("shutil.which", return_value="/usr/bin/gst")
        installed = {"pipewiresrc", "x264enc", "vah264enc", "openh264enc", "mystery264"}
        self.patch("mimtoolbox.tools.lab_display.quiet", side_effect=lambda cmd: cmd[-1] in installed)
        self.patch_dict(sys.modules, {"gi": types.ModuleType("gi")})

    def test_linux_encoder_choice(self) -> None:
        """The priority's first installed encoder is used, unless one is configured; unknown ones are refused."""
        self.pretend_linux()
        ctx = make_ctx("lab-display")
        backend = lab_display.make_backend(ctx, stream())
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertEqual(backend.encoder, "x264enc")
        self.assertEqual(backend.token_file, str(ctx.state_dir / "lab-display-restore-token"))
        backend = lab_display.make_backend(ctx, stream(priority="quality"))
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertEqual(backend.encoder, "vah264enc")
        backend = lab_display.make_backend(ctx, stream(encoder="openh264enc"))
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertEqual(backend.encoder, "openh264enc")
        with self.assertExits(1):
            lab_display.make_backend(ctx, stream(encoder="mystery264"))
        self.assertIn("unsupported encoder mystery264", self.stderr.getvalue())

    def test_linux_stall(self) -> None:
        """Regression: GNOME 50 stops the stream at a display change; silence for 2 s after one is a stall."""
        self.pretend_linux()
        backend = lab_display.make_backend(make_ctx("lab-display"), stream())
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertFalse(backend.is_stalled(100))  # nothing yet
        backend.changed_at = 10.0  # our own virtual monitor appearing, before any output
        self.assertFalse(backend.is_stalled(100))
        backend.last_data = 10.5  # output flows after the change
        self.assertFalse(backend.is_stalled(100))
        backend.changed_at = 20.0  # displays rearranged, nothing since
        self.assertFalse(backend.is_stalled(21.9))
        self.assertTrue(backend.is_stalled(22.0))
        backend.last_data = 21.0  # output resumed: a GNOME that keeps the stream going
        self.assertFalse(backend.is_stalled(30))
        self.assertFalse(lab_display.MacBackend.stalled(mock.Mock()))  # AVFoundation is just quiet when idle


class RelayTest(IsolatedTestCase):
    """Relaying the capture's output to the player's pipe."""

    def test_copies_and_closes(self) -> None:
        """Everything is copied, each chunk is reported, and both ends are closed at EOF."""
        src_r, src_w = os.pipe()
        dst_r, dst_w = os.pipe()
        os.write(src_w, b"frame1frame2")
        os.close(src_w)
        src = os.fdopen(src_r, "rb")
        seen: list[int] = []
        lab_display.relay(src, dst_w, lambda: seen.append(1))
        self.assertTrue(src.closed)
        with self.assertRaises(OSError):
            os.fstat(dst_w)
        self.assertEqual(os.read(dst_r, 100), b"frame1frame2")
        self.assertEqual(os.read(dst_r, 100), b"")  # EOF: the relay held the only write end
        os.close(dst_r)
        self.assertTrue(seen)

    def test_player_gone(self) -> None:
        """When the player has gone (nobody reads), the relay stops instead of raising."""
        src_r, src_w = os.pipe()
        dst_r, dst_w = os.pipe()
        os.close(dst_r)
        os.write(src_w, b"frame")
        os.close(src_w)
        lab_display.relay(os.fdopen(src_r, "rb"), dst_w, lambda: None)  # must not raise


class FakeProc:
    """A process that is running until `polls` polls have been made."""

    def __init__(self, pid: int, polls: int = 10**6) -> None:
        """Running for `polls` polls; exits with 0 after that."""
        self.pid, self.polls = pid, polls

    def poll(self) -> int | None:
        """None while running, 0 once it has exited."""
        self.polls -= 1
        return None if self.polls >= 0 else 0


class FakeBackend:
    """A backend whose captures write one line each, stalling as told."""

    hint = "a hint"

    def __init__(self, stalls: Sequence[bool]) -> None:
        """`stalls`: what stalled() answers on successive calls (then False)."""
        self.stalls = list(stalls)
        self.captures: list[FakeProc] = []
        self.cleanups = 0

    def prepare(self) -> None:
        """Nothing to prepare."""

    def start(self, out: int) -> Any:
        """Write a line to `out` and return a running fake capture."""
        os.write(out, f"capture {len(self.captures)}\n".encode())
        self.captures.append(FakeProc(1000 + len(self.captures)))
        return self.captures[-1]

    def stalled(self) -> bool:
        """The next answer from `stalls`."""
        return self.stalls.pop(0) if self.stalls else False

    def cleanup(self) -> None:
        """Count the calls."""
        self.cleanups += 1


class StreamToPlayerTest(IsolatedTestCase):
    """The streaming loop: capture into the player, restarting a stalled capture."""

    def run_stream(self, backend: FakeBackend, player_polls: int) -> tuple[bytes, list[str], list[Any], Any]:
        """Run stream_to_player with a fake player that runs for `player_polls` polls.

        Returns:
            What the player got on stdin, the guardian's protocol lines, the procs list and the
            stop_processes mock.
        """
        self.patch("time.sleep")
        stop = self.patch("mimtoolbox.tools.lab_display.stop_processes")
        stdins: list[int] = []

        def popen(cmd: list[str], stdin: int) -> FakeProc:
            """The player: keep a copy of its stdin to read what it got."""
            stdins.append(os.dup(stdin))
            return FakeProc(2000, player_polls)
        self.patch("subprocess.Popen", side_effect=popen)
        guardian = mock.Mock()
        procs: list[Any] = []
        lab_display.stream_to_player(backend, ["player"], guardian, procs)
        with os.fdopen(stdins[0], "rb") as f:
            got = f.read()
        return got, [c.args[0] for c in guardian.send.call_args_list], procs, stop

    def test_restart_after_stall(self) -> None:
        """A stalled capture is stopped and replaced; the player keeps running and gets both streams."""
        backend = FakeBackend([False, True, False])
        got, sent, procs, stop = self.run_stream(backend, player_polls=4)
        self.assertEqual(got, b"capture 0\ncapture 1\n")
        self.assertEqual(len(backend.captures), 2)
        self.assertEqual(backend.cleanups, 1)
        stop.assert_called_once_with([1000])
        self.assertEqual(sent, ["pid 1000", "pid 2000", "pid 1001"])
        self.assertEqual([p.pid for p in procs], [1000, 2000, 1001])
        self.assertIn("starting a new virtual monitor", self.stderr.getvalue())
        self.assertIn("stream ended", self.stderr.getvalue())

    def test_no_stall(self) -> None:
        """Without a stall there's one capture until the player ends."""
        backend = FakeBackend([])
        got, sent, procs, stop = self.run_stream(backend, player_polls=3)
        self.assertEqual(got, b"capture 0\n")
        self.assertEqual(backend.cleanups, 0)
        stop.assert_not_called()


def session(host: str) -> str:
    """Probe output for a desktop session (what PROBE_SCRIPT prints)."""
    return f"DISPLAY=:0\nXAUTHORITY=/home/{host}/.Xauthority\n"


class FindDesktopTest(IsolatedTestCase):
    """Finding the lab PC where you're logged in at the desktop."""

    def scenario(self, candidates: Sequence[str], sessions: Sequence[str]) -> list[tuple[str, bool]]:
        """Stub the login history and the probes; return the (host, batch) probes made."""
        self.patch("mimtoolbox.tools.lab_display.desktop_pcs", return_value=list(candidates))
        probes: list[tuple[str, bool]] = []

        def probe(ctx: Any, host: str, batch: bool = False) -> Any:
            """Stub probe: a session on the PCs in `sessions`, key rejected elsewhere."""
            probes.append((host, batch))
            return completed(0, session(host)) if host in sessions else completed(255, stderr="Permission denied")

        self.patch("mimtoolbox.tools.lab_display.probe", probe)
        return probes

    def test_newest_login_wins(self) -> None:
        """If the most recent login's session is running, it's used without probing the others."""
        probes = self.scenario(["white02", "red01"], sessions=["white02", "red01"])
        host, env = lab_display.find_desktop(make_ctx("lab-display"))
        self.assertEqual((host, env), ("white02", ["DISPLAY=:0", "XAUTHORITY=/home/white02/.Xauthority"]))
        self.assertEqual(probes, [("white02", True)])

    def test_falls_back_to_older_logins(self) -> None:
        """If the newest login has no session, the older ones are probed; all probes are key-only."""
        probes = self.scenario(["white02", "red01", "black00"], sessions=["black00"])
        self.assertEqual(lab_display.find_desktop(make_ctx("lab-display"))[0], "black00")
        self.assertEqual(probes[0], ("white02", True))
        self.assertEqual(sorted(probes[1:]), [("black00", True), ("red01", True)])

    def test_several_older_sessions_ask(self) -> None:
        """With sessions on several older PCs, the user chooses, newest login first."""
        self.scenario(["black00", "red01", "white02"], sessions=["white02", "red01"])
        pick = self.patch("mimtoolbox.tools.lab_display.pick_pc_interactive", return_value="white02")
        self.assertEqual(lab_display.find_desktop(make_ctx("lab-display"))[0], "white02")
        pick.assert_called_once_with(["red01", "white02"])

    def test_no_login_history(self) -> None:
        """If no booted PC has your desktop login, it says so without probing anything."""
        probes = self.scenario([], sessions=[])
        with self.assertExits(1):
            lab_display.find_desktop(make_ctx("lab-display"))
        self.assertIn("you aren't logged in at the desktop of any booted lab PC", self.stderr.getvalue())
        self.assertEqual(probes, [])

    def test_logged_out_everywhere(self) -> None:
        """If the PCs from the history have no running session (you logged out), it says so."""
        self.scenario(["white02"], sessions=[])
        with self.assertExits(1):
            lab_display.find_desktop(make_ctx("lab-display"))
        self.assertIn("no desktop session of ab123456 found on white02", self.stderr.getvalue())


class MainTest(IsolatedTestCase):
    """The checks main() does before streaming."""

    def setUp(self) -> None:
        """Fail the test if main() goes looking for the desktop PC unexpectedly."""
        super().setUp()
        self.find = self.patch("mimtoolbox.tools.lab_display.find_desktop",
                               side_effect=AssertionError("must not look for the desktop PC"))

    def run_main(self, argv: Sequence[str], **settings: str) -> Any:
        """Run lab-display with argv and the config defaults (overridden by `settings`)."""
        values = {key: field.default or "" for key, field in lab_display.CONFIG.items()}
        values.update(settings)
        return lab_display.main(parse(["lab-display", *argv]), make_ctx("lab-display", **values))

    def test_bad_fps_and_bitrate_rejected_first(self) -> None:
        """Bad --fps/--bitrate (or config values) fail before any connection."""
        for argv, settings, message in [(["--fps", "x", "red01"], {}, "bad fps: x"),
                                        (["--fps", "0", "red01"], {}, "bad fps: 0"),
                                        (["red01"], {"fps": "abc"}, "bad fps: abc"),
                                        (["--bitrate", "8G", "red01"], {}, "bad bitrate: 8G")]:
            with self.subTest(argv=argv, settings=settings), self.assertExits(1):
                self.run_main(argv, **settings)
            self.assertIn(message, self.stderr.getvalue())

    def test_named_pc_requires_desktop_session(self) -> None:
        """With a PC named, it is probed (password allowed); no session there means a clear error."""
        run = self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(3))
        with self.assertExits(1):
            self.run_main(["red01"])
        self.assertIn("no graphical session of ab123456 found on red01 - log in at the PC first",
                      self.stderr.getvalue())
        self.assertEqual(run.call_args.kwargs["input"], lab_display.PROBE_SCRIPT)
        self.assertNotIn("BatchMode=yes", run.call_args.args[0])

    def test_detected_pc_is_used_and_remembered(self) -> None:
        """Without a PC name the detected one is used, and remembered for print (it takes the key)."""
        self.find.side_effect = None
        self.find.return_value = ("white02", ["DISPLAY=:0"])
        run = self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(1))  # no player: stop there
        with self.assertExits(1):
            self.run_main([])
        self.assertIn("mpv or ffplay not found on white02", self.stderr.getvalue())
        self.assertEqual(run.call_args.args[0][-2:], ["white02", "command -v mpv; command -v ffplay;"])
        self.assertEqual(KnownPCs(make_ctx()).hosts, ["white02"])

    def test_player_choice(self) -> None:
        """mpv is preferred when installed on the lab PC, ffplay otherwise; --player forces one."""
        ctx = make_ctx("lab-display")
        for wanted, installed, expected in [(None, "/usr/bin/mpv\n/usr/bin/ffplay\n", "mpv"),
                                            (None, "/usr/bin/ffplay\n", "ffplay"),
                                            ("ffplay", "/usr/bin/ffplay\n", "ffplay")]:
            with self.subTest(wanted=wanted, installed=installed):
                run = self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(stdout=installed))
                self.assertEqual(lab_display.find_player(ctx, "white02", wanted), expected)
                self.assertEqual(run.call_args.args[0][-2:], ["white02", "command -v ffplay;" if wanted else
                                                              "command -v mpv; command -v ffplay;"])
        self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(stdout="/usr/bin/mpv\n"))
        with self.assertExits(1):
            lab_display.find_player(ctx, "white02", "ffplay")
        self.assertIn("ffplay not found on white02", self.stderr.getvalue())


class StopProcessesTest(IsolatedTestCase):
    """Stopping the capture and player processes on cleanup."""

    def spawn(self, ignore_sigterm: bool) -> int:
        """Fork a child that sleeps (optionally ignoring SIGTERM, like ffmpeg stuck waiting for a frame)."""
        pid = os.fork()
        if pid == 0:
            if ignore_sigterm:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(30)
            os._exit(0)
        self.addCleanup(self.reap, pid)
        time.sleep(0.2)  # let the child set up its signal handling
        return pid

    def reap(self, pid: int) -> None:
        """Make sure a child is gone after the test."""
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    def assertGone(self, pid: int) -> None:
        """The process no longer exists."""
        with self.assertRaises((ProcessLookupError, ChildProcessError)):
            os.kill(pid, 0)
            os.waitpid(pid, os.WNOHANG)

    def test_sigterm_suffices(self) -> None:
        """A process that exits on SIGTERM is stopped without waiting for the timeout."""
        pid = self.spawn(ignore_sigterm=False)
        start = time.monotonic()
        lab_display.stop_processes([pid], timeout=5)
        self.assertLess(time.monotonic() - start, 2)
        self.assertGone(pid)

    def test_stuck_process_is_killed(self) -> None:
        """Regression: a process ignoring SIGTERM gets SIGKILL after the timeout instead of running on."""
        pid = self.spawn(ignore_sigterm=True)
        lab_display.stop_processes([pid], timeout=0.3)
        self.assertGone(pid)

    def test_already_gone(self) -> None:
        """Processes that have already exited are fine."""
        pid = self.spawn(ignore_sigterm=False)
        self.reap(pid)
        lab_display.stop_processes([pid], timeout=0.3)  # must not raise
