"""Tests for the `rooms` tool (mimtoolbox.tools.rooms). SRS and CAS are never contacted.

The canned responses are trimmed copies of real SRS answers.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import http.cookiejar
import io
import json
import os
import re
import stat
import threading
import urllib.request
from typing import Any, Mapping, Sequence

from mimtoolbox.tools import rooms
from tests.support import IsolatedTestCase, captured_stdout, make_ctx, parse


def attr(pl: str, en: str, count: int | None = None) -> dict[str, Any]:
    """A room attribute as filters/searchRoom lists it."""
    return {"descriptionPl": pl, "descriptionEn": en, "count": count}


PCS = attr("Stanowiska komputerowe podłączone do sieci", "PC workstations connected to the network", 15)
WIFI = attr("Sieć radiowa (Internet)", "Wifi")
PROJECTOR = attr("Projektor multimedialny, zamontowany na stałe", "Projector, installed permanently")
SCREEN = attr("Ekran do rzutnika, zawieszony na stałe", "Projector screen, installed permanently")
MULTIMEDIA = attr("Pełny zestaw multimedialny", "A complete set of multimedia")
ROOM_PAGES: Sequence[Mapping[str, Any]] = [
    {"results": [{"id": "54", "name": "2041", "buildingId": "3320", "capacity": "15", "attrs": [WIFI, PCS]},
                 {"id": "409", "name": "2180 (sala RW)", "buildingId": "3320", "capacity": "100", "attrs": []}],
     "currentPage": 0, "nextPages": 1},
    {"results": [{"id": "15", "name": "3180", "buildingId": "3320", "capacity": "156",
                  "attrs": [SCREEN, MULTIMEDIA, PROJECTOR, WIFI]},
                 {"id": "1624", "name": "4060", "buildingId": "3320", "capacity": "30", "attrs": [WIFI]}],
     "currentPage": 1, "nextPages": 0},
]
DAY = datetime.date(2026, 10, 23)
DAY_TS = str(int(datetime.datetime(2026, 10, 23).timestamp()))


def event(start: str, end: str, title: str = "Bazy danych", kind: str = "lecture") -> dict[str, Any]:
    """An SRS event on DAY, as events/fetch sends it (times as HH:MM)."""
    return {"ID": "29859243", "STATUS": "O", "TITLE": title, "TZAJ_KOD": "LAB", "TYPE": kind, "roomId": "54",
            "start": f"2026-10-23T{start}:00+02:00", "end": f"2026-10-23T{end}:00+02:00"}


EVENTS: Mapping[str, list[dict[str, Any]]] = {
    "54": [event("08:30", "10:00"), event("10:15", "12:00"), event("12:15", "14:00"), event("16:00", "17:30"),
           event("18:00", "19:30")],
    "409": [],
    "15": [event("10:00", "12:00", "Analiza"), event("16:00", "17:30", "Spotkanie koła", "custom"),
           event("16:00", "17:30", "Spotkanie koła", "custom")],
    "1624": [event("08:00", "21:00", "Konferencja", "custom")],
}


def at(hhmm: str) -> datetime.datetime:
    """A time on DAY."""
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 10, 23, h, m)


def interval(text: str) -> rooms.Interval:
    """An interval on DAY from 'HH:MM-HH:MM'."""
    s, t = text.split("-")
    return at(s), at(t)


class FakeSRS:
    """Stand-in for `SRS.get`, `SRS.login` and `SRS.sso`: serves the canned responses, records the requests."""

    def __init__(self, hidden: Sequence[str] = ()) -> None:
        """
        Args:
            hidden: Room ids whose timetable SRS doesn't show.
        """
        self.hidden = hidden
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.fetched: list[tuple[int, str]] = []
        """(session number, room id) of each timetable served."""
        self.expired: set[int] = set()
        """Sessions that SRS refuses until they're renewed."""
        self.tgc_valid = True
        """Whether CAS single sign-on works."""
        self.renewed: list[int] = []
        """Sessions that tried to renew themselves without the password."""
        self.passwords = 0
        """How often the password was asked for."""
        self.wait_for: int | None = None
        """A session whose first request the main session waits for (so that it surely gets a room)."""
        self._started = threading.Event()

    def get(self, srs: rooms.SRS, path: str, params: Mapping[str, Any], login: bool = True) -> Any:
        """Answer like SRS would."""
        self.calls.append((path, dict(params), login))
        if path == "filters/searchRoom":
            return ROOM_PAGES[params["search_page_offset"]]
        if srs.n == self.wait_for:
            self._started.set()
        elif srs.n == 0 and not login and self.wait_for is not None:
            self._started.wait(5)
        if srs.n in self.expired:
            if not login:
                raise rooms.Refused(path)
            self.login(srs)
        assert path == "events/fetch" and str(params["start"]) == DAY_TS and params["daysToShow"] == 1
        room = params["roomId"]
        self.fetched.append((srs.n, room))
        return {"events": EVENTS[room],
                "parameters": {"viewRoomEvents": {DAY_TS: room not in self.hidden}, "roomId": room}}

    def login(self, srs: rooms.SRS, ask: bool = True) -> bool:
        """Log in by single sign-on if it works, else with the password if `ask`."""
        if not ask:
            self.renewed.append(srs.n)
        if not self.tgc_valid:
            if not ask:
                return False
            self.passwords += 1
            self.tgc_valid = True
        self.expired.discard(srs.n)
        return True

    def sso(self, srs: rooms.SRS, cas: Sequence[http.cookiejar.Cookie]) -> bool:
        """Renew a session, if single sign-on works."""
        self.renewed.append(srs.n)
        if self.tgc_valid:
            self.expired.discard(srs.n)
        return self.tgc_valid


