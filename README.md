# mim-toolbox

Tools for using the MIMUW lab computers from your own machine, behind one command:

```
mimtoolbox.py <command> [options]
```

| command       | what it does                                                         |
|---------------|----------------------------------------------------------------------|
| `setup`       | create/update this machine's config, set up SSH key logins           |
| `print`       | print local files on the student printer (through a lab PC)          |
| `lab-display` | use a lab PC's monitor as an extra display for this computer         |

`mimtoolbox.py --help` and `mimtoolbox.py <command> --help` list all options.

## Install

Needs Python 3.8+ and `ssh`. Nothing to `pip install`.

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

## `setup`: per-machine config

`setup` asks for each setting and writes `~/.mimtoolbox.config` (to keep it somewhere else, set
`MIMTOOLBOX_CONFIG`). Enter keeps the value in brackets and `-` clears an optional one. Afterwards it
offers to set up SSH key login to `students` and to a lab PC, so you don't have to type passwords.
Run it again at any time to change a setting, or edit the file by hand:

```ini
[common]
lab_user = ab123456              # your MIMUW login (required)
jump = students.mimuw.edu.pl     # the host that reaches the lab PCs

[print]
printer =                        # empty = the lab PC's default (drukarka_studencka)

[lab-display]
fps = 30
bitrate = 8M
hidpi = off                      # macOS: HiDPI virtual screen
encoder =                        # Linux: GStreamer H.264 encoder, empty = best available
```

Other commands refuse to run until `lab_user` is set. Command-line flags override the config.

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

Requirements:

- **macOS:** [BetterDisplay](https://github.com/waydabber/BetterDisplay) with CLI integration
  enabled, and `ffmpeg` (`brew install ffmpeg`). Give your terminal the Screen Recording permission.
- **Linux:** a Wayland session whose ScreenCast portal supports virtual monitors (GNOME), GStreamer
  with `pipewiresrc` and an H.264 encoder (`gst-plugin-va` for hardware encoding), and PyGObject.
  The first run shows a screen-sharing dialog; later runs skip it.
- **Lab PC:** `ffplay` (installed in the labs).

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
ssh helpers `lab_ssh`, `lab_ssh_opts`, `close_master`, `booted_pcs`, `pick_pc_interactive` and
`copy_ssh_key`. `Context` carries `lab_user`, `jump`, the tool's `settings`, `cache_dir` and
`state_dir`. Use type hints and docstrings. Check the code with
`mypy --strict mimtoolbox`.
