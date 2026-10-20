"""Print local files on the MIMUW student printer.

students can't print, but lab PCs can. So a booted lab PC is picked (from lk_booted_pcs), the files
are scp'd through students into a fresh job dir under ~/prints (the shared NFS home), `lp` prints
them there, and the job dir is removed afterwards (~/prints too, if it ends up empty).

Printer defaults: A4, grayscale, two-sided long edge.

Examples:
  mimtoolbox.py print notes.pdf -1              one page per sheet (the printer defaults to duplex)
  mimtoolbox.py print slides.pdf -N 4 -P 1-12   4 slides per side, first 12 pages only
  mimtoolbox.py print a.pdf b.pdf -n 2 --collate
  mimtoolbox.py print x.pdf -o some-option=value --dry-run
  mimtoolbox.py print --list-options            printer-specific options reported by `lpoptions -l`
  mimtoolbox.py print --quota                   pages left this period

The lab PC that worked last is remembered in ~/.cache/mimtoolbox/print-host.
"""

from __future__ import annotations

import argparse
import pathlib
import random
import shlex
import subprocess
from typing import Any, Mapping, Sequence

from mimtoolbox.core import ConfigField, Context, booted_pcs, close_master, die, lab_ssh, lab_ssh_opts, log, run

COMMAND: str = "print"
HELP: str = "Print using the MIM student printer (via students + a lab PC)."
CONFIG: Mapping[str, ConfigField] = {
    "printer": ConfigField("", "default printer (empty = the lab PC's default, drukarka_studencka)"),
}

PRINTS_DIR: str = "prints"  # relative to the (shared) home dir

# (IPP sides, driver Duplex): the printer ignores `sides` alone and duplexes by its driver default.
SIDES: Mapping[str, tuple[str, str]] = {
    "one": ("one-sided", "None"), "long": ("two-sided-long-edge", "DuplexNoTumble"),
    "short": ("two-sided-short-edge", "DuplexTumble")}
QUALITY: Mapping[str, int] = {"draft": 3, "normal": 4, "high": 5}
ORIENTATION: Mapping[str, int] = {"portrait": 3, "landscape": 4, "reverse-landscape": 5, "reverse-portrait": 6}


def host_cache(ctx: Context) -> pathlib.Path:
    """File remembering the lab PC that worked last."""
    return ctx.cache_dir / "print-host"