class ParsingTest(IsolatedTestCase):
    """Rooms, times and the command-line values."""

    def test_rooms_and_kinds(self) -> None:
        """Rooms with PC workstations are labs, 100+ seats are lecture rooms, the rest classrooms."""
        found = rooms.parse_rooms([*ROOM_PAGES[1]["results"], *ROOM_PAGES[0]["results"]])
        self.assertEqual([(r.name, r.seats, r.kind, r.floor) for r in found],
                         [("2041", 15, "lab", 2), ("2180 (sala RW)", 100, "lecture", 2), ("3180", 156, "lecture", 3),
                          ("4060", 30, "classroom", 4)])

    def test_equipment(self) -> None:
        """Short labels in a fixed order; the PCs (that's what --lab is) and wifi (everywhere) left out;
        unknown ones by their English description."""
        self.assertEqual(rooms.parse_rooms(ROOM_PAGES[1]["results"])[0].equipment,
                         ("projector", "screen", "multimedia"))
        self.assertEqual(rooms.parse_rooms(ROOM_PAGES[0]["results"])[0].equipment, ())
        self.assertEqual(rooms.equipment([attr("Klimatyzacja", "Air conditioning"), WIFI, PCS,
                                          attr("Gniazdka sieciowe (Internet)", "Network sockets (Internet)"),
                                          attr("Tablica interaktywna z bardzo długim opisem",
                                               "Interactive whiteboard with a long description")]),
                         ("ethernet", "air conditioning", "interactive white"))

    def test_room_without_capacity_or_number(self) -> None:
        """A missing capacity is 0 seats, a name without a leading digit has no floor."""
        (room,) = rooms.parse_rooms([{"id": "1", "name": "Aula", "capacity": None, "attrs": []}])
        self.assertEqual((room.seats, room.floor, room.kind), (0, None, "classroom"))

    def test_srs_times_are_warsaw_wall_clock(self) -> None:
        """The UTC offset is dropped, whatever the offset (summer or winter time)."""
        self.assertEqual(rooms.wall_clock("2026-10-23T08:30:00+02:00"), at("08:30"))
        self.assertEqual(rooms.wall_clock("2026-12-01T08:30:00+01:00"), datetime.datetime(2026, 12, 1, 8, 30))

    def test_dates(self) -> None:
        """--date takes ISO dates, today/tomorrow and weekday names (today or the next one)."""
        friday = datetime.date(2026, 10, 23)
        for text, expected in [("2026-11-02", datetime.date(2026, 11, 2)), ("today", friday),
                               ("Tomorrow", datetime.date(2026, 10, 24)), ("fri", friday),
                               ("monday", datetime.date(2026, 10, 26)), ("thu", datetime.date(2026, 10, 29))]:
            with self.subTest(text=text):
                self.assertEqual(rooms.parse_date(text, today=friday), expected)
        for bad in ["fr", "someday", "2026-13-01"]:
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                rooms.parse_date(bad, today=friday)

    def test_clock_times(self) -> None:
        """--from/--to take H, HH and HH:MM."""
        self.assertEqual(rooms.parse_clock("8"), datetime.time(8))
        self.assertEqual(rooms.parse_clock("12:15"), datetime.time(12, 15))
        for bad in ["24", "12:60", "noon", "12:5"]:
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                rooms.parse_clock(bad)

    def test_durations(self) -> None:
        """--min-free takes minutes, hours or both."""
        for text, mins in [("45", 45), ("45m", 45), ("2h", 120), ("1h30", 90), ("1h30m", 90), ("0", 0)]:
            with self.subTest(text=text):
                self.assertEqual(rooms.parse_minutes(text), datetime.timedelta(minutes=mins))
        for bad in ["", "h", "1.5h", "90s"]:
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                rooms.parse_minutes(bad)


