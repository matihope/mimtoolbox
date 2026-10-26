# MIM Toolbox

Tools for using the computer infrastructure at MIMUW from your own machine, behind one command:

```
mimtoolbox.py <command> [options]
```

| command       | what it does                                                         |
|---------------|----------------------------------------------------------------------|
| `setup`       | create/update this machine's config, set up SSH key logins           |
| `print`       | print local files on the student printer (through a lab PC)          |
| `lab-display` | use a lab PC's monitor as an extra display for this computer         |
| `rooms`       | find free rooms in the MIM building, by kind, size, floor and time   |

`mimtoolbox.py --help` and `mimtoolbox.py <command> --help` list all options.

## `setup`: per-machine config

`setup` asks for each setting and writes `~/.mimtoolbox.config` (to keep it somewhere else, set
`MIMTOOLBOX_CONFIG`). Enter keeps the value in brackets and `-` clears an optional one. Afterwards, if
SSH key login to `students` doesn't work yet, it offers to set it up so you don't have to type
passwords. The lab PCs share your home directory with `students`, so the same key works there too.
Run it again at any time to change a setting, or edit the file by hand:

```ini
[common]
# your MIMUW login (required)
lab_user = ab123456

[print]
# empty = the lab PC's default (drukarka_studencka)
printer =

[lab-display]
# latency (default) or quality, see lab-display below
priority = latency
# empty = chosen by priority
fps =
bitrate =
# macOS only: HiDPI virtual screen
hidpi = off
# Linux only: GStreamer H.264 encoder, empty = chosen by priority
encoder =

[rooms]
# UW CAS identifier (usually your PESEL), empty = asked when logging in
cas_user =
```

`setup` stores only values that differ from the defaults, so your file holds just your own choices.
That way a better default in a later version reaches you too. The example above lists every
setting with its default.

`setup` only asks about settings that apply to your OS: `hidpi` on macOS, `encoder` on Linux.
Comments must be on their own lines; text after a value counts as part of the value. The other
commands refuse to run until `lab_user` is set. Command-line flags override the config. All ssh
connections go through `students.mimuw.edu.pl`, which is built in.

## `print`: the student printer

You can't print from `students` itself, but you can from the lab PCs. So `print` picks a booted lab
PC, copies your files there through `students`, runs `lp` on them and removes the copies.

To avoid password prompts, it remembers the lab PCs that let you in with your SSH key
(`~/.cache/mimtoolbox/lab-pcs`). Each run it asks `students` which PCs are booted and compares that
list with the remembered ones:

1. It tries up to three remembered PCs that are booted right now, most recently used first.
2. If none of them takes the key, it picks a random booted PC, which asks for your password once.
   That PC is remembered from then on.

Switched-off PCs are never tried, so you don't wait for timeouts. They stay on the list for later.
A remembered PC that's booted but rejects the key is dropped from the list.

```sh
mimtoolbox.py print notes.pdf -1              # one-sided (the printer duplexes by default)
mimtoolbox.py print slides.pdf -N 4 -P 1-12   # 4 slides per side, first 12 pages only
mimtoolbox.py print a.pdf b.pdf -n 2 --collate
mimtoolbox.py print code.py --prettyprint     # header + highlighting for text/code
mimtoolbox.py print x.pdf --dry-run           # show the ssh/scp/lp commands, print nothing
mimtoolbox.py print --quota                   # pages left this period
mimtoolbox.py print --status                  # the print queue
mimtoolbox.py print --list-options            # printer-specific options, usable with -o NAME=VALUE
```

Printer defaults: A4, grayscale, two-sided long edge. Use `-H <pc>` to print from a specific lab PC.

## `lab-display`: a lab monitor as an extra screen

