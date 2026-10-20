"""Tests for the `lab-display` tool (mimtoolbox.tools.lab_display) that don't need a lab PC or a display."""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Sequence

from mimtoolbox.tools import lab_display
from tests.support import IsolatedTestCase, completed, make_ctx, parse


def stream(res: str = "1920x1080", encoder: str = "") -> lab_display.Stream:
    """Video settings with the config defaults."""
    return lab_display.Stream(res, "30", "8M", "off", encoder)


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
        self.assertEqual(platforms, {"fps": None, "bitrate": None, "hidpi": "darwin", "encoder": "linux"})


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
        """With both installed, the macOS backend is used."""
        self.patch("sys.platform", "darwin")
        self.patch("os.path.exists", return_value=True)
        self.patch("shutil.which", return_value="/usr/bin/ffmpeg")
        self.assertIsInstance(lab_display.make_backend(make_ctx("lab-display"), stream()), lab_display.MacBackend)

    def test_linux_needs_wayland(self) -> None:
        """On Linux, an X11 session is refused."""
        self.patch("sys.platform", "linux")
        self.patch_dict(os.environ, {"WAYLAND_DISPLAY": ""})
        with self.assertExits(1):
            lab_display.make_backend(make_ctx("lab-display"), stream())
        self.assertIn("needs a Wayland session", self.stderr.getvalue())

    def test_linux_encoder_choice(self) -> None:
        """The first installed encoder is used, unless one is configured; unknown ones are refused."""
        self.patch("sys.platform", "linux")
        self.patch_dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"})
        self.patch("shutil.which", return_value="/usr/bin/gst")
        installed = {"pipewiresrc", "x264enc", "openh264enc", "mystery264"}
        self.patch("mimtoolbox.tools.lab_display.quiet", side_effect=lambda cmd: cmd[-1] in installed)
        self.patch_dict(sys.modules, {"gi": types.ModuleType("gi")})  # pretend PyGObject is installed
        ctx = make_ctx("lab-display")
        backend = lab_display.make_backend(ctx, stream())
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertEqual(backend.encoder, "x264enc")
        self.assertEqual(backend.token_file, str(ctx.state_dir / "lab-display-restore-token"))
        backend = lab_display.make_backend(ctx, stream(encoder="openh264enc"))
        assert isinstance(backend, lab_display.PortalBackend)
        self.assertEqual(backend.encoder, "openh264enc")
        with self.assertExits(1):
            lab_display.make_backend(ctx, stream(encoder="mystery264"))
        self.assertIn("unsupported encoder mystery264", self.stderr.getvalue())


class MainTest(IsolatedTestCase):
    """The checks main() does before streaming."""

    def setUp(self) -> None:
        """Fail the test if main() tries to list booted PCs unexpectedly."""
        super().setUp()
        self.booted = self.patch("mimtoolbox.tools.lab_display.booted_pcs",
                                 side_effect=AssertionError("must not ask students"))

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

    def test_requires_desktop_session(self) -> None:
        """If you aren't logged in at the lab PC's desktop, it stops and says so."""
        run = self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(3))
        with self.assertExits(1):
            self.run_main(["red01"])
        self.assertIn("no graphical session of ab123456 found on red01 - log in at the PC first",
                      self.stderr.getvalue())
        self.assertEqual(run.call_args.kwargs["input"], lab_display.PROBE_SCRIPT)

    def test_picks_from_booted_pcs_without_host(self) -> None:
        """Without a host, the user chooses among the booted PCs."""
        self.booted.side_effect = None
        self.booted.return_value = ["red01", "red02"]
        pick = self.patch("mimtoolbox.tools.lab_display.pick_pc_interactive", return_value="red02")
        self.patch("mimtoolbox.tools.lab_display.run", return_value=completed(3))
        with self.assertExits(1):
            self.run_main([])
        pick.assert_called_once_with(["red01", "red02"])
        self.assertIn("found on red02", self.stderr.getvalue())