class FreeTimesTest(IsolatedTestCase):
    """Turning a room's events into its free times."""

    def free(self, events: Sequence[Mapping[str, Any]], window: str = "08:00-21:00") -> list[str]:
        """free_times for a window on DAY, as 'HH:MM-HH:MM' strings."""
        return [f"{s:%H:%M}-{t:%H:%M}" for s, t in rooms.free_times(events, *interval(window))]

    def test_gaps_between_events(self) -> None:
        """The free times are the gaps before, between and after the events."""
        self.assertEqual(self.free(EVENTS["54"]),
                         ["08:00-08:30", "10:00-10:15", "12:00-12:15", "14:00-16:00", "17:30-18:00", "19:30-21:00"])

    def test_no_events(self) -> None:
        """A room without events is free for the whole window."""
        self.assertEqual(self.free([]), ["08:00-21:00"])

    def test_overlapping_and_duplicate_events(self) -> None:
        """Overlapping or repeated events (SRS sends some twice) merge into one busy time."""
        self.assertEqual(self.free([event("10:00", "12:00"), event("11:00", "13:00"), event("11:00", "13:00")]),
                         ["08:00-10:00", "13:00-21:00"])

    def test_events_clipped_to_window(self) -> None:
        """Events reaching outside the window only count inside it."""
        self.assertEqual(self.free([event("07:00", "09:00"), event("11:00", "13:00")], "10:00-12:00"),
                         ["10:00-11:00"])
        self.assertEqual(self.free([event("07:00", "22:00")]), [])

    def test_unreadable_event_exits(self) -> None:
        """An event without times stops the tool instead of showing the room as free."""
        with self.assertExits(1):
            rooms.free_times([{"TITLE": "x"}], *interval("08:00-21:00"))
        self.assertIn("unexpected event from SRS", self.stderr.getvalue())


CAS_FORM = """<form method="post" id="fm1" action="login">
  <input class="mdc-text-field__input" id="username" type="text" required name="username" value="" />
  <input type="password" id="password" required name="password" value="" />
  <input type="checkbox" class="mdc-checkbox__native-control" name="loginVis" id="loginVis" />
  <input type="hidden" name="execution" value="c4b2affe_ZXlK"/><input type="hidden" name="_eventId" value="submit"/>
  <input type="hidden" name="geolocation"/>
  <button class="mdc-button" name="submit" type="submit">ZALOGUJ</button>
</form>
<input type="hidden" name="outside" value="x"/>"""
CAS_ERROR = """<form method="post" id="fm1" action="login">
  <div class="banner banner-danger my-2">
    <p>Dostarczone dane uwierzytelniania nie mogą zostać uznane za poprawne.</p>
  </div>
  <input type="hidden" name="execution" value="e2"/>
</form>"""
CAS_URL = "https://login.uw.edu.pl/cas/login?service=https%3A%2F%2Fsrs.uw.edu.pl%2Flogin%2FloginCas"
SERVICE_URL = "https://srs.uw.edu.pl/login/loginCas"


class LoginPageTest(IsolatedTestCase):
    """Reading CAS pages."""

    def test_login_form(self) -> None:
        """Only the hidden fields of the login form are kept; they're sent back with the login."""
        form = rooms.LoginForm(CAS_FORM)
        self.assertEqual(form.action, "login")
        self.assertEqual(form.fields, {"execution": "c4b2affe_ZXlK", "_eventId": "submit", "geolocation": ""})

    def test_error_message(self) -> None:
        """The red banner's text is the reason a login failed."""
        self.assertEqual(rooms.cas_message(CAS_ERROR),
                         "Dostarczone dane uwierzytelniania nie mogą zostać uznane za poprawne.")
        self.assertEqual(rooms.cas_message(CAS_FORM), "")