This creates a virtual monitor on your computer and streams it as H.264 over ssh to a fullscreen
player (mpv, or `ffplay` if mpv isn't installed) in your desktop session on the lab PC. It is view-only: you keep using your own keyboard
and mouse, and the lab monitor acts as an extra screen.

**You must already be logged in on the lab PC's desktop, in person, with the same `lab_user`.**
`lab-display` can't start a graphical session itself. It finds your running session by looking
for a process of yours on that PC that has `DISPLAY`/`WAYLAND_DISPLAY` set, then opens the player
window there. If nobody is logged in, or someone else is, it stops with
`no graphical session of <user> found on <pc> - log in at the PC first`. If you log out at the PC,
the stream ends.

1. Sit down at a lab PC and log in to its desktop.
2. On your own computer, run one of:

   ```sh
   mimtoolbox.py lab-display                    # the PC where you're logged in, at its resolution
   mimtoolbox.py lab-display <pc>               # a specific PC
   mimtoolbox.py lab-display <pc> 1920x1080     # a fixed resolution
   mimtoolbox.py lab-display --quality          # sharper picture, more delay
   mimtoolbox.py lab-display --fps 30 --bitrate 8M
   ```

3. Press Ctrl-C to stop. The virtual monitor and the remote player are cleaned up even if the
   process is killed.

**Latency or quality.** `--latency` (the default) and `--quality` choose how the picture is encoded.
You can also set `priority` in the config:

| priority | settings | delay* | cost |
|---|---|---|---|
| `latency` | 60 fps, 16 Mbit/s, x264 on the CPU (`zerolatency`) | ~30 ms | about one CPU core |
| `quality` | 30 fps, 12 Mbit/s, the hardware encoder (VideoToolbox / VA-API) | ~200 ms | almost no CPU, sharper |

\* Measured with a test pattern, from encoding on a Mac to the player on a lab PC, over the
faculty network. Screen capture and the monitor itself add more on top. The hardware encoders hold
back a few frames, which is where most of `quality`'s extra delay comes from. `--fps`, `--bitrate`
and `--encoder` override what the priority chose.

**Player.** On the lab PC, lab-display uses mpv in its low-latency, untimed mode, which shows each
frame as soon as it's decoded: about 28 ms in the test above, against 40–80 ms with occasional
stalls for `ffplay`, which paces frames and checks for new ones only every 10 ms. If mpv isn't
installed it falls back to `ffplay`; `--player ffplay` forces that.

**How it finds your PC.** Every desktop login on a lab PC adds that PC to `~/.Xauthority`, which is
in your home directory shared with `students`. So one call to `students` gives your login history
and the list of booted PCs. lab-display first checks the PC you logged in to most recently. If your
session there is gone, it checks the other booted PCs from the history. If several have your
session, it asks which one to use (with fzf if it's installed). These checks use only your SSH key,
which a lab PC accepts while you're logged in there, so they never ask for a password. The PC it
finds is also remembered for `print`. To skip the search, name the PC.

Before the first run, install what's listed under [Requirements](#requirements). Then:

- **macOS:** give your terminal the *Screen Recording* permission (System Settings → Privacy &
  Security). Without it the lab screen stays black.
- **Linux:** the first run shows a screen-sharing dialog. Later runs skip it, because a restore
  token is saved in `~/.local/state/mimtoolbox/`.
- If you have several H.264 encoders, choose one with `--encoder` or the `encoder` setting.

## `rooms`: free rooms in the MIM building

`rooms` lists the teaching rooms of the MIM building (Banacha 2) with the times they're free on a
given day. It reads the timetable of each room from [SRS](https://srs.uw.edu.pl), the UW room booking
system, which covers classes as well as exams and one-off bookings. Office rooms aren't included.

```sh
mimtoolbox.py rooms                                  # today, 08:00-21:00
mimtoolbox.py rooms --now --lab                      # labs free right now, and until when
mimtoolbox.py rooms -d tomorrow --from 12 --to 16 --min-free 90m
mimtoolbox.py rooms -d fri --lecture --min-seats 150
mimtoolbox.py rooms --classroom --floor 3 --floor 4
mimtoolbox.py rooms --plain                          # plain columns, for other programs
```

```
Fri 23 Oct 2026 · 08:00–21:00 · free for 30 min or more
╭──────┬───────┬───────────┬────────────────────────────┬───────────────────────────────────────┬─────────────────────────╮
│ Room │ Seats │ Kind      │ 8   10  12  14  16  18  20 │ Free                                  │ Equipment               │
├──────┼───────┼───────────┼────────────────────────────┼───────────────────────────────────────┼─────────────────────────┤
│ 2041 │    15 │ lab       │ █░░░░░░░░░░░░░░░░░░█░░░███ │ 08:00–08:30  17:30–18:00  19:30–21:00 │ projector screen blinds │
│ 3042 │    15 │ lab       │ █░░░░░░░░░░░██████████████ │ 08:00–08:30  14:00–21:00              │ projector screen blinds │
│ 5490 │    15 │ lab       │ ████░░░░░░░░██████████████ │ 08:00–10:00  14:00–21:00              │ ethernet blinds         │
╰──────┴───────┴───────────┴────────────────────────────┴───────────────────────────────────────┴─────────────────────────╯
3 rooms · █ free  ░ busy, 30 min per mark
```

**Equipment** comes from SRS's room attributes: `projector`, `screen` (for the projector), `ethernet`
(network sockets for a cable), `blinds`, `sound` (speakers, sound system on request) and `multimedia`
(a full multimedia set), with `–` for none. Two attributes aren't listed: the computer workstations,
since they're what makes a room a lab, and wifi, which the whole building has. The data is entered by
hand in SRS, so it can be incomplete.

The timeline shows the day at a glance; each mark counts as free when at least half of its time is.
The table fits itself to the terminal: spare width goes to the equipment, then the free times, and
both wrap when space is short. The timeline needs about 115 columns next to the equipment, so on a
narrower terminal it's left out; below about 75 columns, so is the equipment. On a terminal the table
is in color (unless `NO_COLOR` is set), and where box-drawing characters can't be shown it's drawn in
ASCII.

`--plain` prints tab-separated values instead, for other programs: a header line, then one line per
room. The free times (`HH:MM-HH:MM`) and the equipment are separated by commas; seats and equipment
are empty when unknown or none. Room names can contain spaces (`2180 (sala RW)`), so split on tabs
only. When no room is free, there's just the header. Columns may be added at the end in the future,
so read them by name:

```
room	seats	kind	free	equipment
2041	15	lab	08:00-08:30,17:30-18:00,19:30-21:00	projector,screen,blinds
2180 (sala RW)	100	lecture	08:00-21:00	blinds,sound
5450	24	classroom	08:00-10:15,16:00-21:00	
```

```sh
mimtoolbox.py rooms --plain | cut -f1,4                      # room and free times
mimtoolbox.py rooms --plain --now | awk -F'\t' 'NR > 1 && $5 ~ /projector/ {print $1}'
```

```python
import csv, subprocess
out = subprocess.run(["mimtoolbox.py", "rooms", "--plain"], capture_output=True, text=True).stdout
rooms = list(csv.DictReader(out.splitlines(), delimiter="\t"))
```

- **Kinds:** `--lab` are rooms with computer workstations, `--lecture` rooms with 100 or more seats,
  `--classroom` the rest. They can be combined, e.g. `--lab --classroom`.
- **When:** `-d/--date` takes `YYYY-MM-DD`, `today` (the default), `tomorrow` or a weekday (`fri`,
  `monday`: today or the next one). `--from`/`--to` set the window (default 08:00-21:00). `--now`
  shows only rooms that are free right now.
- **How long:** free times shorter than `--min-free` (default `30m`) aren't listed, so the breaks
  between classes don't clutter the list. It takes `45m`, `2h`, `1h30` and so on.
- **Which rooms:** `--min-seats N` and `--floor N`. The floor is the first digit of the room number,
  e.g. 3041 is on floor 3.

**Logging in.** SRS needs a UW CAS login, the one you use at `login.uw.edu.pl`. Your identifier there
is usually your PESEL, not your MIMUW login. `rooms` asks for the password, and the identifier unless
`cas_user` is set, only when it has no valid session. Neither is ever written to disk. The session
cookies are kept in `~/.cache/mimtoolbox/srs-cookies*`, readable only by you, so later runs don't ask
again until the session expires. `rooms --logout` deletes them.

**Speed.** SRS answers only one request per session at a time, and each room's timetable is a
separate request of about half a second, so one session needs about 15 s for the whole building.
`rooms` therefore fetches over up to 12 sessions at once, all starting right away with their saved
cookies. The first one is the one you logged in with. The others come from CAS single sign-on, so
they don't need the password, and are saved too (`srs-cookies.1` and so on). A session that has
expired renews itself the same way; only if none of them can is the password asked for. If single
sign-on has expired while some sessions still work, the tool uses fewer sessions and says so;
`--logout` makes the next run log in afresh. Logging in also skips SRS's front page, which is big and
slow to load and not needed.

The room list changes rarely, so it's cached for a week in `~/.cache/mimtoolbox/srs-rooms.json`.
`--refresh-rooms` fetches it again. The timetables are fetched fresh on every run.

## Requirements

mimtoolbox uses only the Python standard library, so there's nothing to `pip install`. It calls the
programs below, which you install once per machine. Everything on the MIMUW side (`students` and the
lab PCs) is provided by the faculty.

**macOS** (Homebrew; `ssh`, `scp` and `ssh-copy-id` come with the system):

```sh
brew install python fzf
brew install ffmpeg && brew install --cask betterdisplay    # lab-display only
```

**Ubuntu / Debian:**

```sh
sudo apt install python3 openssh-client fzf
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-pipewire python3-gi    # lab-display only
```

**Arch:**

```sh
sudo pacman -S --needed python openssh fzf
sudo pacman -S --needed gstreamer gst-plugins-base gst-plugins-bad gst-plugin-va gst-plugins-ugly gst-plugin-pipewire python-gobject    # lab-display only
```

If you don't have an SSH key yet, create one with `ssh-keygen -t ed25519`.

What each program is for:

| program | needed for |
|---|---|
| Python 3.8+ (`python3`) | everything |
| OpenSSH client (`ssh`, `scp`, `ssh-copy-id`) | everything; `ssh-copy-id` only for `setup` |
| an SSH key (`~/.ssh/id_ed25519` or similar) | passwordless logins set up by `setup` |
| `fzf` *(optional)* | picking a lab PC from a fuzzy list; without it you get a numbered list |
| [BetterDisplay](https://github.com/waydabber/BetterDisplay) with *CLI integration* enabled in its settings | `lab-display` on macOS: the virtual screen |
| `ffmpeg` (with AVFoundation, libx264 and VideoToolbox, as in Homebrew's build) | `lab-display` on macOS: capture and encoding |
| GStreamer and PyGObject (table below) | `lab-display` on Linux |

`print` needs nothing beyond the common programs, and `rooms` needs only Python: it talks to
`srs.uw.edu.pl` and `login.uw.edu.pl` over HTTPS itself.

`lab-display` on Linux needs a Wayland session whose ScreenCast portal can create virtual monitors
(e.g. GNOME). The packages in the commands above provide:

| program / plugin | Arch | Debian / Ubuntu |
|---|---|---|
| `gst-launch-1.0`, `gst-inspect-1.0` | `gstreamer` | `gstreamer1.0-tools` |
| `videoconvert`, `videorate` | `gst-plugins-base` | `gstreamer1.0-plugins-base` |
| `pipewiresrc` | `gst-plugin-pipewire` | `gstreamer1.0-pipewire` |
| `h264parse`, `mpegtsmux` | `gst-plugins-bad` | `gstreamer1.0-plugins-bad` |
| H.264 encoder: `vah264lpenc`/`vah264enc` (hardware, preferred) or `x264enc` (software fallback) | `gst-plugin-va`, `gst-plugins-ugly` | `gstreamer1.0-plugins-bad`, `gstreamer1.0-plugins-ugly` |
| PyGObject (`import gi`) | `python-gobject` | `python3-gi` |

The hardware encoders also need your GPU's VA-API driver (e.g. `intel-media-driver` /
`intel-media-va-driver` for Intel, Mesa for AMD). Without it, the `x264enc` software encoder is
used.

**On the MIMUW side**, which you don't install yourself but the tools rely on:

- On `students`: the `lk_booted_pcs` command, plus `lk_print_quota` for `print --quota`.
- On the lab PCs: `lp`, `lpstat` and `lpoptions` for `print`; `mpv` (or `ffplay`), `bash`,
  `pgrep`/`pkill` and `xrandr` for `lab-display`. `xrandr` only detects the resolution; without it you get 1920x1080.

## Install

1. Clone the repo anywhere and put that directory on your `PATH`. The only executable in it is
   `mimtoolbox.py`, so nothing else gets onto your `PATH`.

   ```sh
   git clone git@github.com:matihope/mim-toolbox.git ~/mim-toolbox
   # fish
   fish_add_path ~/mim-toolbox
   # bash / zsh (add to ~/.bashrc or ~/.zshrc)
   export PATH="$HOME/mim-toolbox:$PATH"
   ```

2. Run the setup once on every machine you use:

   ```sh
   mimtoolbox.py setup
   ```

## Adding a tool

Drop a module into `mimtoolbox/tools/`. It becomes a subcommand automatically if it defines:

```python
"""Long description, shown at the end of `mimtoolbox.py <command> --help`."""
from __future__ import annotations
import argparse
from typing import Mapping
from mimtoolbox.core import ConfigField, Context, log

COMMAND: str = "hello"
HELP: str = "One-line description."
CONFIG: Mapping[str, ConfigField] = {    # stored in the [hello] section; asked for by `setup`
    "greeting": ConfigField("hi", "what to say"),
}

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the tool's options."""
    parser.add_argument("--loud", action="store_true")

def main(args: argparse.Namespace, ctx: Context) -> int | None:
    """Run the tool; the return value is the exit status."""
    log(ctx.settings["greeting"] + (" !!!" if args.loud else ""))
    return 0
```

`mimtoolbox/core.py` has the shared helpers: `log`, `die`, `run` (with dry-run support), and the
ssh helpers `lab_ssh`, `lab_ssh_opts`, `close_master`, `booted_pcs`, `pick_pc_interactive`,
`key_login_works` and `copy_ssh_key`. `Context` carries `lab_user`, `jump`
(`user@students.mimuw.edu.pl`), the tool's `settings`, `cache_dir` and `state_dir`.

Add `tests/test_<tool>.py` for the new tool, and add its command to the list in
`tests/test_cli.py` (`test_finds_all_tools`).

## Tests

Run these from the repository root:

```sh
python3 -m unittest        # add -v to see each test's description
```

The suite uses only the standard library and runs in well under a second. It never starts real
processes or sends HTTP requests: any attempt to run `ssh`, `scp` or `lp`, or to reach SRS, fails the
test. `HOME`, the config file and the cache are redirected to a temporary directory, so running it
doesn't touch the lab, the printer, SRS or your own settings.

`tests/test_style.py` checks that every module, class and function, including the tests, has a
docstring and type hints. To check that the hints are correct, run
[mypy](https://mypy-lang.org) (needs Python 3.10+):

```sh
python3 -m venv /tmp/mypy-venv && /tmp/mypy-venv/bin/pip install mypy
/tmp/mypy-venv/bin/mypy --strict mimtoolbox tests
```
