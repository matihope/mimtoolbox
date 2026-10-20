"""Use a MIMUW lab PC's monitor as an extra display for this computer.

A virtual monitor is created locally, captured, encoded to H.264 and piped over ssh (via students)
into a fullscreen ffplay on your logged-in desktop session on the lab PC. View-only: keep using
this computer's keyboard/mouse, the lab monitor behaves like an extra screen.

Local side:
  macOS          BetterDisplay virtual screen -> ffmpeg (AVFoundation, VideoToolbox H.264)
  Linux/Wayland  ScreenCast portal virtual monitor (GNOME) -> GStreamer (pipewiresrc, best H.264 encoder)
                 First run shows a "share screen" dialog; the restore token saved afterwards skips it.

Examples:
  mimtoolbox.py lab-display                  pick a booted lab PC, use its resolution
  mimtoolbox.py lab-display <pc> 1920x1080   stream to <pc> at a fixed resolution
  mimtoolbox.py lab-display --fps 60 --bitrate 12M

Assumes you are already logged in on the lab PC's desktop (in person, as lab_user): the player is
opened inside that existing graphical session, found through a process of yours that has DISPLAY or
WAYLAND_DISPLAY set. Without such a session it stops. Key login to the PC: `mimtoolbox.py setup`.

Cleanup (stop ffplay on the lab PC, remove the virtual monitor, close the ssh master) is done by a
guardian process in its own session. It watches a pipe held by the main process, so it runs even
when the main process is SIGKILLed or its whole process group is killed.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from types import FrameType
from typing import IO, Any, Callable, Mapping, Protocol, Sequence

from mimtoolbox.core import (ConfigField, Context, booted_pcs, close_master, die, lab_ssh, log, pick_pc_interactive,
                             quiet, run)

COMMAND: str = "lab-display"
HELP: str = "Use a lab PC's monitor as an extra display."
CONFIG: Mapping[str, ConfigField] = {
    "fps": ConfigField("30", "frames per second"),
    "bitrate": ConfigField("8M", "video bitrate (e.g. 8M, 4500k)"),
    "hidpi": ConfigField("off", "HiDPI virtual screen (on/off)", platform="darwin"),
    "encoder": ConfigField("", "GStreamer H.264 encoder element (empty = best available)", platform="linux"),
}

SCREEN_NAME: str = "LabDisplay"

# Finds a process of ours that runs inside the graphical session and prints its display env.
PROBE_SCRIPT: str = r"""
for p in $(pgrep -u "$USER"); do
    e=$( { tr '\0' '\n' < /proc/$p/environ; } 2>/dev/null) || continue
    grep -qE '^(DISPLAY|WAYLAND_DISPLAY)=' <<<"$e" || continue
    grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR)=' <<<"$e"
    exit 0
done
exit 3
"""


@dataclass(frozen=True)
class Stream:
    """Video settings, resolved from the command line and the config."""

    res: str
    """WxH."""
    fps: str
    bitrate: str
    """Bits per second, optionally with a k/M suffix."""
    hidpi: str
    """macOS: 'on' or 'off'."""
    encoder: str
    """Linux: GStreamer element; empty = first installed one of PortalBackend.ENCODERS."""

    @property
    def width(self) -> str:
        """Width part of `res`."""
        return self.res.split("x")[0]

    @property
    def height(self) -> str:
        """Height part of `res`."""
        return self.res.split("x")[1]


class Backend(Protocol):
    """A way of creating a virtual monitor on this computer and capturing it."""

    hint: str
    """Told to the user once streaming has started."""

    def prepare(self) -> None:
        """Get ready to start; runs before the guardian is forked."""

    def start(self) -> subprocess.Popen[bytes]:
        """Create the virtual monitor and start a process writing its H.264 MPEG-TS to stdout."""

    def cleanup(self) -> None:
        """Remove the virtual monitor; idempotent."""


def bits_per_second(rate: str) -> int:
    """Parse a bitrate like 8M or 4500k."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", rate)
    if not m:
        die(f"bad bitrate: {rate}")
    return int(float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()])


def remote_cleanup_cmd() -> str:
    """Shell command that stops our ffplay on the lab PC."""
    return f"pkill -u \"$USER\" -f 'window_title {SCREEN_NAME}'"