class Lab:
    """A lab PC to run commands on, through this tool's ssh master connection."""

    def __init__(self, ctx: Context, host: str, batch: bool = True, dry_run: bool = False) -> None:
        """
        Args:
            batch: Fail instead of asking for a password (for a PC known to take our key).
            dry_run: Print the commands instead of running them.
        """
        self.ctx, self.host, self.batch, self.dry_run = ctx, host, batch, dry_run

    def ssh(self, cmd: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        """Run a shell command on the PC; exits (and forgets the PC) if ssh itself fails."""
        res = run(lab_ssh(self.ctx, self.host, self.batch, connect_timeout=8) + [cmd], dry_run=self.dry_run, **kw)
        if res.returncode == 255:  # ssh itself failed; anything else came from cmd
            forget_host(self.ctx, self.host)
            die(f"ssh to {self.host} failed" + (f": {res.stderr.strip()}" if res.stderr else ""))
        return res

    def scp(self, files: Sequence[pathlib.Path], dest: str) -> subprocess.CompletedProcess[str]:
        """Copy local files to `dest` (relative to the home dir) on the PC."""
        return run(["scp", "-q", *lab_ssh_opts(self.ctx, self.batch, connect_timeout=8), *map(str, files),
                    f"{self.ctx.lab_user}@{self.host}:{dest}"], dry_run=self.dry_run)

    def close(self) -> None:
        """Close the master connection."""
        close_master(self.ctx, self.host, self.dry_run)


def can_login(ctx: Context, host: str) -> bool:
    """Whether the PC lets us in without a password."""
    return run(lab_ssh(ctx, host, batch=True, connect_timeout=8) + ["true"], capture_output=True).returncode == 0


def forget_host(ctx: Context, host: str) -> None:
    """Drop `host` from the cache if it's the remembered one."""
    try:
        if host_cache(ctx).read_text().strip() == host:
            host_cache(ctx).unlink()
    except OSError:
        pass


def connect(ctx: Context, host: str | None, dry_run: bool = False) -> Lab:
    """Choose the lab PC to use.

    `host`, else the last PC that worked if it still takes our key (no prompt), else a random booted PC;
    ssh asks for the password there once, after which key logins to it work too.
    """
    if host or dry_run:
        return Lab(ctx, host or "<lab-pc>", batch=False, dry_run=dry_run)
    cache = host_cache(ctx)
    try:
        cached = cache.read_text().strip()
    except OSError:
        cached = ""
    if cached and can_login(ctx, cached):
        return Lab(ctx, cached)
    lab = Lab(ctx, random.choice([h for h in booted_pcs(ctx) if h != cached]), batch=False)
    log(f"using {lab.host}; it may ask for your password once")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(lab.host + "\n")
    return lab


def lp_args(a: argparse.Namespace) -> list[str]:
    """The `lp` command (without files) for the parsed options."""
    cmd = ["lp"]
    if a.printer:
        cmd += ["-d", a.printer]
    if a.copies != 1:
        cmd += ["-n", str(a.copies)]
    if a.pages:
        cmd += ["-P", a.pages]
    if a.title:
        cmd += ["-t", a.title]
    if a.priority is not None:
        cmd += ["-q", str(a.priority)]
    opts = []
    if a.sides:
        sides, duplex = SIDES[a.sides]
        opts += [f"sides={sides}", f"Duplex={duplex}"]
    if a.number_up:
        opts.append(f"number-up={a.number_up}")
    if a.number_up_layout:
        opts.append(f"number-up-layout={a.number_up_layout}")
    if a.page_border:
        opts.append(f"page-border={a.page_border}")
    if a.orientation:
        opts.append(f"orientation-requested={ORIENTATION[a.orientation]}")
    if a.media:
        opts.append(f"media={a.media}")
    if a.fit_to_page:
        opts.append("fit-to-page")
    if a.page_set:
        opts.append(f"page-set={a.page_set}")
    if a.collate:
        opts.append("collate=true")
    if a.reverse:
        opts.append("outputorder=reverse")
    if a.mirror:
        opts.append("mirror")
    if a.quality:
        opts.append(f"print-quality={QUALITY[a.quality]}")
    if a.cpi:
        opts.append(f"cpi={a.cpi}")
    if a.lpi:
        opts.append(f"lpi={a.lpi}")
    if a.prettyprint:
        opts.append("prettyprint")
    opts += a.option
    for o in opts:
        cmd += ["-o", o]
    return cmd


def print_files(ctx: Context, a: argparse.Namespace) -> None:
    """Copy `a.files` to a lab PC, print them there with `lp`, and clean up."""
    names = [f.name for f in a.files]
    if len(set(names)) != len(names):
        die("files must have distinct names")
    if not a.title and len(a.files) == 1:
        a.title = a.files[0].name

    lab = connect(ctx, a.host, a.dry_run)
    try:
        # All file operations run on the lab PC itself: it sees the same NFS /home as students, but
        # files written on students can stay invisible to its NFS cache for up to a minute.
        res = lab.ssh(f"mkdir -p {PRINTS_DIR} && mktemp -d {PRINTS_DIR}/job.XXXXXX", capture_output=True)
        if res.returncode != 0:
            die(f"could not create job dir on {lab.host}: {res.stderr.strip()}")
        job_dir = res.stdout.strip() or f"{PRINTS_DIR}/job.XXXXXX"
        try:
            if lab.scp(a.files, f"{job_dir}/").returncode != 0:
                die(f"scp to {lab.host} failed")
            res = lab.ssh(shlex.join(lp_args(a) + ["--"] + [f"{job_dir}/{n}" for n in names]),
                          capture_output=not a.dry_run)
            if res.returncode != 0:
                die(f"lp on {lab.host} failed: {(res.stderr or res.stdout).strip()}")
            if not a.dry_run:
                log(f"printed via {lab.host}: {res.stdout.strip()}")
        finally:
            # lp has already spooled the data, so it's safe to remove right away.
            lab.ssh(f"rm -rf -- {shlex.quote(job_dir)}; rmdir {PRINTS_DIR} 2>/dev/null; true")
    finally:
        lab.close()


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Register the print options (mostly `lp` options) on the subparser."""
    ap.add_argument("files", nargs="*", type=pathlib.Path, metavar="file")

    g = ap.add_argument_group("layout")
    g.add_argument("-s", "--sides", choices=SIDES, help="one = each page on its own sheet, "
                   "long = duplex portrait (printer default), short = duplex landscape")
    g.add_argument("-1", "--one-sided", dest="sides", action="store_const", const="one",
                   help="same as --sides one")
    g.add_argument("-N", "--number-up", type=int, choices=[1, 2, 4, 6, 9, 16], help="pages per side")
    g.add_argument("--number-up-layout", choices=["lrtb", "lrbt", "rltb", "rlbt", "tblr", "tbrl", "btlr", "btrl"],
                   help="page order for --number-up (default lrtb)")
    g.add_argument("--page-border", choices=["none", "single", "single-thick", "double", "double-thick"],
                   help="border around each page with --number-up")
    g.add_argument("-O", "--orientation", choices=ORIENTATION)
    g.add_argument("-l", "--landscape", dest="orientation", action="store_const", const="landscape",
                   help="same as --orientation landscape")
    g.add_argument("-m", "--media", help="paper size, e.g. A4 (default), Letter, Legal")
    g.add_argument("-f", "--fit-to-page", action="store_true", help="scale to fit the paper")
    g.add_argument("--mirror", action="store_true", help="mirror each page")

    g = ap.add_argument_group("pages and copies")
    g.add_argument("-P", "--pages", help="page ranges, e.g. 1-4,7,9-12")
    g.add_argument("--page-set", choices=["odd", "even"], help="print only odd or even pages")
    g.add_argument("-n", "--copies", type=int, default=1)
    g.add_argument("-c", "--collate", action="store_true", help="collate copies")
    g.add_argument("-r", "--reverse", action="store_true", help="reverse output order")

    g = ap.add_argument_group("quality and text files")
    g.add_argument("-Q", "--quality", choices=QUALITY)
    g.add_argument("--cpi", type=float, help="characters per inch (text files)")
    g.add_argument("--lpi", type=float, help="lines per inch (text files)")
    g.add_argument("--prettyprint", action="store_true", help="header + highlighting for text/code files")

    g = ap.add_argument_group("job")
    g.add_argument("-t", "--title", help="job title (default: file name)")
    g.add_argument("-d", "--printer", help="destination (default: `printer` from the config, else the lab PC's "
                   "default, drukarka_studencka)")
    g.add_argument("-q", "--priority", type=int, choices=range(1, 101), metavar="1-100")
    g.add_argument("-o", "--option", action="append", default=[], metavar="NAME[=VALUE]",
                   help="raw lp -o option, repeatable (see --list-options)")

    g = ap.add_argument_group("connection")
    g.add_argument("-H", "--host", help="lab PC to print from (default: the last one used, else a random booted one)")
    g.add_argument("--dry-run", action="store_true", help="show the commands instead of running them")
    g.add_argument("--list-options", action="store_true", help="show the printer's options (lpoptions -l)")
    g.add_argument("--status", action="store_true", help="show the print queue (lpstat -o)")
    g.add_argument("--quota", action="store_true", help="show your print quota (lk_print_quota on students)")


def main(a: argparse.Namespace, ctx: Context) -> int | None:
    """Print the given files, or run one of the query options (--quota, --list-options, --status)."""
    a.printer = a.printer or ctx.settings["printer"] or None
    if a.quota:
        return run(["ssh", ctx.jump, "lk_print_quota"], dry_run=a.dry_run).returncode
    if a.list_options or a.status:
        cmd = "lpoptions -l" if a.list_options else "lpstat -o"
        if a.printer:
            cmd += f" -p {shlex.quote(a.printer)}" if a.list_options else f" {shlex.quote(a.printer)}"
        lab = connect(ctx, a.host, a.dry_run)
        try:
            res = lab.ssh(cmd, capture_output=not a.dry_run)
            print(res.stdout or res.stderr, end="")
        finally:
            lab.close()
        return None
    if not a.files:
        die("no files given")
    for f in a.files:
        if not f.is_file():
            die(f"not a file: {f}")
    if a.copies < 1:
        die("--copies must be at least 1")
    print_files(ctx, a)
    return None
