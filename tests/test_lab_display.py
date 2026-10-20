"""Tests for the `lab-display` tool (mimtoolbox.tools.lab_display) that don't need a lab PC or a display."""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Sequence

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
        """Cleanup on the lab PC only kills our own ffplay window."""
        self.assertIn("window_title LabDisplay", lab_display.remote_cleanup_cmd())
        self.assertIn('-u "$USER"', lab_display.remote_cleanup_cmd())

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

    def test_player_paces_by_video(self) -> None:
        """ffplay shows frames as they arrive instead of slowing to an external clock."""
        for opt in ("-sync video", "-fflags nobuffer", "-flags low_delay", "-framedrop"):
            self.assertIn(opt, lab_display.PLAYER_OPTS)


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

    def test_linux_encoder_choice(self) -> None:
        """The priority's first installed encoder is used, unless one is configured; unknown ones are refused."""
        self.patch("sys.platform", "linux")
        self.patch_dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"})
        self.patch("shutil.which", return_value="/usr/bin/gst")
        installed = {"pipewiresrc", "x264enc", "vah264enc", "openh264enc", "mystery264"}
        self.patch("mimtoolbox.tools.lab_display.quiet", side_effect=lambda cmd: cmd[-1] in installed)
        self.patch_dict(sys.modules, {"gi": types.ModuleType("gi")})  # pretend PyGObject is installed
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
        run = self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(1))  # no ffplay: stop there
        with self.assertExits(1):
            self.run_main([])
        self.assertIn("ffplay not found on white02", self.stderr.getvalue())
        self.assertEqual(run.call_args.args[0][-2:], ["white02", "command -v ffplay >/dev/null"])
        self.assertEqual(KnownPCs(make_ctx()).hosts, ["white02"])
