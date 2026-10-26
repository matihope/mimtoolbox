"""Find free rooms in the MIM building (Banacha 2), using the UW room booking system SRS (srs.uw.edu.pl).

For each teaching room it lists the free time within one day's window, according to SRS's timetable,
which has the classes as well as exams and one-off bookings. Rooms are of three kinds: lab (has
computer workstations), lecture (100 or more seats) and classroom (the rest).

Examples:
  mimtoolbox.py rooms                          today, 08:00-21:00
  mimtoolbox.py rooms --now --lab              labs that are free right now, and until when
  mimtoolbox.py rooms -d tomorrow --from 12 --to 16 --min-free 90m
  mimtoolbox.py rooms -d fri --lecture --min-seats 150
  mimtoolbox.py rooms --classroom --floor 3 --floor 4
  mimtoolbox.py rooms --plain                  tab-separated values, for other programs

SRS needs a UW CAS login (login.uw.edu.pl): your identifier, usually your PESEL, and password. They're
asked for when needed and never stored. The session cookies are kept, readable only by you, in
~/.cache/mimtoolbox/srs-cookies*, so later runs don't ask again until the session expires; --logout
deletes them. The room list is cached for a week in ~/.cache/mimtoolbox/srs-rooms.json.

SRS answers one request per session at a time, so the timetables are fetched over several sessions
at once. The extra ones come from CAS single sign-on, without asking for the password again.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import getpass
import html
import html.parser
import http.client
import http.cookiejar
import json
import os
import pathlib
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import IO, Any, Mapping, Sequence, Tuple

from mimtoolbox.core import ConfigField, Context, die, log

COMMAND: str = "rooms"
HELP: str = "Find free rooms in the MIM building (from SRS, srs.uw.edu.pl)."
CONFIG: Mapping[str, ConfigField] = {
    "cas_user": ConfigField("", "UW CAS identifier, usually your PESEL (empty = ask when logging in)"),
}

SRS_URL: str = "https://srs.uw.edu.pl/"
CAS_HOST: str = "login.uw.edu.pl"
CAS_LOGIN: str = f"https://{CAS_HOST}/cas/login"
SERVICE: str = SRS_URL + "login/loginCas"
"""Where CAS sends you back after logging in; SRS turns the ticket it gets there into a session."""
USER_AGENT: str = "mimtoolbox (https://github.com/matihope/mim-toolbox)"
"""Sent with every request. CAS ties its session cookie to the User-Agent, so it must stay the same."""
TIMEOUT: int = 20
"""Seconds to wait for SRS or CAS."""
SESSIONS: int = 12
"""How many SRS sessions to fetch timetables over. SRS handles one request per session at a time
(it locks the session), so this is how many requests run in parallel."""

BUILDING_ID: str = "3320"
"""SRS id of the MIM building (Gmach Wydziału Matematyki - Banacha 2)."""
LECTURE_SEATS: int = 100
"""Rooms with at least this many seats are lecture rooms."""
LAB_ATTR: str = "Stanowiska komputerowe"
"""Start of the (Polish) description of the room attribute that makes a room a lab: PC workstations."""
EQUIPMENT: Sequence[Tuple[str, str]] = [
    ("Projektor", "projector"), ("Ekran", "screen"), ("Gniazdka sieciowe", "ethernet"), ("Rolety", "blinds"),
    ("Nagłośnienie", "sound"), ("Pełny zestaw multimedialny", "multimedia"),
]
"""Short labels for the room attributes, by the start of their Polish description, in the order shown.

Attributes not in this list or NOT_SHOWN are shown by their English description, up to the first
comma and cut to GEAR_WIDTH.
"""
NOT_SHOWN: Sequence[str] = [LAB_ATTR, "Sieć radiowa"]
"""Attributes left out of the equipment, by the start of their Polish description: the PC workstations
make a room a lab, which the Kind column says already, and the whole building has wifi."""
GEAR_WIDTH: int = 17
"""Narrowest the Equipment column gets before the timeline is dropped (fits 'projector screen')."""
ROOMS_MAX_AGE: int = 7 * 24 * 3600
"""Seconds the cached room list is used before it's fetched again."""
MAX_PAGES: int = 20
"""Safety limit on the pages of SRS's room search."""

DAY_START: datetime.time = datetime.time(8)
DAY_END: datetime.time = datetime.time(21)
WEEKDAYS: Sequence[str] = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
"""For --date; any prefix of 3 or more letters works."""