class SessionTest(IsolatedTestCase):
    """The SRS client: JSON requests, logging in through CAS and keeping the session."""

    def setUp(self) -> None:
        """Replace the network with a script of responses."""
        super().setUp()
        self.responses: list[tuple[int, str, str]] = []
        self.requests: list[tuple[str, Mapping[str, str] | None]] = []
        self.patch("mimtoolbox.tools.rooms.SRS._open", self._open)
        self.passwords = self.patch("getpass.getpass", return_value="s3cret")

    def _open(self, url: str, data: Mapping[str, str] | None = None, xhr: bool = False) -> tuple[int, str, str]:
        """Record the request and return the next scripted response."""
        self.requests.append((url, data))
        return self.responses.pop(0)

    def srs(self, **settings: str) -> rooms.SRS:
        """A client for a context with the given settings."""
        settings.setdefault("cas_user", "12345678901")
        return rooms.SRS(make_ctx("rooms", **settings))

    def test_json_request(self) -> None:
        """A request with a valid session just returns the decoded JSON."""
        self.responses = [(200, "https://srs.uw.edu.pl/events/fetch?x", '{"events": []}')]
        self.assertEqual(self.srs().get("events/fetch", {"roomId": "54", "daysToShow": 1}), {"events": []})
        self.assertEqual(self.requests, [("https://srs.uw.edu.pl/events/fetch?roomId=54&daysToShow=1", None)])
        self.passwords.assert_not_called()

    def test_logs_in_when_refused(self) -> None:
        """A 403 means no session: log in with the CAS form, save the cookies privately, retry."""
        self.responses = [(403, "https://srs.uw.edu.pl/filters/searchRoom?x", "<html>Brak dostępu</html>"),
                          (200, CAS_URL, CAS_FORM),
                          (200, "https://srs.uw.edu.pl/", "<html>var isLoggedIn = true;</html>"),
                          (200, "https://srs.uw.edu.pl/filters/searchRoom?x", '{"results": []}')]
        srs = self.srs()
        self.assertEqual(srs.get("filters/searchRoom", {"building_id": "3320"}), {"results": []})
        (cas, _), (post, fields), (retry, _) = self.requests[1:]
        self.assertEqual(cas, rooms.CAS_LOGIN + "?service=https%3A%2F%2Fsrs.uw.edu.pl%2Flogin%2FloginCas")
        self.assertEqual(post, "https://login.uw.edu.pl/cas/login")
        self.assertEqual(fields, {"execution": "c4b2affe_ZXlK", "_eventId": "submit", "geolocation": "",
                                  "username": "12345678901", "password": "s3cret"})
        self.assertEqual(retry, self.requests[0][0])
        mode = os.stat(srs.cookie_file).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_asks_for_identifier_if_not_configured(self) -> None:
        """Without cas_user in the config, the identifier is asked for along with the password."""
        self.patch("builtins.input", return_value="98765432109")
        self.responses = [(200, CAS_URL, CAS_FORM), (200, "https://srs.uw.edu.pl/", "")]
        self.srs(cas_user="").login()
        fields = self.requests[1][1]
        self.assertEqual(fields and fields["username"], "98765432109")

    def test_login_without_asking(self) -> None:
        """login(ask=False) uses single sign-on if it can, and gives up instead of asking otherwise."""
        self.responses = [(200, CAS_URL, CAS_FORM), (302, "https://srs.uw.edu.pl/login/loginCas?ticket=ST-1", "")]
        srs = self.srs()
        self.assertFalse(srs.login(ask=False))
        self.assertTrue(srs.login(ask=False))
        self.passwords.assert_not_called()

    def test_front_page_not_loaded(self) -> None:
        """Redirects are followed, except to SRS's big front page, where logging in ends."""
        handler = rooms.StopAtFrontPage()
        req = urllib.request.Request("https://srs.uw.edu.pl/login/loginCas?ticket=ST-1")
        for url in ["https://srs.uw.edu.pl/", "https://srs.uw.edu.pl/#/welcome"]:
            self.assertIsNone(handler.redirect_request(req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), url))
        follow = handler.redirect_request(req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), SERVICE_URL)
        self.assertEqual(follow and follow.full_url, SERVICE_URL)

    def test_cas_session_skips_password(self) -> None:
        """While CAS still knows you, it sends you straight back to SRS: no password needed."""
        self.responses = [(200, "https://srs.uw.edu.pl/", "<html>var isLoggedIn = true;</html>")]
        self.srs().login()
        self.passwords.assert_not_called()

    def test_failed_login(self) -> None:
        """When CAS rejects the login, its message is shown."""
        self.responses = [(200, CAS_URL, CAS_FORM), (401, "https://login.uw.edu.pl/cas/login", CAS_ERROR)]
        with self.assertExits(1):
            self.srs().login()
        self.assertIn("CAS login failed: Dostarczone dane uwierzytelniania", self.stderr.getvalue())

    def test_unexpected_cas_page(self) -> None:
        """A CAS page without the login form stops before asking for anything."""
        self.responses = [(200, CAS_URL, "<html>maintenance</html>")]
        with self.assertExits(1):
            self.srs().login()
        self.passwords.assert_not_called()

    def test_still_refused_after_login(self) -> None:
        """If SRS refuses even after a successful login, give up instead of looping."""
        self.responses = [(403, "u", ""), (200, "https://srs.uw.edu.pl/", ""), (403, "u", "")]
        with self.assertExits(1):
            self.srs().get("events/fetch", {})
        self.assertIn("SRS refused access after logging in", self.stderr.getvalue())

    def test_no_login_in_parallel_requests(self) -> None:
        """With login=False a refusal raises Refused rather than asking for the password."""
        self.responses = [(403, "u", "")]
        with self.assertRaises(rooms.Refused):
            self.srs().get("events/fetch", {}, login=False)
        self.passwords.assert_not_called()

    def test_single_sign_on(self) -> None:
        """An extra session starts with the main one's CAS cookies, but doesn't save them."""
        main = self.srs()
        main.jar.set_cookie(make_cookie("TGC", "tgt", "login.uw.edu.pl", "/cas/"))
        main.jar.set_cookie(make_cookie("PHPSESSID", "main"))
        extra = rooms.SRS(make_ctx("rooms", cas_user=""), 2)
        sent: list[list[str]] = []

        def cas(srs: rooms.SRS, url: str, data: Any = None, xhr: bool = False) -> tuple[int, str, str]:
            """Note the cookies the request would carry; CAS sends you back to SRS."""
            sent.append([c.name for c in srs.jar])
            return 200, "https://srs.uw.edu.pl/", ""

        self.patch("mimtoolbox.tools.rooms.SRS._open", cas)
        self.assertTrue(extra.sso(main.cas_cookies()))
        self.assertEqual(sent, [["TGC"]])
        self.assertEqual(extra.cookie_file.name, "srs-cookies.2")
        self.assertNotIn("TGC", extra.cookie_file.read_text())
        self.passwords.assert_not_called()

    def test_single_sign_on_expired(self) -> None:
        """When CAS shows its login form instead, single sign-on fails and nothing is saved."""
        main = self.srs()
        main.jar.set_cookie(make_cookie("TGC", "tgt", "login.uw.edu.pl", "/cas/"))
        extra = rooms.SRS(make_ctx("rooms", cas_user=""), 1)
        self.responses = [(200, CAS_URL, CAS_FORM)]
        self.assertFalse(extra.sso(main.cas_cookies()))
        self.assertFalse(extra.cookie_file.exists())
        self.assertFalse(extra.sso([]))  # no CAS cookies: not even tried
        self.assertEqual(len(self.requests), 1)

    def test_srs_errors(self) -> None:
        """SRS's own error replies, other HTTP errors and non-JSON answers stop the tool with a message."""
        error = {"error": {"params": [], "global": ["Wystąpił nieoczekiwany błąd."], "message": None}}
        for response, message in [((200, "u", json.dumps(error)), "SRS error for events/fetch: Wystąpił"),
                                  ((500, "u", ""), "SRS answered HTTP 500"),
                                  ((200, "u", "<html>"), "something other than JSON")]:
            with self.subTest(message=message), self.assertExits(1):
                self.responses = [response]
                self.srs().get("events/fetch", {})
            self.assertIn(message, self.stderr.getvalue())

    def test_saved_session_is_loaded(self) -> None:
        """Cookies saved by one run are sent by the next."""
        self.responses = [(200, "https://srs.uw.edu.pl/", "")]
        srs = self.srs()
        srs.jar.set_cookie(make_cookie("PHPSESSID", "abc"))
        srs.login()
        self.assertEqual([c.value for c in self.srs().jar], ["abc"])


