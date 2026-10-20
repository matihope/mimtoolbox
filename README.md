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
fps = 30
bitrate = 8M
# macOS only: HiDPI virtual screen
hidpi = off
# Linux only: GStreamer H.264 encoder, empty = best available
encoder =
```

`setup` only asks about settings that apply to your OS: `hidpi` on macOS, `encoder` on Linux.
Comments must be on their own lines; text after a value counts as part of the value. The other
commands refuse to run until `lab_user` is set. Command-line flags override the config. All
connections go through `students.mimuw.edu.pl`, which is built in.

## `print`: the student printer

You can't print from `students` itself, but you can from the lab PCs. So `print` picks a booted lab
PC, copies your files there through `students`, runs `lp` on them and removes the copies. It reuses
the lab PC that worked last time; otherwise it picks a random booted one, which may ask for your
password once.

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
`ffplay` in your desktop session on the lab PC. It is view-only: you keep using your own keyboard
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
   mimtoolbox.py lab-display                    # choose from the booted lab PCs (uses fzf if installed)
   mimtoolbox.py lab-display <pc>               # a specific PC, at its current resolution
   mimtoolbox.py lab-display <pc> 1920x1080     # a fixed resolution
   mimtoolbox.py lab-display --fps 60 --bitrate 12M
   ```

3. Press Ctrl-C to stop. The virtual monitor and the remote player are cleaned up even if the
   process is killed.

Before the first run, install what's listed under [Requirements](#requirements). Then:

- **macOS:** give your terminal the *Screen Recording* permission (System Settings → Privacy &
  Security). Without it the lab screen stays black.
- **Linux:** the first run shows a screen-sharing dialog. Later runs skip it, because a restore
  token is saved in `~/.local/state/mimtoolbox/`.
- If you have several H.264 encoders, choose one with `--encoder` or the `encoder` setting.

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
| `ffmpeg` (with AVFoundation and VideoToolbox, as in Homebrew's build) | `lab-display` on macOS: capture and encoding |
| GStreamer and PyGObject (table below) | `lab-display` on Linux |

`print` needs nothing beyond the common programs.

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
- On the lab PCs: `lp`, `lpstat` and `lpoptions` for `print`; `ffplay`, `bash`, `pgrep`/`pkill` and
  `xrandr` for `lab-display`. `xrandr` only detects the resolution; without it you get 1920x1080.

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
(`user@students.mimuw.edu.pl`), the tool's `settings`, `cache_dir` and `state_dir`. Use type hints
and docstrings. Check the code with
`mypy --strict mimtoolbox`.