Interval = Tuple[datetime.datetime, datetime.datetime]


@dataclass(frozen=True)
class Room:
    """A teaching room as SRS lists it."""

    id: str
    """SRS room id."""
    name: str
    """Room number, sometimes with a note, e.g. '2180 (sala RW)'."""
    seats: int
    """Capacity; 0 if SRS doesn't know it."""
    lab: bool
    """Whether it has computer workstations."""
    equipment: Tuple[str, ...] = ()
    """Short labels of its other attributes, e.g. ('projector', 'screen'); see EQUIPMENT."""

    @property
    def kind(self) -> str:
        """'lab', 'lecture' or 'classroom'."""
        if self.lab:
            return "lab"
        return "lecture" if self.seats >= LECTURE_SEATS else "classroom"

    @property
    def floor(self) -> int | None:
        """The first digit of the room number (3041 is on floor 3); None if the name doesn't start with one."""
        return int(self.name[0]) if self.name[:1].isdigit() else None


def equipment(attrs: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Short labels for a room's attributes (as filters/searchRoom lists them), except NOT_SHOWN, in
    EQUIPMENT order, with unknown ones last."""
    labels = []
    for a in attrs:
        pl = a.get("descriptionPl") or ""
        known = [i for i, (prefix, _) in enumerate(EQUIPMENT) if pl.startswith(prefix)]
        if known:
            labels.append((known[0], EQUIPMENT[known[0]][1]))
        elif not pl.startswith(tuple(NOT_SHOWN)):
            # Up to the first comma ('Projector, installed permanently'), so labels never contain one.
            label = (a.get("descriptionEn") or pl).split(",")[0].strip().lower()[:GEAR_WIDTH]
            labels.append((len(EQUIPMENT), label))
    return tuple(label for _, label in sorted(labels))


def parse_rooms(results: Sequence[Mapping[str, Any]]) -> list[Room]:
    """The rooms in the `results` of SRS's filters/searchRoom, sorted by name."""
    rooms = [Room(str(r["id"]), str(r["name"]), int(r.get("capacity") or 0),
                  any((a.get("descriptionPl") or "").startswith(LAB_ATTR) for a in r.get("attrs") or []),
                  equipment(r.get("attrs") or []))
             for r in results]
    return sorted(rooms, key=lambda r: r.name)


def wall_clock(stamp: str) -> datetime.datetime:
    """An SRS time ('2026-10-23T08:30:00+02:00') as a naive Warsaw local time.

    SRS gives Warsaw's UTC offset, so dropping it leaves the time as shown on Warsaw clocks, which is
    what the window is in too.
    """
    return datetime.datetime.fromisoformat(stamp).replace(tzinfo=None)


def free_times(events: Sequence[Mapping[str, Any]], start: datetime.datetime,
               end: datetime.datetime) -> list[Interval]:
    """The parts of [start, end) that none of the SRS events overlaps, in order.

    Every event counts, whatever its status, so a room is never shown as free when it might not be.
    Exits on an event without readable times.
    """
    busy = []
    for e in events:
        try:
            s, t = wall_clock(e["start"]), wall_clock(e["end"])
        except (KeyError, TypeError, ValueError):
            die(f"unexpected event from SRS: {str(e)[:200]}")
        busy.append((max(s, start), min(t, end)))
    free = []
    cursor = start
    for s, t in sorted(busy):
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, t)
    if cursor < end:
        free.append((cursor, end))
    return free


def parse_date(text: str, today: datetime.date | None = None) -> datetime.date:
    """Parse --date: YYYY-MM-DD, 'today', 'tomorrow', or a weekday (mon...sun, today or the next one)."""
    today = today or datetime.date.today()
    word = text.strip().lower()
    if word == "today":
        return today
    if word == "tomorrow":
        return today + datetime.timedelta(days=1)
    weekday = [i for i, name in enumerate(WEEKDAYS) if len(word) >= 3 and name.startswith(word)]
    if weekday:
        return today + datetime.timedelta(days=(weekday[0] - today.weekday()) % 7)
    try:
        return datetime.date.fromisoformat(word)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad date {text!r}: use YYYY-MM-DD, today, tomorrow or mon...sun")