def make_cookie(name: str, value: str, domain: str = "srs.uw.edu.pl", path: str = "/") -> http.cookiejar.Cookie:
    """A session cookie, by default for srs.uw.edu.pl."""
    return http.cookiejar.Cookie(0, name, value, None, False, domain, False, False, path, True, True, None, True, None,
                                 None, {})


class ToolTestCase(IsolatedTestCase):
    """Runs the whole tool, with SRS replaced by canned responses."""

    def setUp(self) -> None:
        """Serve canned SRS answers; it's 14:05 on DAY."""
        super().setUp()
        self.srs = FakeSRS()
        self.patch("mimtoolbox.tools.rooms.SRS.get", lambda srs, *a, **kw: self.srs.get(srs, *a, **kw))
        self.patch("mimtoolbox.tools.rooms.SRS.login", lambda srs, ask=True: self.srs.login(srs, ask))
        self.patch("mimtoolbox.tools.rooms.SRS.sso", lambda srs, cas: self.srs.sso(srs, cas))
        self.patch("mimtoolbox.tools.rooms.now", return_value=at("14:05"))

    def run_main(self, argv: Sequence[str], plain: bool = True) -> list[str]:
        """Run `rooms` with argv (and --plain, unless `plain` is False); return the printed lines."""
        with captured_stdout() as out:
            rooms.main(parse(["rooms", *(["--plain"] if plain else []), *argv]), make_ctx("rooms", cas_user=""))
        return out.getvalue().splitlines()