class MacBackend:
    """BetterDisplay virtual screen captured by ffmpeg via AVFoundation."""

    BD: str = "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
    hint: str = "if the lab screen stays black, grant Screen Recording to your terminal in System Settings"

    def __init__(self, stream: Stream) -> None:
        """Check that BetterDisplay and ffmpeg are installed."""
        self.stream = stream
        self.before: set[str] = set()
        if not os.path.exists(self.BD):
            die("BetterDisplay is not installed")
        if not shutil.which("ffmpeg"):
            die("ffmpeg not found (brew install ffmpeg)")

    def bd(self, *args: str) -> bool:
        """Run a BetterDisplay CLI command; tell whether it succeeded."""
        return quiet([self.BD, *args])

    @staticmethod
    def list_screens() -> set[str]:
        """Indices of the AVFoundation screen capture devices."""
        out = run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                  capture_output=True).stderr
        return set(re.findall(r"\[(\d+)\] Capture screen", out))

    def prepare(self) -> None:
        """Start BetterDisplay, create or update the (disconnected) virtual screen."""
        s = self.stream
        if not quiet(["pgrep", "-qx", "BetterDisplay"]):
            run(["open", "-ga", "BetterDisplay"])
            time.sleep(3)
        if not self.bd("get", f"-nameLike={SCREEN_NAME}", "-identifiers"):
            log(f"creating virtual screen {SCREEN_NAME}")
            if not self.bd("create", "-type=VirtualScreen", f"-virtualScreenName={SCREEN_NAME}",
                           "-useResolutionList=on", f"-resolutionList={s.res}", f"-virtualScreenHiDPI={s.hidpi}"):
                die(f"could not create virtual screen (enable CLI integration in BetterDisplay "
                    f"settings, or create '{SCREEN_NAME}' by hand)")
        # Also clears leftovers from a previous run that didn't get to clean up.
        self.cleanup()
        self.bd("set", f"-nameLike={SCREEN_NAME}", "-useResolutionList=on", f"-resolutionList={s.res}",
                f"-virtualScreenHiDPI={s.hidpi}")
        time.sleep(1)
        self.before = self.list_screens()

    def start(self) -> subprocess.Popen[bytes]:
        """Connect the virtual screen, find its capture device and start ffmpeg on it."""
        s = self.stream
        self.bd("set", f"-nameLike={SCREEN_NAME}", "-connected=on")
        idx = None
        for _ in range(20):
            time.sleep(0.5)
            new = self.list_screens() - self.before
            if new:
                idx = min(new, key=int)
                break
        if idx is None:
            die("virtual screen did not show up as an AVFoundation capture device")
        self.bd("set", f"-nameLike={SCREEN_NAME}", f"-resolution={s.res}")
        log(f"capturing AVFoundation device {idx} ({s.res} @ {s.fps}fps, {s.bitrate})")
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
             "-f", "avfoundation", "-capture_cursor", "1", "-framerate", s.fps, "-pixel_format", "nv12",
             "-i", f"{idx}:none",
             "-vf", f"scale={s.width}:{s.height},format=nv12",
             "-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "1",
             "-b:v", s.bitrate, "-maxrate", s.bitrate, "-g", s.fps, "-bf", "0", "-profile:v", "high",
             "-f", "mpegts", "-"],
            stdout=subprocess.PIPE)

    def cleanup(self) -> None:
        """Disconnect the virtual screen."""
        self.bd("set", f"-nameLike={SCREEN_NAME}", "-connected=off")