def parse_clock(text: str) -> datetime.time:
    """Parse --from/--to: H, HH, H:MM or HH:MM."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", text.strip())
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
        raise argparse.ArgumentTypeError(f"bad time {text!r}: use HH:MM")
    return datetime.time(int(m[1]), int(m[2] or 0))


def parse_minutes(text: str) -> datetime.timedelta:
    """Parse --min-free: minutes ('45', '45m'), hours ('2h') or both ('1h30', '1h30m')."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m?)?", text.strip())
    if not m or not (m[1] or m[2]):
        raise argparse.ArgumentTypeError(f"bad duration {text!r}: use e.g. 45m, 2h or 1h30")
    return datetime.timedelta(hours=int(m[1] or 0), minutes=int(m[2] or 0))


class LoginForm(html.parser.HTMLParser):
    """The CAS login form (id fm1): where it's sent, and its hidden fields (e.g. the `execution` token)."""

    def __init__(self, page: str) -> None:
        """Parse the form out of a CAS page."""
        super().__init__()
        self.action = "login"
        self.fields: dict[str, str] = {}
        self._inside = False
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Note the form's action and the hidden inputs inside it."""
        a = dict(attrs)
        name = a.get("name")
        if tag == "form" and a.get("id") == "fm1":
            self._inside, self.action = True, a.get("action") or self.action
        elif tag == "input" and self._inside and a.get("type") == "hidden" and name:
            self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        """Notice the end of the form."""
        if tag == "form":
            self._inside = False


def cas_message(page: str) -> str:
    """The error CAS shows on a login page (e.g. wrong password), or '' if there's none."""
    m = re.search(r'class="banner banner-danger[^"]*"[^>]*>(.*?)</div>', page, re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m[1])).split()) if m else ""


def on_srs(url: str) -> bool:
    """Whether a URL is on SRS (and not, e.g., still on the CAS login page)."""
    return urllib.parse.urlsplit(url).hostname == urllib.parse.urlsplit(SRS_URL).hostname


def cookie_file(ctx: Context, n: int = 0) -> pathlib.Path:
    """Where the cookies of SRS session `n` are kept; the main one (0) also has the CAS cookies."""
    return ctx.cache_dir / ("srs-cookies" + (f".{n}" if n else ""))


class Refused(Exception):
    """SRS refused access: the session has expired, or never was logged in."""


def _ask(prompt: str) -> str:
    """`input()` with the prompt on stderr (stdout may be piped), exiting cleanly when stdin runs out."""
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        return input().strip()
    except EOFError:
        die("login aborted")