class MainTest(ToolTestCase):
    """Filters, time windows and the --plain output."""

    def test_default_day(self) -> None:
        """Today 08:00-21:00: every room with 30 free minutes or more, and those free times."""
        self.assertEqual(self.run_main([]), [
            "Fri 2026-10-23, 08:00-21:00, free for 30 min or more",
            "room            seats  kind       free",
            "2041               15  lab        08:00-08:30, 14:00-16:00, 17:30-18:00, 19:30-21:00",
            "2180 (sala RW)    100  lecture    08:00-21:00",
            "3180              156  lecture    08:00-10:00, 12:00-16:00, 17:30-21:00",
        ])

    def test_kind_filters(self) -> None:
        """--lab, --lecture and --classroom pick kinds, and can be combined; only those rooms are fetched."""
        self.assertEqual([line.split()[0] for line in self.run_main(["--lab"])[2:]], ["2041"])
        self.assertEqual([line.split()[0] for line in self.run_main(["--lab", "--lecture"])[2:]],
                         ["2041", "2180", "3180"])
        fetched = [c[1]["roomId"] for c in self.srs.calls if c[0] == "events/fetch"]
        self.assertEqual(fetched[0], "54")
        self.assertNotIn("1624", fetched)

    def test_seat_and_floor_filters(self) -> None:
        """--min-seats and --floor narrow the rooms down."""
        self.assertEqual([line.split()[0] for line in self.run_main(["--min-seats", "120"])[2:]], ["3180"])
        self.assertEqual([line.split()[0] for line in self.run_main(["--floor", "2"])[2:]], ["2041", "2180"])

    def test_window_and_min_free(self) -> None:
        """--from/--to set the window, --min-free the shortest free time listed."""
        lines = self.run_main(["-d", "2026-10-23", "--from", "9", "--to", "18:30", "--min-free", "1h30"])
        self.assertEqual(lines[0], "Fri 2026-10-23, 09:00-18:30, free for 90 min or more")
        self.assertEqual(lines[2:], ["2041               15  lab        14:00-16:00",
                                     "2180 (sala RW)    100  lecture    09:00-18:30",
                                     "3180              156  lecture    12:00-16:00"])

    def test_now(self) -> None:
        """--now lists only rooms that are free right now, from now on."""
        self.assertEqual(self.run_main(["--now"])[1:], [
            "room            seats  kind       free",
            "2041               15  lab        14:05-16:00, 17:30-18:00, 19:30-21:00",
            "2180 (sala RW)    100  lecture    14:05-21:00",
            "3180              156  lecture    14:05-16:00, 17:30-21:00",
        ])

    def test_now_excludes_date_and_from(self) -> None:
        """--now already fixes the day and start."""
        for extra in (["-d", "tomorrow"], ["--from", "10"]):
            with self.subTest(extra=extra), self.assertExits(1):
                self.run_main(["--now", *extra])

    def test_empty_window(self) -> None:
        """A window that ends before it starts is refused before asking SRS."""
        with self.assertExits(1):
            self.run_main(["--from", "18", "--to", "10"])
        self.assertIn("empty time window 18:00-10:00", self.stderr.getvalue())
        self.assertEqual(self.srs.calls, [])

    def test_nothing_free(self) -> None:
        """When no room fits, say so instead of printing an empty table."""
        self.assertEqual(self.run_main(["--classroom"]), [])
        self.assertIn("no room is free for 30 min or more between 08:00 and 21:00", self.stderr.getvalue())

    def test_no_room_matches(self) -> None:
        """Filters that leave no room at all are an error."""
        with self.assertExits(1):
            self.run_main(["--lab", "--min-seats", "100"])

    def test_hidden_timetable(self) -> None:
        """Rooms whose timetable SRS hides are left out, with a note."""
        self.srs.hidden = ["409"]
        self.assertNotIn("2180", "\n".join(self.run_main([])))
        self.assertIn("SRS doesn't show you the timetable of 2180 (sala RW)", self.stderr.getvalue())

    def test_room_list_is_cached(self) -> None:
        """The room list is fetched once (both pages), then read from the cache until --refresh-rooms."""
        self.run_main([])
        self.run_main([])
        searches = [c[1]["search_page_offset"] for c in self.srs.calls if c[0] == "filters/searchRoom"]
        self.assertEqual(searches, [0, 1])
        self.run_main(["--refresh-rooms"])
        self.assertEqual(len([c for c in self.srs.calls if c[0] == "filters/searchRoom"]), 4)


class SessionPoolTest(ToolTestCase):
    """Fetching the timetables over several SRS sessions at once."""

    def test_parallel_requests_never_log_in(self) -> None:
        """All timetables are fetched in parallel, none of them allowed to ask for the password."""
        self.run_main([])
        logins = [login for path, _, login in self.srs.calls if path == "events/fetch"]
        self.assertEqual(logins, [False] * 4)

    def test_sessions_share_the_work(self) -> None:
        """Each timetable is fetched once, over the main session and new ones from single sign-on."""
        self.run_main([])
        self.assertEqual(sorted(room for _, room in self.srs.fetched), ["15", "1624", "409", "54"])
        self.assertEqual(sorted(self.srs.renewed), [1, 2, 3])  # 4 rooms: main + 3 more
        self.assertLessEqual({n for n, _ in self.srs.fetched}, {0, 1, 2, 3})
        self.assertNotIn("sessions could be used", self.stderr.getvalue())

    def saved_session(self, n: int) -> None:
        """Save a (stand-in) cookie file for extra session n."""
        srs = rooms.SRS(make_ctx("rooms"), n)
        srs.jar.set_cookie(make_cookie("PHPSESSID", f"s{n}"))
        srs.save()

    def test_saved_session_renewed_when_expired(self) -> None:
        """A saved session that SRS refuses is renewed through single sign-on, and its room retried."""
        self.saved_session(1)
        self.srs.expired, self.srs.wait_for = {1}, 1
        self.run_main([])
        self.assertIn(1, self.srs.renewed)
        self.assertIn(1, {n for n, _ in self.srs.fetched})
        self.assertEqual(len(self.srs.fetched), 4)

    def test_main_session_renewed_by_single_sign_on(self) -> None:
        """An expired main session logs in again through single sign-on, without the password."""
        self.srs.expired = {0}
        self.run_main([])
        self.assertIn(0, self.srs.renewed)
        self.assertEqual(self.srs.passwords, 0)
        self.assertEqual(len(self.srs.fetched), 4)

    def test_everything_expired_asks_for_the_password_once(self) -> None:
        """When no session can renew itself, the password is asked for once, then all run in parallel again."""
        self.srs.expired, self.srs.tgc_valid = {0}, False
        self.run_main([])
        self.assertEqual(self.srs.passwords, 1)
        self.assertEqual(sorted(room for _, room in self.srs.fetched), ["15", "1624", "409", "54"])
        self.assertNotIn("sessions could be used", self.stderr.getvalue())

    def test_rooms_handed_back_when_renewal_fails(self) -> None:
        """Without single sign-on, the other sessions fetch the rooms, and the slowdown is explained."""
        self.saved_session(1)
        self.srs.expired, self.srs.wait_for, self.srs.tgc_valid = {1}, 1, False
        self.run_main([])
        self.assertEqual(sorted(room for _, room in self.srs.fetched), ["15", "1624", "409", "54"])
        self.assertEqual({n for n, _ in self.srs.fetched}, {0})
        self.assertEqual(self.srs.passwords, 0)
        self.assertIn("only 1 of 4 SRS sessions could be used", self.stderr.getvalue())

    def test_one_room_one_session(self) -> None:
        """A single room needs no extra sessions."""
        self.run_main(["--min-seats", "150"])
        self.assertEqual(self.srs.fetched, [(0, "15")])
        self.assertEqual(self.srs.renewed, [])

    def test_logout(self) -> None:
        """--logout deletes all saved sessions, and doesn't mind if there are none."""
        path = rooms.cookie_file(make_ctx("rooms"))
        path.parent.mkdir(parents=True)
        path.write_text("# Netscape HTTP Cookie File\n")
        self.saved_session(3)
        self.run_main(["--logout"])
        self.assertEqual(list(path.parent.glob("srs-cookies*")), [])
        self.run_main(["--logout"])
        self.assertEqual(self.srs.calls, [])