class PortalBackend:
    """Virtual monitor from the xdg-desktop-portal ScreenCast API (GNOME), captured by GStreamer.

    The virtual monitor takes the size GStreamer negotiates and lives as long as our D-Bus
    connection, so the compositor removes it by itself even if we are SIGKILLed.
    """

    DEST: str = "org.freedesktop.portal.Desktop"
    OBJ: str = "/org/freedesktop/portal/desktop"
    IFACE: str = "org.freedesktop.portal.ScreenCast"
    VIRTUAL, CURSOR_EMBEDDED, PERSIST_UNTIL_REVOKED = 4, 2, 2
    hint: str = "move windows to the new monitor (arrange it in Settings > Displays)"

    # element -> (input format, properties); first one installed wins
    ENCODERS: Mapping[str, tuple[str, str]] = {
        "vah264lpenc": ("NV12", "bitrate={kbps} key-int-max={fps} b-frames=0 rate-control=cbr target-usage=7"),
        "vah264enc": ("NV12", "bitrate={kbps} key-int-max={fps} b-frames=0 rate-control=cbr target-usage=7"),
        "x264enc": ("I420", "tune=zerolatency speed-preset=ultrafast bitrate={kbps} key-int-max={fps}"),
        "openh264enc": ("I420", "bitrate={bps} gop-size={fps} complexity=low rate-control=bitrate "
                                "usage-type=screen"),
    }

    def __init__(self, stream: Stream, token_file: str) -> None:
        """Check for Wayland, GStreamer, an H.264 encoder and PyGObject.

        Args:
            token_file: Where the portal's restore token is kept between runs.
        """
        self.stream, self.token_file = stream, token_file
        if not os.environ.get("WAYLAND_DISPLAY"):
            die("the Linux backend needs a Wayland session (WAYLAND_DISPLAY is not set)")
        for tool in ("gst-launch-1.0", "gst-inspect-1.0"):
            if not shutil.which(tool):
                die(f"{tool} not found (install gstreamer)")
        if not quiet(["gst-inspect-1.0", "--exists", "pipewiresrc"]):
            die("GStreamer pipewiresrc missing (install gst-plugin-pipewire)")
        names = [stream.encoder] if stream.encoder else list(self.ENCODERS)
        encoder = next((e for e in names if quiet(["gst-inspect-1.0", "--exists", e])), None)
        if not encoder:
            die(f"no usable H.264 encoder among {', '.join(names)} "
                f"(install gst-plugin-va for hardware encoding)")
        if encoder not in self.ENCODERS:
            die(f"unsupported encoder {encoder}; known: {', '.join(self.ENCODERS)}")
        self.encoder: str = encoder
        try:
            import gi  # type: ignore[import-not-found]  # noqa: F401  (imported for real in start(), after the guardian fork)
        except ImportError:
            die("PyGObject (python-gobject) is required")
        self.bus: Any = None
        self.session: Any = None
        self.n = 0

    def prepare(self) -> None:
        """Nothing to do before the guardian is forked."""

    def _request(self, method: str, make_args: Callable[[Any], Any], timeout: float) -> dict[str, Any]:
        """Call a portal method that answers through a Request object, and wait for the answer.

        Args:
            make_args: Builds the call's arguments (a GLib.Variant) from the request's handle_token.
            timeout: Seconds to wait for the answer (it may involve the user confirming a dialog).

        Returns:
            The results dict of the Response signal.
        """
        from gi.repository import GLib  # type: ignore[import-not-found]
        self.n += 1
        token = f"labdisplay{os.getpid()}_{self.n}"
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"{self.OBJ}/request/{sender}/{token}"
        res: list[Any] = []
        sid = self.bus.signal_subscribe(self.DEST, "org.freedesktop.portal.Request", "Response", path, None, 0,
                                        lambda *a: res.append(a[5].unpack()))
        self.bus.call_sync(self.DEST, self.OBJ, self.IFACE, method, make_args(GLib.Variant("s", token)),
                           None, 0, -1, None)
        ctx, t0 = GLib.MainContext.default(), time.time()
        while not res and time.time() - t0 < timeout:
            ctx.iteration(False)
            time.sleep(0.01)
        self.bus.signal_unsubscribe(sid)
        if not res:
            die(f"portal {method} timed out")
        code, results = res[0]
        if code != 0:
            die(f"portal {method} " + ("cancelled" if code == 1 else f"failed (code {code})"))
        return dict(results)

    def start(self) -> subprocess.Popen[bytes]:
        """Open a ScreenCast session with a virtual monitor and start GStreamer on its PipeWire stream."""
        from gi.repository import Gio, GLib
        V = GLib.Variant
        s = self.stream
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        try:
            types = self.bus.call_sync(self.DEST, self.OBJ, "org.freedesktop.DBus.Properties", "Get",
                                       V("(ss)", (self.IFACE, "AvailableSourceTypes")), None, 0, 5000,
                                       None).unpack()[0]
        except GLib.Error as e:
            die(f"ScreenCast portal unavailable: {e.message}")
        if not types & self.VIRTUAL:
            die("the ScreenCast portal of this desktop can't create virtual monitors")

        r = self._request("CreateSession", lambda t: V("(a{sv})", (
            {"handle_token": t, "session_handle_token": t},)), 10)
        self.session = r["session_handle"]
        opts = {"types": V("u", self.VIRTUAL), "cursor_mode": V("u", self.CURSOR_EMBEDDED),
                "persist_mode": V("u", self.PERSIST_UNTIL_REVOKED)}
        try:
            with open(self.token_file) as f:
                opts["restore_token"] = V("s", f.read().strip())
        except OSError:
            log("first run: confirm the screen sharing dialog on this computer")
        self._request("SelectSources", lambda t: V("(oa{sv})", (self.session, dict(opts, handle_token=t))), 10)
        r = self._request("Start", lambda t: V("(osa{sv})", (self.session, "", {"handle_token": t})), 120)
        if "restore_token" in r:  # single-use: a fresh one comes with every start
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, "w") as f:
                f.write(r["restore_token"])
        node = r["streams"][0][0]
        out, fds = self.bus.call_with_unix_fd_list_sync(self.DEST, self.OBJ, self.IFACE, "OpenPipeWireRemote",
                                                        V("(oa{sv})", (self.session, {})), None, 0, -1, None,
                                                        None)
        fd: int = fds.get(out.unpack()[0])

        fmt, props = self.ENCODERS[self.encoder]
        bps = bits_per_second(s.bitrate)
        props = props.format(kbps=bps // 1000, bps=bps, fps=s.fps)
        pipeline = (f"pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time=100 "
                    f"! video/x-raw,width={s.width},height={s.height},max-framerate={s.fps}/1 "
                    f"! videorate ! video/x-raw,framerate={s.fps}/1 "
                    f"! videoconvert ! video/x-raw,format={fmt} "
                    f"! {self.encoder} {props} "
                    f"! h264parse config-interval=-1 ! mpegtsmux ! fdsink fd=1")
        log(f"capturing virtual monitor (PipeWire node {node}, {s.res} @ {s.fps}fps, {s.bitrate}, "
            f"{self.encoder})")
        proc = subprocess.Popen(["gst-launch-1.0", "-q", *shlex.split(pipeline)],
                                stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, pass_fds=[fd])
        os.close(fd)
        return proc

    def cleanup(self) -> None:
        """Close the ScreenCast session, which removes the virtual monitor.

        Only meaningful in the main process; the guardian has no bus (the monitor dies with ours).
        """
        if self.bus and self.session:
            try:
                self.bus.call_sync(self.DEST, self.session, "org.freedesktop.portal.Session", "Close",
                                   None, None, 0, 2000, None)
            except Exception:
                pass
            self.session = None


def make_backend(ctx: Context, stream: Stream) -> Backend:
    """The backend for this platform."""
    if sys.platform == "darwin":
        return MacBackend(stream)
    if sys.platform.startswith("linux"):
        return PortalBackend(stream, str(ctx.state_dir / "lab-display-restore-token"))
    die(f"unsupported platform {sys.platform}")


def cleanup(ctx: Context, host: str, backend: Backend, pids: Sequence[int] = ()) -> None:
    """Stop local processes, the virtual monitor, ffplay on the lab PC and the ssh master.

    Idempotent: safe to run from both the main process and the guardian.
    """
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    backend.cleanup()
    run(lab_ssh(ctx, host, connect_timeout=10) + [remote_cleanup_cmd()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    close_master(ctx, host)


class Guardian:
    """Forked watchdog that cleans up if the main process dies without doing it.

    Protocol over the pipe: 'pid N' registers a child to kill, 'done' means the main process cleaned
    up itself. EOF without 'done' (main process died) triggers cleanup.
    """

    def __init__(self, ctx: Context, host: str, backend: Backend) -> None:
        """Fork the guardian; in the parent, keep the write end of the pipe."""
        r, self.w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        if os.fork() == 0:
            os.close(self.w)
            os.setsid()  # escape the process group so a group kill doesn't take us too
            for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(s, signal.SIG_IGN)
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            buf = b""
            while True:
                chunk = os.read(r, 4096)
                if not chunk:
                    break
                buf += chunk
            lines = buf.decode().split("\n")
            if "done" not in lines:
                pids = [int(line.split()[1]) for line in lines if line.startswith("pid ")]
                cleanup(ctx, host, backend, pids)
            os._exit(0)
        os.close(r)

    def send(self, line: str) -> None:
        """Send one protocol line to the guardian."""
        os.write(self.w, f"{line}\n".encode())

    def dismiss(self) -> None:
        """Tell the guardian the main process has cleaned up, and let it exit."""
        self.send("done")
        os.close(self.w)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Register the lab-display options on the subparser."""
    ap.add_argument("host", nargs="?", help="lab PC (default: choose from the booted ones)")
    ap.add_argument("res", nargs="?", help="WxH (default: the lab PC's current resolution)")
    ap.add_argument("--fps", help="frames per second (config: fps)")
    ap.add_argument("--bitrate", help="video bitrate, e.g. 8M (config: bitrate)")
    ap.add_argument("--hidpi", choices=["on", "off"], help="macOS: HiDPI virtual screen (config: hidpi)")
    ap.add_argument("--encoder", help="Linux: GStreamer H.264 encoder element (config: encoder)")


def main(args: argparse.Namespace, ctx: Context) -> int | None:
    """Stream a local virtual monitor to the lab PC until Ctrl-C or until the stream ends."""
    cfg = ctx.settings
    fps, bitrate = args.fps or cfg["fps"], args.bitrate or cfg["bitrate"]
    if not fps.isdigit() or int(fps) < 1:
        die(f"bad fps: {fps}")
    bits_per_second(bitrate)

    host: str = args.host or pick_pc_interactive(booted_pcs(ctx))
    ssh = lab_ssh(ctx, host)

    # --- Find the environment of your graphical session on the lab PC ---
    log(f"probing desktop session on {host}...")
    probe = run(ssh + ["bash", "-s"], input=PROBE_SCRIPT, capture_output=True)
    if probe.returncode != 0:
        die(f"no graphical session of {ctx.lab_user} found on {host} - log in at the PC first"
            + (f" ({probe.stderr.strip()})" if probe.returncode == 255 else ""))
    session_env = probe.stdout.split()
    remote_env = "env " + " ".join(shlex.quote(kv) for kv in session_env)
    log(f"session: {' '.join(session_env)}")

    if run(ssh + ["command -v ffplay >/dev/null"]).returncode != 0:
        die(f"ffplay not found on {host}")

    res: str | None = args.res
    if not res:
        out = run(ssh + [f"{remote_env} xrandr --current 2>/dev/null"], capture_output=True).stdout
        m = re.search(r"^\s+(\d+x\d+)\s.*\*", out, re.M)
        res = m.group(1) if m else "1920x1080"
    if not re.fullmatch(r"\d+x\d+", res):
        die(f"bad resolution: {res}")
    log(f"lab resolution: {res}")

    stream = Stream(res, fps, bitrate, args.hidpi or cfg["hidpi"], args.encoder or cfg["encoder"])
    backend = make_backend(ctx, stream)
    backend.prepare()
    run(ssh + [remote_cleanup_cmd()])

    guardian = Guardian(ctx, host, backend)
    procs: list[subprocess.Popen[bytes]] = []

    def on_signal(signum: int, frame: FrameType | None) -> None:
        """Turn SIGTERM/SIGHUP into the same orderly shutdown as Ctrl-C."""
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    try:
        capture = backend.start()
        procs.append(capture)
        guardian.send(f"pid {capture.pid}")
        capture_out: IO[bytes] | None = capture.stdout
        player = subprocess.Popen(
            ssh + [f"{remote_env} ffplay -hide_banner -loglevel error -fs -noborder -an -autoexit "
                   f"-fflags nobuffer -flags low_delay -framedrop -probesize 32 -analyzeduration 0 "
                   f"-window_title {SCREEN_NAME} -i -"],
            stdin=capture_out)
        if capture_out:
            capture_out.close()
        procs.append(player)
        guardian.send(f"pid {player.pid}")
        log(f"streaming to {host} - Ctrl-C to stop ({backend.hint})")

        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        log("stream ended")
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        log("cleaning up...")
        cleanup(ctx, host, backend, [p.pid for p in procs if p.poll() is None])
        guardian.dismiss()
    return None