class StopAtFrontPage(urllib.request.HTTPRedirectHandler):
    """Follows redirects, except to SRS's front page.

    Logging in ends with a redirect there, but by then the session exists, and the page is big
    (over 100 KB) and slow to make. Not following makes urllib raise HTTPError (302), which `SRS._open`
    returns like any other answer.
    """

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: http.client.HTTPMessage, newurl: str) -> urllib.request.Request | None:
        """None (don't follow) for the front page, else what urllib normally does."""
        if urllib.parse.urldefrag(newurl)[0] == SRS_URL:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SRS:
    """A session with SRS, which logs in through UW CAS when SRS refuses access."""

    def __init__(self, ctx: Context, n: int = 0) -> None:
        """Load the saved session cookies, if there are any.

        Args:
            n: Which session: 0 is the main one, which logs in; the others get started from its CAS
                cookies with `sso`.
        """
        self.ctx, self.n = ctx, n
        self.cookie_file = cookie_file(ctx, n)
        self.jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        try:
            self.jar.load(ignore_discard=True)
        except (OSError, http.cookiejar.LoadError):
            pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), StopAtFrontPage())
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def _open(self, url: str, data: Mapping[str, str] | None = None, xhr: bool = False) -> tuple[int, str, str]:
        """Send a request (POST if `data` is given), following redirects.

        Returns:
            The HTTP status, the final URL and the body. Exits if the server can't be reached.
        """
        headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"} if xhr else {}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        try:
            with self.opener.open(urllib.request.Request(url, body, headers), timeout=TIMEOUT) as res:
                return res.getcode() or 200, res.geturl(), res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.geturl() or url, e.read().decode("utf-8", "replace")
        except OSError as e:  # also URLError and timeouts
            die(f"can't reach {urllib.parse.urlsplit(url).hostname}: {getattr(e, 'reason', e)}")

    def get(self, path: str, params: Mapping[str, Any], login: bool = True) -> Any:
        """GET one of SRS's JSON endpoints, e.g. 'events/fetch'; exits on any error.

        Args:
            login: Log in and retry if SRS refuses access. When False, a refusal raises Refused
                instead; for the parallel requests, which mustn't all ask for the password.
        """
        url = f"{SRS_URL}{path}?{urllib.parse.urlencode(params)}"
        status, _, body = self._open(url, xhr=True)
        if status == 403 and login:
            self.login()
            status, _, body = self._open(url, xhr=True)
            if status == 403:
                die("SRS refused access after logging in")
        if status == 403:
            raise Refused(path)
        if status != 200:
            die(f"SRS answered HTTP {status} to {path}")
        try:
            data = json.loads(body)
        except ValueError:
            die(f"SRS answered {path} with something other than JSON")
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):  # {"global": [messages], "message": ..., "params": [...]}
                err = " ".join(err.get("global") or []) or err.get("message") or err
            die(f"SRS error for {path}: {err}")
        return data

    def login(self, ask: bool = True) -> bool:
        """Log in to SRS through UW CAS: by single sign-on while CAS still knows you, else with a password.

        Args:
            ask: Ask for the identifier and password if needed; when False, give up instead.

        Returns:
            Whether it worked. Never False when `ask` is True: then it exits if CAS rejects the login.
        """
        _, url, page = self._open(f"{CAS_LOGIN}?{urllib.parse.urlencode({'service': SERVICE})}")
        if not on_srs(url):  # CAS skips the form while its own session (the TGC cookie) lasts
            if not ask:
                return False
            form = LoginForm(page)
            if "execution" not in form.fields:
                die(f"expected the CAS login form at {url}")
            log(f"logging in to SRS through UW CAS ({CAS_HOST})")
            user = self.ctx.settings["cas_user"] or _ask("UW CAS identifier (usually your PESEL): ")
            try:
                password = getpass.getpass("UW CAS password: ")
            except EOFError:
                die("login aborted")
            _, url, page = self._open(urllib.parse.urljoin(url, form.action),
                                      data={**form.fields, "username": user, "password": password})
            if not on_srs(url):
                die(f"CAS login failed: {cas_message(page) or 'it did not send you back to SRS'}")
        self.save()
        return True

    def cas_cookies(self) -> list[http.cookiejar.Cookie]:
        """This session's CAS cookies, with which other sessions can log in through single sign-on."""
        return [c for c in self.jar if c.domain == CAS_HOST]

    def sso(self, cas_cookies: Sequence[http.cookiejar.Cookie]) -> bool:
        """Start a new SRS session through CAS single sign-on, with the main session's CAS cookies.

        No password is involved: while its own session lasts, CAS sends you straight back to SRS with
        a ticket. The CAS cookies aren't saved with this session, so only the main session's file has them.

        Returns:
            Whether it worked.
        """
        if not cas_cookies:
            return False
        self.jar.clear()
        for c in cas_cookies:
            self.jar.set_cookie(c)
        _, url, _ = self._open(f"{CAS_LOGIN}?{urllib.parse.urlencode({'service': SERVICE})}")
        for c in self.cas_cookies():
            self.jar.clear(c.domain, c.path, c.name)
        if not on_srs(url):
            return False
        self.save()
        return True

    def save(self) -> None:
        """Save the session cookies, readable only by you; failures are ignored (you just log in again)."""
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_file.touch(mode=0o600)
            os.chmod(self.cookie_file, 0o600)
            self.jar.save(ignore_discard=True)
        except OSError:
            pass


def load_rooms(srs: SRS, ctx: Context, refresh: bool = False) -> list[Room]:
    """The building's teaching rooms, from the cache unless it's older than ROOMS_MAX_AGE or `refresh`."""
    path = ctx.cache_dir / "srs-rooms.json"
    if not refresh:
        try:
            cached = json.loads(path.read_text())
            if time.time() - cached["time"] < ROOMS_MAX_AGE:
                return parse_rooms(cached["rooms"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    results: list[Mapping[str, Any]] = []
    for page in range(MAX_PAGES):
        data = srs.get("filters/searchRoom", {"building_id": BUILDING_ID, "search_text": "",
                                              "search_page_offset": page})
        results += data["results"]
        if not data.get("nextPages"):
            break
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"time": time.time(), "rooms": results}))
    except OSError:
        pass
    return parse_rooms(results)


def room_events(srs: SRS, room: Room, day: datetime.date, login: bool = True) -> list[Mapping[str, Any]] | None:
    """The room's SRS events on `day`; None if SRS doesn't show you that room's timetable."""
    start = int(datetime.datetime.combine(day, datetime.time()).timestamp())
    data = srs.get("events/fetch", {"start": start, "roomId": room.id, "daysToShow": 1}, login)
    params = data.get("parameters")
    if isinstance(params, dict) and (params.get("viewRoomEvents") or {}).get(str(start)) is False:
        return None
    return list(data.get("events") or [])