class TerminalStream(io.StringIO):
    """A captured stdout that claims to be a terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        """Yes, a terminal."""
        return True


ANSI = re.compile(r"\033\[[0-9;]*m")


class TableTest(ToolTestCase):
    """The default output: a table with a timeline."""

    def table(self, width: int, argv: Sequence[str] = ()) -> list[str]:
        """The tool's default output for a terminal `width` columns wide."""
        self.patch("shutil.get_terminal_size", return_value=os.terminal_size((width, 24)))
        return self.run_main(argv, plain=False)

    def test_table_with_timeline(self) -> None:
        """Wide enough: a timeline of the day, the free times and the equipment, the lists wrapped to fit."""
        self.assertEqual(self.table(120), [
            "Fri 23 Oct 2026 · 08:00–21:00 · free for 30 min or more",
            "╭────────────────┬───────┬───────────┬────────────────────────────┬──────────────────────────┬"
            "──────────────────╮",
            "│ Room           │ Seats │ Kind      │ 8   10  12  14  16  18  20 │ Free                     │"
            " Equipment        │",
            "├────────────────┼───────┼───────────┼────────────────────────────┼──────────────────────────┼"
            "──────────────────┤",
            "│ 2041           │    15 │ lab       │ █░░░░░░░░░░░████░░░█░░░███ │ 08:00–08:30  14:00–16:00 │"
            " –                │",
            "│                │       │           │                            │ 17:30–18:00  19:30–21:00 │"
            "                  │",
            "│ 2180 (sala RW) │   100 │ lecture   │ ██████████████████████████ │ 08:00–21:00              │"
            " –                │",
            "│ 3180           │   156 │ lecture   │ ████░░░░████████░░░███████ │ 08:00–10:00  12:00–16:00 │"
            " projector screen │",
            "│                │       │           │                            │ 17:30–21:00              │"
            " multimedia       │",
            "╰────────────────┴───────┴───────────┴────────────────────────────┴──────────────────────────┴"
            "──────────────────╯",
            "3 rooms · █ free  ░ busy, 30 min per mark",
        ])

    def test_wide_terminal_one_line_per_room(self) -> None:
        """With room to spare, the free times and equipment stay on one line each."""
        lines = self.table(160)
        self.assertTrue(lines[6].endswith("│ 08:00–10:00  12:00–16:00  17:30–21:00              │"
                                          " projector screen multimedia │"))
        self.assertIn("│ 08:00–08:30  14:00–16:00  17:30–18:00  19:30–21:00 │", lines[4])
        self.assertEqual(len(lines), 9)

    def test_narrow_terminal_drops_timeline(self) -> None:
        """Too narrow for the timeline: free times and equipment only."""
        lines = self.table(100)
        self.assertEqual(lines[2], "│ Room           │ Seats │ Kind      │ Free                     │"
                                   " Equipment                   │")
        self.assertEqual(lines[-1], "3 rooms")

    def test_very_narrow_terminal_drops_equipment(self) -> None:
        """Narrower still: just the free times."""
        lines = self.table(70)
        self.assertEqual(lines[2], "│ Room           │ Seats │ Kind      │ Free                     │")
        self.assertEqual(lines[7], "│ 3180           │   156 │ lecture   │ 08:00–10:00  12:00–16:00 │")

    def test_every_line_fits(self) -> None:
        """No line is wider than the terminal, down to one free time per line (about 60 columns here)."""
        for width in (60, 70, 80, 90, 100, 120, 160):
            with self.subTest(width=width):
                self.assertLessEqual(max(len(line) for line in self.table(width)), width)

    def test_timeline_of_a_short_window(self) -> None:
        """--now from 14:05: quarter-hour marks from 14:00, labelled every hour."""
        lines = self.table(120, ["--now", "--lecture"])
        self.assertEqual(lines[0], "Fri 23 Oct 2026 · 14:05–21:00 · free for 30 min or more")
        self.assertIn("│ 14  15  16  17  18  19  20   │", lines[2])
        self.assertIn("│ ████████████████████████████ │", lines[4])  # 2180, free all the time
        self.assertTrue(lines[-1].endswith("15 min per mark"))

    def test_colors_on_terminal(self) -> None:
        """On a terminal there are colors, which don't change the layout."""
        self.patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 24)))
        out = TerminalStream()
        self.patch("sys.stdout", out)
        self.patch_dict(os.environ, {"TERM": "xterm-256color"})
        os.environ.pop("NO_COLOR", None)
        rooms.main(parse(["rooms"]), make_ctx("rooms", cas_user=""))
        self.assertIn(rooms.GREEN, out.getvalue())
        self.assertEqual(ANSI.sub("", out.getvalue()).splitlines(), self.table(120))

    def test_style_choice(self) -> None:
        """Box drawing unless the output can't encode it; colors only on a terminal, and not with NO_COLOR."""
        ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        self.assertEqual(rooms.style_for(ascii_out), rooms.ASCII)
        self.assertEqual(rooms.style_for(io.StringIO()), rooms.UNICODE)
        self.patch_dict(os.environ, {"TERM": "xterm"})
        os.environ.pop("NO_COLOR", None)
        self.assertTrue(rooms.style_for(TerminalStream()).color)
        for env in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
            with self.subTest(env=env):
                self.patch_dict(os.environ, env)
                self.assertFalse(rooms.style_for(TerminalStream()).color)

    def test_ascii_table(self) -> None:
        """The ASCII style draws the same table with plain characters."""
        lines = rooms.pretty([(rooms.parse_rooms(ROOM_PAGES[0]["results"])[0], [interval("08:00-08:30")])],
                             at("08:00"), at("21:00"), datetime.timedelta(minutes=30), rooms.ASCII, 120)
        self.assertEqual(lines[0], "Fri 23 Oct 2026 | 08:00-21:00 | free for 30 min or more")
        self.assertEqual(lines[1], "+------+-------+-----------+" + "-" * 28 + "+-------------+-----------+")
        self.assertEqual(lines[4], "| 2041 |    15 | lab       | #" + "." * 25 + " | 08:00-08:30 | -         |")