def fetch_round(sessions: Sequence[SRS], rooms: Sequence[Room], day: datetime.date, todo: list[int],
                events: dict[int, list[Mapping[str, Any]] | None]) -> list[SRS]:
    """Fetch the timetables of the rooms in `todo` (indexes into `rooms`) over all sessions at once.

    Each session takes the next room from `todo` and puts its events into `events`. A session that
    SRS refuses is renewed once, without the password: the main one (the first) by single sign-on in
    `SRS.login`, the others by `SRS.sso` with its CAS cookies. If that fails too, the session stops
    and its room goes back to `todo`.

    Returns:
        The sessions that stopped that way.
    """
    main = sessions[0]
    cas = main.cas_cookies()
    lock = threading.Lock()
    failed: list[SRS] = []

    def work(srs: SRS) -> None:
        """Fetch rooms with one session, until there are none left or the session is gone."""
        renewed = False
        if srs is not main and not len(srs.jar):  # nothing saved: start a new session right away
            if not srs.sso(cas):
                failed.append(srs)
                return
            renewed = True
        while True:
            with lock:
                if not todo:
                    return
                i = todo.pop()
            try:
                events[i] = room_events(srs, rooms[i], day, login=False)
            except Refused:
                with lock:
                    todo.append(i)
                if renewed or not (srs.login(ask=False) if srs is main else srs.sso(cas)):
                    failed.append(srs)
                    return
                renewed = True

    with concurrent.futures.ThreadPoolExecutor(len(sessions)) as pool:
        for future in [pool.submit(work, s) for s in sessions]:
            future.result()
    return failed


def day_events(main: SRS, rooms: Sequence[Room], day: datetime.date) -> list[list[Mapping[str, Any]] | None]:
    """`room_events` for each room, fetched over up to SESSIONS sessions at once.

    All sessions start right away with their saved cookies, renewing themselves without the password
    when they've expired (see `fetch_round`). Only if none of them can is the password asked for, after
    which the rest is fetched in parallel again.
    """
    events: dict[int, list[Mapping[str, Any]] | None] = {}
    todo = list(range(len(rooms)))
    sessions = [main, *(SRS(main.ctx, n) for n in range(1, min(SESSIONS, len(rooms))))]
    failed = fetch_round(sessions, rooms, day, todo, events)
    if todo and len(failed) == len(sessions):  # all expired, CAS single sign-on too
        i = todo.pop()
        events[i] = room_events(main, rooms[i], day)  # logs in, asking for the password
        failed = fetch_round(sessions, rooms, day, todo, events)
    for i in todo:  # left by sessions that are gone; logs in if needed
        events[i] = room_events(main, rooms[i], day)
    if failed:
        log(f"only {len(sessions) - len(failed)} of {len(sessions)} SRS sessions could be used, which is slower: "
            "CAS single sign-on has probably expired (--logout makes the next run log in afresh)")
    return [events[i] for i in range(len(rooms))]


def now() -> datetime.datetime:
    """The current local time (a function, so tests can replace it)."""
    return datetime.datetime.now()


def window(a: argparse.Namespace) -> Interval:
    """The time span to look at, from --date/--from/--to or --now; exits if it's empty."""
    if a.now:
        if a.date or a.start:
            die("--now can't be combined with --date or --from")
        start = now().replace(second=0, microsecond=0)
        day = start.date()
    else:
        day = a.date or now().date()
        start = datetime.datetime.combine(day, a.start or DAY_START)
    end = datetime.datetime.combine(day, a.end or DAY_END)
    if start >= end:
        die(f"empty time window {start:%H:%M}-{end:%H:%M}")
    return start, end


def wanted(room: Room, a: argparse.Namespace) -> bool:
    """Whether the room passes the kind, seat and floor filters."""
    return ((not a.kinds or room.kind in a.kinds) and room.seats >= a.min_seats
            and (not a.floor or room.floor in a.floor))


def minutes(d: datetime.timedelta) -> int:
    """A duration in whole minutes."""
    return int(d.total_seconds()) // 60


Shown = Sequence[Tuple[Room, Sequence[Interval]]]
"""The rooms to list, each with its free times."""


PLAIN_COLUMNS: Sequence[str] = ["room", "seats", "kind", "free", "equipment"]
"""The --plain header; columns may be added at the end later, but these stay as they are."""


def plain(shown: Shown) -> list[str]:
    """The --plain output, for other programs: tab-separated values under a header line (PLAIN_COLUMNS).

    One line per room: its name (which may contain spaces), seats (empty if unknown), kind (lab,
    lecture or classroom), free times as HH:MM-HH:MM and equipment labels, both separated by commas
    (equipment empty if none). Without any room to list there's just the header.
    """
    def field(text: str) -> str:
        """A value without tabs or line breaks, which would break the format."""
        return " ".join(text.split())

    lines = ["\t".join(PLAIN_COLUMNS)]
    for room, free in shown:
        lines.append("\t".join([field(room.name), str(room.seats or ""), room.kind,
                                ",".join(f"{s:%H:%M}-{t:%H:%M}" for s, t in free), ",".join(room.equipment)]))
    return lines


BOLD, DIM, GREEN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[0m"
KIND_COLORS: Mapping[str, str] = {"lab": "\033[36m", "lecture": "\033[35m", "classroom": ""}


@dataclass(frozen=True)
class Style:
    """The characters and colors the table is drawn with."""

    box: str
    """Corners, lines and joints, in the order ╭─┬╮│├┼┤╰┴╯."""
    free: str
    """Timeline mark for free time."""
    busy: str
    """Timeline mark for busy time."""
    dash: str
    """Between the two times of an interval."""
    sep: str
    """Between the parts of the title."""
    color: bool = False
    """Whether to use ANSI colors."""

    def paint(self, text: str, *codes: str) -> str:
        """`text` in the given ANSI colors (empty ones are skipped), if colors are on."""
        on = "".join(codes)
        return f"{on}{text}{RESET}" if self.color and on else text


UNICODE: Style = Style("╭─┬╮│├┼┤╰┴╯", "█", "░", "–", " · ")
ASCII: Style = Style("+-++|++++++", "#", ".", "-", " | ")


def style_for(stream: IO[str]) -> Style:
    """Box drawing if the stream can encode it (else ASCII), colors if it's a terminal and NO_COLOR isn't set."""
    try:
        (UNICODE.box + UNICODE.free + UNICODE.busy + UNICODE.dash + UNICODE.sep).encode(
            getattr(stream, "encoding", None) or "utf-8")
        style = UNICODE
    except (UnicodeEncodeError, LookupError):
        style = ASCII
    color = stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    return dataclasses.replace(style, color=color)


def slot_for(start: datetime.datetime, end: datetime.datetime) -> datetime.timedelta:
    """How much time one timeline character stands for: the finest of 15, 30 or 60 min that fits in ~32."""
    for mins in (15, 30):
        if (end - start) / datetime.timedelta(minutes=mins) <= 32:
            return datetime.timedelta(minutes=mins)
    return datetime.timedelta(hours=1)


def timeline(free: Sequence[Interval], start: datetime.datetime, end: datetime.datetime,
             slot: datetime.timedelta) -> tuple[datetime.datetime, list[bool]]:
    """Whether each slot from `start` (rounded down to a whole slot) to `end` is free.

    A slot counts as free when at least half of it is.

    Returns:
        The start of the first slot, and the flags.
    """
    midnight = datetime.datetime.combine(start.date(), datetime.time())
    first = midnight + (start - midnight) // slot * slot
    flags = []
    t = first
    while t < end:
        free_time = sum((min(t + slot, e) - max(t, s) for s, e in free if s < t + slot and e > t),
                        datetime.timedelta())
        flags.append(free_time * 2 >= slot)
        t += slot
    return first, flags


def ticks(first: datetime.datetime, cells: int, slot: datetime.timedelta) -> str:
    """Hour labels for a timeline of `cells` slots from `first`, each where its hour starts, not overlapping."""
    per_hour = datetime.timedelta(hours=1) // slot
    step = next(h for h in (1, 2, 3, 4, 6, 12) if h * per_hour >= 3)
    line = [" "] * cells
    hour = first.replace(minute=0) + (datetime.timedelta(hours=1) if first.minute else datetime.timedelta())
    taken = 0
    while (pos := (hour - first) // slot) < cells:
        label = str(hour.hour)
        if hour.hour % step == 0 and pos >= taken and pos + len(label) <= cells:
            line[pos:pos + len(label)] = label
            taken = pos + len(label) + 1
        hour += datetime.timedelta(hours=1)
    return "".join(line)


def wrap(items: Sequence[str], width: int, sep: str = "  ") -> list[str]:
    """Join items with `sep` into lines of at most `width` characters (longer items get a line of their own)."""
    lines: list[str] = []
    for item in items:
        if lines and len(lines[-1]) + len(sep) + len(item) <= width:
            lines[-1] += sep + item
        else:
            lines.append(item)
    return lines or [""]


def pretty(shown: Shown, start: datetime.datetime, end: datetime.datetime, min_free: datetime.timedelta,
           style: Style, width: int) -> list[str]:
    """The default output: a title, a table of the rooms with a timeline of the day, their free times
    and equipment, and a legend.

    The equipment and the free times wrap to fit in `width` columns; space to spare goes to the
    equipment first. If the timeline would leave room for fewer than two free times per line it's left
    out, and if even one doesn't fit, the equipment too.
    """
    slot = slot_for(start, end)
    first, _ = timeline([], start, end, slot)
    flags_of = [timeline(free, start, end, slot)[1] for _, free in shown]
    cells = len(flags_of[0])
    times = [[f"{s:%H:%M}{style.dash}{t:%H:%M}" for s, t in free] for _, free in shown]
    one = len(times[0][0])  # every free time has the same length

    def total(widths: Sequence[int]) -> int:
        """How wide a table with columns this wide is, with their borders and spaces."""
        return sum(widths) + 3 * len(widths) + 1

    fixed = [max(4, *(len(r.name) for r, _ in shown)), len("Seats"), len("classroom")]
    gear_full = max(len("Equipment"), *(len(" ".join(r.equipment)) for r, _ in shown))
    gear_least = min(gear_full, max(len("Equipment"), GEAR_WIDTH))
    free_least = min(max(len("  ".join(t)) for t in times), 2 * one + 2)
    with_timeline = total([*fixed, gear_least, cells, free_least]) <= width
    with_gear = with_timeline or total([*fixed, gear_least, one]) <= width
    spare = width - total([*fixed, gear_least, *[cells] * with_timeline, free_least if with_timeline else one])
    gear_lines = [wrap(r.equipment, min(gear_full, gear_least + max(0, spare)), " ") for r, _ in shown]
    side = [cells] * with_timeline
    gear = [max(len("Equipment"), *(len(x) for g in gear_lines for x in g))] * with_gear
    free_lines = [wrap(t, max(one, width - total([*fixed, *side, 0, *gear]))) for t in times]
    widths = [*fixed, *side, max(len("Free"), *(len(x) for w in free_lines for x in w)), *gear]

    def rule(left: str, joint: str, right: str) -> str:
        """A horizontal line across the table."""
        return style.paint(left + joint.join(style.box[1] * (w + 2) for w in widths) + right, DIM)

    Line = Tuple[str, str]  # a line of a cell: as text, and as shown (maybe with colors)

    def rows(columns: Sequence[Sequence[Line]]) -> list[str]:
        """The table lines for one row: as many as its tallest cell has."""
        bar = style.paint(style.box[4], DIM)
        out = []
        for k in range(max(len(c) for c in columns)):
            cells = [c[k] if k < len(c) else ("", "") for c in columns]
            padded = [shown + " " * (w - len(text)) for (text, shown), w in zip(cells, widths)]
            out.append(f"{bar} " + f" {bar} ".join(padded) + f" {bar}")
        return out

    def painted(text: str, *codes: str) -> Line:
        """A cell line in the given colors."""
        return text, style.paint(text, *codes)

    def timeline_bar(flags: Sequence[bool]) -> Line:
        """The timeline of one room, with free and busy runs colored."""
        text, shown, i = "", "", 0
        while i < len(flags):
            j = i
            while j < len(flags) and flags[j] == flags[i]:
                j += 1
            run = (style.free if flags[i] else style.busy) * (j - i)
            text, shown = text + run, shown + style.paint(run, GREEN if flags[i] else DIM)
            i = j
        return text, shown

    title = style.sep.join([f"{start:%a %d %b %Y}", f"{start:%H:%M}{style.dash}{end:%H:%M}",
                            f"free for {minutes(min_free)} min or more"])
    header = ["Room", "Seats", "Kind", *[ticks(first, cells, slot)] * with_timeline, "Free",
              *["Equipment"] * with_gear]
    out = [style.paint(title, BOLD), rule(style.box[0], style.box[2], style.box[3]),
           *rows([[painted(h, BOLD)] for h in header]), rule(style.box[5], style.box[6], style.box[7])]
    for (room, _), flags, gear_text, free in zip(shown, flags_of, gear_lines, free_lines):
        has = [painted(x) for x in gear_text] if room.equipment else [painted(style.dash, DIM)]
        out += rows([[painted(room.name, BOLD)], [painted(f"{room.seats or '?':>5}")],
                     [painted(room.kind, KIND_COLORS[room.kind])], *[[timeline_bar(flags)]] * with_timeline,
                     [painted(x) for x in free], *[has] * with_gear])
    out.append(rule(style.box[8], style.box[9], style.box[10]))
    legend = style.paint(f"{len(shown)} room{'s' if len(shown) != 1 else ''}", DIM)
    if with_timeline:
        legend += (style.paint(style.sep, DIM) + f"{style.paint(style.free, GREEN)} free  "
                   f"{style.paint(style.busy, DIM)} busy" + style.paint(f", {minutes(slot)} min per mark", DIM))
    return out + [legend]


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Register the rooms options on the subparser."""
    g = ap.add_argument_group("when")
    g.add_argument("-d", "--date", type=parse_date, help="YYYY-MM-DD, today (default), tomorrow, "
                   "or a weekday (mon...sun: today or the next one)")
    g.add_argument("--from", dest="start", type=parse_clock, metavar="HH:MM",
                   help=f"start of the time window (default {DAY_START:%H:%M})")
    g.add_argument("--to", dest="end", type=parse_clock, metavar="HH:MM",
                   help=f"end of the time window (default {DAY_END:%H:%M})")
    g.add_argument("-n", "--now", action="store_true", help="only rooms that are free right now; the window "
                   "starts now")
    g.add_argument("-m", "--min-free", type=parse_minutes, default="30m", metavar="DURATION",
                   help="shortest free time worth listing, e.g. 45m, 2h, 1h30 (default 30m)")

    g = ap.add_argument_group("which rooms")
    g.add_argument("--lab", dest="kinds", action="append_const", const="lab",
                   help="labs: rooms with computer workstations")
    g.add_argument("--lecture", dest="kinds", action="append_const", const="lecture",
                   help=f"lecture rooms: {LECTURE_SEATS} or more seats")
    g.add_argument("--classroom", dest="kinds", action="append_const", const="classroom",
                   help="the other rooms (kinds can be combined)")
    g.add_argument("-s", "--min-seats", type=int, default=0, metavar="N")
    g.add_argument("--floor", type=int, action="append", metavar="N",
                   help="floor, the first digit of the room number; repeatable")

    g = ap.add_argument_group("output")
    g.add_argument("--plain", action="store_true", help="tab-separated values for other programs instead of "
                   "the table: a header (room, seats, kind, free, equipment), then one line per room, with "
                   "the free times (HH:MM-HH:MM) and equipment separated by commas")

    g = ap.add_argument_group("session")
    g.add_argument("--refresh-rooms", action="store_true", help="fetch the room list again instead of using the "
                   "cached one (it's refetched weekly anyway)")
    g.add_argument("--logout", action="store_true", help="delete the saved SRS session")


def main(a: argparse.Namespace, ctx: Context) -> int | None:
    """List the rooms that match the filters, with their free times in the window."""
    if a.logout:
        for path in ctx.cache_dir.glob(cookie_file(ctx).name + "*"):
            path.unlink()
        log("deleted the saved SRS sessions")
        return None
    start, end = window(a)
    srs = SRS(ctx)
    rooms = [r for r in load_rooms(srs, ctx, a.refresh_rooms) if wanted(r, a)]
    if not rooms:
        die("no room in the building matches the filters")

    shown: list[tuple[Room, list[Interval]]] = []
    hidden = []
    for room, events in zip(rooms, day_events(srs, rooms, start.date())):
        if events is None:
            hidden.append(room.name)
            continue
        free = [f for f in free_times(events, start, end) if f[1] - f[0] >= a.min_free]
        if free and (not a.now or free[0][0] == start):
            shown.append((room, free))
    if hidden:
        log(f"SRS doesn't show you the timetable of {', '.join(hidden)}; left out")
    if not shown:
        log(f"no room is free{' now' if a.now else ''} for {minutes(a.min_free)} min or more "
            f"between {start:%H:%M} and {end:%H:%M}")
    if a.plain:
        print("\n".join(plain(shown)))  # the header even without rooms, so the format never changes
    elif shown:
        print("\n".join(pretty(shown, start, end, a.min_free, style_for(sys.stdout),
                               shutil.get_terminal_size((100, 24)).columns)))
    return None