class TimelineTest(IsolatedTestCase):
    """The pieces of the timeline."""

    def test_slot_sizes(self) -> None:
        """Finer marks for shorter windows, so the timeline stays about 30 characters wide."""
        for window, mins in [("08:00-21:00", 30), ("12:00-16:00", 15), ("08:00-16:00", 15), ("00:00-23:00", 60)]:
            with self.subTest(window=window):
                self.assertEqual(rooms.slot_for(*interval(window)), datetime.timedelta(minutes=mins))

    def test_flags(self) -> None:
        """A slot is free when at least half of it is; the first slot starts on a whole slot."""
        first, flags = rooms.timeline([interval("08:00-08:30"), interval("09:15-10:00"), interval("10:40-11:00")],
                                      at("07:50"), at("11:00"), datetime.timedelta(minutes=30))
        self.assertEqual(first, at("07:30"))
        self.assertEqual(flags, [False, True, False, True, True, False, True])

    def test_ticks(self) -> None:
        """Hour labels where their hour starts, every 2 h at 2 marks per hour, never overlapping."""
        half = datetime.timedelta(minutes=30)
        self.assertEqual(rooms.ticks(at("08:00"), 26, half), "8   10  12  14  16  18  20")
        self.assertEqual(rooms.ticks(at("08:30"), 25, half), "   10  12  14  16  18  20")
        self.assertEqual(rooms.ticks(at("09:00"), 9, datetime.timedelta(hours=1)), "9  12 15 ")
        self.assertEqual(rooms.ticks(at("14:00"), 8, datetime.timedelta(minutes=15)), "14  15  ")

    def test_wrap(self) -> None:
        """Free times are joined into lines that fit, one per line at least."""
        self.assertEqual(rooms.wrap(["a" * 5, "b" * 5, "c" * 5], 12), ["aaaaa  bbbbb", "ccccc"])
        self.assertEqual(rooms.wrap(["a" * 5, "b" * 5], 3), ["aaaaa", "bbbbb"])
