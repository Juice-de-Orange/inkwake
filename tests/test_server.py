"""Tests for the BYOS endpoints and for source assembly.

No test touches the network or a database. The source modules are
replaced with stubs in sys.modules, the dashboard builder is replaced wholesale
for the endpoint tests, and the store is `tests.fakes.InMemoryStore` -- a second
implementation of the same contract rather than a mock, so a broken merge shows
up as a failing test here instead of as a row that quietly stops updating.

What is exercised is the server's own logic: who gets in, header parsing, the
repaint decision, the conditional GET, the battery gate and the firmware offer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
import threading
import time as time_module
import types
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app import assemble, main
from app.config import settings
from app.models import Booking, Dashboard, Event, Sighting, SunTimes, Telemetry, Weather
from app.schedule import next_slot
from app.store import DbUnavailable, DeviceRecord, FirmwareRecord, hash_device_token
from tests.fakes import InMemoryStore, make_device

#: The row id, and therefore the half of the bearer token in front of the dot.
#: Hex on purpose: auth.parse_device_token_id refuses anything else before the
#: value goes near the store, so a token with a non-hex id never costs a lookup.
DEVICE_ID = "a1b2c3d4e5f60718293a"
#: Obviously fake, but the same shape `new_device_token` produces.
DEVICE_SECRET = "test-secret-not-a-real-token"
TOKEN = f"{DEVICE_ID}.{DEVICE_SECRET}"

MAC = "AA:BB:CC:DD:EE:01"
TZ = settings.timezone

#: The operational limits live on the device row now, not in `settings`. The
#: fixture writes exactly these numbers into the seeded row and the tests read
#: them from here, so the two can never drift apart silently. A test that wants
#: a different limit calls `env.set_device`, never `override` -- poking settings
#: would prove nothing, because the endpoints stopped reading it.
SLOTS_MIN = (6 * 60 + 50, 14 * 60 + 50, 19 * 60 + 50)
REFRESH_JITTER_S = 20
MIN_REFRESH_GAP_S = 180
MAX_IMAGE_AGE_S = 23 * 3600
MIN_REFRESH_TEMP_C = 15.0
BATTERY_LOW_MV = 3300
BATTERY_RECOVER_MV = 3500
LOW_BATTERY_SLEEP_S = 6 * 3600
OTA_MIN_BATTERY_PCT = 50

#: Tells "the key was absent" apart from "the key held None"; a failed import
#: legitimately parks None in sys.modules.
_ABSENT = object()

# A Tuesday morning between the 06:50 and the 14:50 slot.
T0 = datetime(2026, 8, 25, 9, 13, 0, tzinfo=TZ)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Clock:
    """A stand-in for main._now, so a frame's minute text is deterministic."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@contextmanager
def override(**values: Any) -> Iterator[None]:
    """Settings is a frozen dataclass singleton; poke it and put it back.

    The pokes happen inside the try because a half-applied override has to be
    undone too: getattr raises on a typo'd key, and the fields set before it
    would otherwise stay set for the rest of the session.
    """
    previous = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        yield
    finally:
        for key, value in previous.items():
            object.__setattr__(settings, key, value)


def dashboard_factory(content: dict[str, Any]):
    def build(telemetry: Telemetry, now: datetime, **_: Any) -> Dashboard:
        return Dashboard(
            now=now,
            next_refresh=next_slot(now, settings.refresh_slots, TZ),
            telemetry=telemetry,
            weather=Weather(
                temp_c=21.5,
                code=1,
                description=content["weather"],
                precip_prob_pct=10,
                temp_min_c=12.0,
                temp_max_c=24.0,
            ),
            sun=SunTimes(sunrise=time(5, 42), sunset=time(20, 12)),
            bookings=[
                Booking(
                    arrival=date(2026, 8, 28),
                    departure=date(2026, 8, 31),
                    nights=3,
                    name="Familie Huber",
                    guests=4,
                    comment="spaete Anreise",
                )
            ],
            events=[
                Event(
                    day=date(2026, 8, 26),
                    start="19:30",
                    title="Theater im Hof",
                    location="Schlosshof",
                )
            ],
            sighting=None,
            failures=list(content.get("failures", [])),
        )

    return build


def seed_device_row(store: InMemoryStore, *, token: str = TOKEN, **fields: Any) -> DeviceRecord:
    """Put a row in place the way `device add` does, token hash included.

    `hash_device_token` is deliberately the same function the CLI calls, and
    it hashes the FULL `<id>.<secret>` string. A test that hashed only the
    secret would pass against a server that did the same -- and lock the real
    board out, because the CLI does not.
    """
    defaults: dict[str, Any] = {
        "mac": MAC,
        "slots": SLOTS_MIN,
        "refresh_jitter_s": REFRESH_JITTER_S,
        "min_refresh_gap_s": MIN_REFRESH_GAP_S,
        "max_image_age_s": MAX_IMAGE_AGE_S,
        "min_refresh_temp_c": MIN_REFRESH_TEMP_C,
        "battery_low_mv": BATTERY_LOW_MV,
        "battery_recover_mv": BATTERY_RECOVER_MV,
        "low_battery_sleep_s": LOW_BATTERY_SLEEP_S,
        "ota_min_battery_pct": OTA_MIN_BATTERY_PCT,
    }
    defaults.update(fields)
    return store.seed_device(
        make_device(DEVICE_ID, token_hash=hash_device_token(token), **defaults)
    )


#: "Send whatever the fixture set up." Distinct from None, which means "send no
#: credential at all" -- the anonymous case, which is a 401 and has its own test.
_DEFAULT = object()


class Env:
    def __init__(
        self,
        client: TestClient,
        clock: Clock,
        content: dict[str, Any],
        tmp: Path,
        store: InMemoryStore,
        token: str = TOKEN,
    ) -> None:
        self.client = client
        self.clock = clock
        self.content = content
        self.tmp = tmp
        self.store = store
        self.token = token
        self.device_id = DEVICE_ID

    # -- the row ------------------------------------------------------------

    @property
    def device(self) -> DeviceRecord:
        row = self.store.get_device(self.device_id)
        assert row is not None, "the fixture's device row went missing"
        return row

    def set_device(self, **fields: Any) -> DeviceRecord:
        """Rewrite the row the way `device set` would.

        Every operational limit is per-device now, so this is where a test
        changes a threshold. `override(min_refresh_gap_s=...)` would change
        nothing at all: /api/display reads the row, not the environment.
        """
        return self.store.seed_device(replace(self.device, **fields))

    # -- requests -----------------------------------------------------------

    def headers(
        self,
        extra: Optional[dict[str, str]] = None,
        *,
        token: Any = _DEFAULT,
        id: Optional[str] = MAC,
    ) -> dict[str, str]:
        """Both headers every real request carries.

        Authorization is what authenticates. ID is pure telemetry now and
        decides nothing -- it is sent anyway precisely so the tests exercise the
        case where the two disagree, which is what
        `test_log_files_under_the_authenticated_id` is about.
        """
        merged = dict(extra or {})
        if token is _DEFAULT:
            token = self.token
        if token is not None:
            merged.setdefault("Authorization", f"Bearer {token}")
        if id is not None:
            merged.setdefault("ID", id)
        return merged

    def display(self, *, token: Any = _DEFAULT, **headers: str) -> dict[str, Any]:
        response = self.display_raw(token=token, **headers)
        assert response.status_code == 200, response.text
        return response.json()

    def display_raw(self, *, token: Any = _DEFAULT, **headers: str):
        """The unwrapped response, for tests that expect a non-200."""
        return self.client.get("/api/display", headers=self.headers(headers, token=token))

    def get(self, url: str, headers: Optional[dict[str, str]] = None, *, token: Any = _DEFAULT):
        return self.client.get(url, headers=self.headers(headers, token=token))

    def post(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        *,
        token: Any = _DEFAULT,
        **kwargs: Any,
    ):
        return self.client.post(url, headers=self.headers(headers, token=token), **kwargs)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    content: dict[str, Any] = {"weather": "Sonnig"}
    clock = Clock(T0)
    store = InMemoryStore()
    seed_device_row(store)

    # admin_api_key is pinned empty rather than left alone: it is read from
    # the environment, and a developer who exports one for a deploy would
    # otherwise turn every /preview in this file into a 401.
    with override(
        data_dir=tmp_path,
        admin_api_key="",
    ):
        # reset_state() drops the store handle, so the fake goes in after it,
        # never before -- and before the lifespan, which calls get_store().
        main.reset_state()
        main.set_store(store)
        monkeypatch.setattr(main, "_now", clock)
        monkeypatch.setattr(assemble, "build_dashboard", dashboard_factory(content))
        try:
            with TestClient(main.app) as client:
                yield Env(client, clock, content, tmp_path, store)
        finally:
            # main holds the store and the last frame in module globals keyed
            # to this tmp_path. A lifespan shutdown that raises must not leave
            # them there for the next test, which gets a different tmp_path --
            # and must never leave a *real* Store behind for a test that
            # expects the fake and has no database to talk to.
            main.reset_state()


class _Unreachable(InMemoryStore):
    """A store whose device lookup cannot answer at all."""

    def get_device(self, device_id: str) -> Optional[DeviceRecord]:
        raise DbUnavailable("connection refused")


class _DiesAfterTheLookup(InMemoryStore):
    """Authentication succeeds, the telemetry write does not."""

    def record_telemetry(self, *args: Any, **kwargs: Any) -> Optional[DeviceRecord]:
        raise DbUnavailable("connection reset by peer")


class _DeletedMidRequest(InMemoryStore):
    """The operator removed the row between the lookup and the write."""

    def record_telemetry(self, device_id: str, *args: Any, **kwargs: Any) -> Optional[DeviceRecord]:
        self.devices.pop((device_id or "").strip(), None)
        return None


class _WritesFail(InMemoryStore):
    """Reads answer, every write raises. What a Postgres blip looks like.

    Deliberately not `record_telemetry`: that one is the 503 path and has its own
    test. These are the writes that go through `main._record`, which is the only
    thing standing between a failing UPDATE and a 500 for the board.
    """

    def record_paint(self, *args: Any, **kwargs: Any) -> None:
        raise DbUnavailable("update failed")

    def record_image_fetch(self, *args: Any, **kwargs: Any) -> None:
        raise DbUnavailable("update failed")

    def set_low_battery(self, *args: Any, **kwargs: Any) -> None:
        raise DbUnavailable("update failed")

    def add_log(self, *args: Any, **kwargs: Any) -> str:
        raise DbUnavailable("insert failed")

    def list_devices(self) -> list[DeviceRecord]:
        raise DbUnavailable("select failed")


def _swap_store(env: Env, store: InMemoryStore) -> InMemoryStore:
    """Hand main a differently broken store, and point env at it as well.

    `env.store` has to follow, or `env.device` would read a store the server
    never touched and an assertion about the row would be about nothing. The
    fixture's teardown does not put the original back -- reset_state() only drops
    main's handle -- so nothing after a swap may assume it is there.
    """
    seed_device_row(store)
    main.set_store(store)
    env.store = store
    return store


# --------------------------------------------------------------------------
# The shipped defaults
# --------------------------------------------------------------------------


def test_the_seeded_row_carries_the_shipped_defaults() -> None:
    """The numbers this file asserts against are the ones a real row has.

    Without this the suite would happily pin values that exist nowhere but in
    this file. That is exactly what the move onto the row nearly cost: the old
    battery test read `main.LOW_BATTERY_SLEEP_S`, so the shipped six hours were
    asserted; the constant here is a copy, and a copy compared against itself
    proves nothing. `DeviceRecord`'s defaults are the Python-side mirror of the
    column defaults the migration wrote, so they are what has to be checked.
    """
    shipped = make_device()
    assert shipped.slots == SLOTS_MIN
    assert shipped.refresh_jitter_s == REFRESH_JITTER_S
    assert shipped.min_refresh_gap_s == MIN_REFRESH_GAP_S
    assert shipped.max_image_age_s == MAX_IMAGE_AGE_S
    assert shipped.min_refresh_temp_c == MIN_REFRESH_TEMP_C
    assert shipped.battery_low_mv == BATTERY_LOW_MV
    assert shipped.battery_recover_mv == BATTERY_RECOVER_MV
    assert shipped.low_battery_sleep_s == LOW_BATTERY_SLEEP_S
    assert shipped.ota_min_battery_pct == OTA_MIN_BATTERY_PCT

    # And the three panel-care rules the defaults have to satisfy. A row the
    # operator can edit may go tighter; the shipped value may not go looser,
    # because the documented failure for rule 1 is permanent damage and for
    # rule 2 it is burn-in.
    assert MIN_REFRESH_GAP_S >= 180
    assert MAX_IMAGE_AGE_S <= 24 * 3600
    assert MIN_REFRESH_TEMP_C >= 15.0
    # Long enough that polling cannot itself drain a flat cell, short enough
    # that a charge is noticed the same day (9.6).
    assert 3600 < LOW_BATTERY_SLEEP_S <= 12 * 3600


# --------------------------------------------------------------------------
# Authentication
#
# There is no enrolment endpoint: the server never mints keys, the operator
# creates the row with `python -m app.cli device add` and the token is shown
# there once. So the thing worth pinning is "only the row the operator created
# gets in at all".
#
# It is worth remembering what the old check did, because it looked like
# authentication and was not: it hung off `paired_at` and let an unknown MAC
# through. Chained with the frame name /healthz used to publish, that was a way
# to pull a PNG carrying guests' names off a public host with no credential.
# --------------------------------------------------------------------------


def test_the_right_token_is_let_in(env: Env) -> None:
    assert env.display_raw().status_code == 200


def test_no_token_is_401_on_every_device_endpoint(env: Env) -> None:
    """The ID header alone used to be enough. It is not a credential.

    Each of these leads to a frame, to the bytes of one, or to a disk write, so
    none of them may answer an anonymous caller.
    """
    assert env.display_raw(token=None).status_code == 401
    assert env.get("/api/image/whatever.png", token=None).status_code == 401
    assert env.post("/api/log", json={"message": "hi"}, token=None).status_code == 401
    assert env.get("/api/firmware/fw01", token=None).status_code == 401


def test_a_wrong_secret_is_401(env: Env) -> None:
    """The id half is right, so this is the case the lookup actually reaches.

    Only the hash of the FULL token is stored, so one changed character in the
    secret cannot match. The replacement has to differ from what it replaces --
    otherwise the "wrong" token would be the real one on a secret that happens
    to end in that character.
    """
    wrong = TOKEN[:-1] + ("A" if TOKEN[-1] != "A" else "B")
    assert env.display_raw(token=wrong).status_code == 401


def test_an_unknown_id_is_401(env: Env) -> None:
    """A well-formed token for a row that does not exist."""
    assert env.display_raw(token=f"{'f' * 20}.{DEVICE_SECRET}").status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(DEVICE_SECRET, id="no-dot"),
        pytest.param(".secret", id="empty-id"),
        pytest.param("zz11.secret", id="id-not-hex"),
        pytest.param(f"{'a' * 65}.secret", id="id-too-long"),
    ],
)
def test_a_malformed_token_is_401_without_a_lookup(env: Env, token: str) -> None:
    """Refused before the value goes anywhere near a query.

    Not because the driver would interpolate it -- it would not -- but because a
    4 kB "id" is a firmware bug or a probe, and either way the answer is 401
    without a round trip. The unreachable store is what proves no round trip
    happened: any lookup at all would surface as 503, not 401.
    """
    _swap_store(env, _Unreachable())
    assert env.display_raw(token=token).status_code == 401


def test_a_disabled_device_is_403_and_not_401(env: Env) -> None:
    """The two codes mean opposite things to the firmware.

    401 says "your identity is wrong, ask a human to re-provision you"; 403 says
    "you are who you say you are and the operator switched you off". A board
    switched off with `device set --disable` must not walk into a provisioning
    loop over it.
    """
    env.set_device(active=False)
    assert env.display_raw().status_code == 403

    env.set_device(active=True)
    assert env.display_raw().status_code == 200


def test_a_store_outage_is_503_and_never_401(env: Env) -> None:
    """The whole reason DbUnavailable exists as its own exception.

    A 401 here would tell a future firmware its identity is broken and, once the
    provisioning ladder is in place, would send a perfectly good board into setup
    mode over a restart of Postgres. A 503 is just "no content this wake", which
    the firmware's retry ladder already handles.
    """
    _swap_store(env, _Unreachable())
    assert env.display_raw().status_code == 503
    assert env.get("/api/image/whatever.png").status_code == 503
    assert env.post("/api/log", json={}).status_code == 503
    assert env.get("/api/firmware/fw01").status_code == 503


def test_a_store_that_dies_after_the_lookup_is_also_503(env: Env) -> None:
    """The second store call in /api/display gets the same treatment."""
    _swap_store(env, _DiesAfterTheLookup())
    assert env.display_raw().status_code == 503


def test_a_row_deleted_mid_request_is_401(env: Env) -> None:
    """Authorised a moment ago and gone now: the token belongs to nothing."""
    _swap_store(env, _DeletedMidRequest())
    assert env.display_raw().status_code == 401


def test_a_failing_store_write_never_costs_the_board_its_answer(env: Env) -> None:
    """Every write goes through `_record`, and that is the whole point of it.

    A 500 here is not "one lost wake": the board sleeps on its firmware default
    or not at all, and the countdown is the only thing that tells it when to come
    back. So a paint that could not be recorded, a fetch stamp that was lost, a
    log line that did not land and a device list the pruner could not read all
    have to end in the same ordinary response.
    """
    _swap_store(env, _WritesFail())

    # A full render, so the pruner's device list and the paint record both fail.
    body = env.display()
    assert body["refresh_rate"] > 0
    assert body["filename"].endswith(".png")

    assert env.get(f"/api/image/{body['filename']}").status_code == 200
    assert env.post("/api/log", json={"message": "hi"}).status_code == 204

    env.clock.advance(400)
    gated = env.display(BATTERY_VOLTAGE="3.10")  # and the low-battery latch
    assert gated["refresh_rate"] > 0


def test_the_legacy_access_token_header_is_still_accepted(env: Env) -> None:
    """Both headers are read, permanently, and there is no switch between them.

    The board sends the same secret over the same TLS connection either way, so
    neither is weaker -- and a configuration flag would be a third thing to set
    wrongly on hardware whose BOOT button sits behind a wall mount. Underscore
    and hyphen are genuinely different header names (9.2), so both spellings
    are checked.
    """
    for name in ("ACCESS_TOKEN", "Access-Token"):
        response = env.client.get("/api/display", headers={"ID": MAC, name: TOKEN})
        assert response.status_code == 200, name


def test_a_bare_token_without_the_scheme_is_tolerated(env: Env) -> None:
    """One branch, against a board that is silently unauthenticated because
    somebody pasted the value without the word "Bearer" in front of it."""
    assert env.client.get("/api/display", headers={"Authorization": TOKEN}).status_code == 200


# --------------------------------------------------------------------------
# /api/display
# --------------------------------------------------------------------------


def test_display_without_any_telemetry(env: Env) -> None:
    """A board that authenticates and reports no sensors at all."""
    body = env.display()
    assert set(body) >= {
        "filename",
        "image_url",
        "refresh_rate",
        "update_firmware",
        "firmware_url",
        "reset_firmware",
        "special_function",
        "image_url_timeout",
    }
    assert body["filename"].endswith(".png")
    assert body["image_url"].endswith(body["filename"])
    assert body["special_function"] == "none"
    assert body["update_firmware"] is False
    assert body["refresh_rate"] > 0


def test_refresh_rate_points_at_the_next_slot(env: Env) -> None:
    body = env.display()
    # 09:13 -> 14:50 is 5 h 37 min; jitter is only ever added.
    expected = 5 * 3600 + 37 * 60
    assert expected <= body["refresh_rate"] <= expected + REFRESH_JITTER_S


def test_the_wake_slots_come_from_the_row(env: Env) -> None:
    """Minutes since midnight, because that is what a CHECK can express and what
    an <input> round-trips without a parser. The REFRESH_SLOTS environment value
    is only what the migration wrote into the column."""
    env.set_device(slots=(6 * 60, 18 * 60))
    body = env.display()
    expected = 8 * 3600 + 47 * 60  # 09:13 -> 18:00, not the 14:50 default
    assert expected <= body["refresh_rate"] <= expected + REFRESH_JITTER_S


def test_refresh_rate_wraps_past_midnight(env: Env) -> None:
    env.clock.now = datetime(2026, 8, 25, 20, 30, tzinfo=TZ)
    body = env.display()
    expected = 10 * 3600 + 20 * 60  # 20:30 -> 06:50 next morning
    assert expected <= body["refresh_rate"] <= expected + REFRESH_JITTER_S


def test_countdown_absorbs_the_spring_forward(env: Env) -> None:
    """The board holds no tz database, so the countdown must carry the hour.

    2026-03-29 is the European spring transition: 02:00 CET becomes 03:00
    CEST, so the night between the 19:50 and the 06:50 slot is one hour
    shorter in real time.
    """
    env.clock.now = datetime(2026, 3, 28, 20, 30, tzinfo=TZ)
    body = env.display()
    # 10 h 20 min of wall clock, one hour of which does not exist.
    expected = 9 * 3600 + 20 * 60
    assert expected <= body["refresh_rate"] <= expected + REFRESH_JITTER_S


def test_countdown_absorbs_the_autumn_fallback(env: Env) -> None:
    """2026-10-25: 03:00 CEST becomes 02:00 CET, so the night is longer."""
    env.clock.now = datetime(2026, 10, 24, 20, 30, tzinfo=TZ)
    body = env.display()
    expected = 11 * 3600 + 20 * 60
    assert expected <= body["refresh_rate"] <= expected + REFRESH_JITTER_S


def test_slot_boundary_never_returns_a_zero_sleep(env: Env) -> None:
    env.clock.now = datetime(2026, 8, 25, 14, 50, 0, tzinfo=TZ)
    body = env.display()
    # Exactly on the slot the next one is 5 h away, never 0: a board handed 0
    # power-cycles until the battery is flat.
    assert body["refresh_rate"] >= 5 * 3600


def test_unchanged_content_keeps_the_same_filename(env: Env) -> None:
    first = env.display()
    second = env.display()
    assert first["filename"] == second["filename"]

    # Only the first wake was a repaint; the second is the cheap one.
    assert env.device.refresh_count == 1


def test_changed_content_produces_a_new_filename(env: Env) -> None:
    first = env.display()
    env.clock.advance(400)  # past the 180 s panel-care floor
    env.content["weather"] = "Gewitter"
    second = env.display()

    assert second["filename"] != first["filename"]
    assert env.device.refresh_count == 2


def test_panel_care_floor_beats_a_content_change(env: Env) -> None:
    first = env.display()
    env.clock.advance(60)  # inside the 180 s minimum gap
    env.content["weather"] = "Gewitter"
    second = env.display()

    assert second["filename"] == first["filename"]
    assert env.device.refresh_count == 1


def test_the_panel_care_floor_comes_from_the_row(env: Env) -> None:
    """Per-device, so the operator can widen it without a redeploy."""
    env.set_device(min_refresh_gap_s=900)
    first = env.display()
    env.clock.advance(400)  # well past the 180 s default, inside the row's 900
    env.content["weather"] = "Gewitter"
    second = env.display()

    assert second["filename"] == first["filename"]
    assert env.device.refresh_count == 1


def test_an_unchanged_frame_is_forced_out_once_the_row_says_it_is_old(env: Env) -> None:
    """Panel rule 2: an image held indefinitely is the documented burn-in path.

    The row's age limit is set to seconds so the elapsed time stays inside one
    wall-clock minute -- the layout prints "Stand HH:MM", so a longer wait would
    change the pixels and the repaint would be an ordinary content change rather
    than the forced one this test is about.
    """
    env.set_device(min_refresh_gap_s=10, max_image_age_s=30)
    first = env.display()
    env.clock.advance(40)
    second = env.display()

    assert second["filename"] == first["filename"], "same pixels..."
    assert env.device.refresh_count == 2, "...and painted again anyway"


def test_cold_panel_is_not_repainted(env: Env) -> None:
    env.display(TEMPERATURE="21.0")
    env.clock.advance(400)
    env.content["weather"] = "Schneefall"
    body = env.display(TEMPERATURE="4.5")

    device = env.device
    assert device.refresh_count == 1
    assert body["filename"] == f"{device.last_etag}.png"


def test_the_cold_panel_floor_comes_from_the_row(env: Env) -> None:
    """A panel in an unheated room is the operator's problem, not the image's."""
    env.set_device(min_refresh_temp_c=25.0)
    body = env.display(TEMPERATURE="21.0")

    # 21 C would have painted under the 15 C default; under the row's 25 it does
    # not, and the board is told to keep whatever it already holds.
    assert env.device.refresh_count == 0
    assert body["refresh_rate"] > 0


def test_display_survives_a_broken_assembler(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(telemetry: Telemetry, now: datetime) -> Dashboard:
        raise RuntimeError("upstream module is on fire")

    env.display()  # one good frame exists
    monkeypatch.setattr(assemble, "build_dashboard", explode)
    env.clock.advance(400)

    body = env.display()
    assert body["refresh_rate"] > 0
    assert body["filename"].endswith(".png")


def test_display_survives_a_renderer_that_raises(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different promise from the assembler one, and a different code path.

    `_resolve_renderer` finds app/render/layout.py in any real checkout, so the
    placeholder and the "render" badge are otherwise never reached from here. At
    07:00 with a board waiting, a deliberately ugly but truthful frame beats no
    frame -- and the badge is the only thing that says so on the glass.
    """

    def explode(dash: Dashboard) -> Any:
        raise ZeroDivisionError("layout maths went wrong")

    env.display()  # one good frame exists
    monkeypatch.setattr(main, "_resolve_renderer", lambda: explode)
    env.clock.advance(400)
    env.content["weather"] = "Gewitter"

    body = env.display()
    assert body["filename"].endswith(".png")
    assert body["refresh_rate"] > 0

    frame = main.current_frame()
    assert frame is not None
    assert "render" in frame.dashboard.failures


def test_the_panel_care_floor_counts_from_the_later_of_the_two_stamps(env: Env) -> None:
    """Told-to-paint and actually-fetched are different clocks, and both count.

    `last_painted_at` alone let anyone who could reach /api/display keep the
    floor artificially fresh and freeze the panel. Here the paint is old enough
    to allow a repaint and the fetch is not, so the fetch has to be the one that
    decides -- otherwise the 180 s rule is enforced against the wrong instant.
    """
    first = env.display()  # painted at T0

    env.clock.advance(200)  # past the floor, so this is a legitimate fetch
    assert env.get(f"/api/image/{first['filename']}").status_code == 200

    env.clock.advance(100)
    env.content["weather"] = "Gewitter"
    second = env.display()

    device = env.device
    painted_ago = (env.clock.now - device.last_painted_at).total_seconds()
    assert painted_ago > MIN_REFRESH_GAP_S, "the paint stamp alone would have allowed this"
    assert second["filename"] == first["filename"]
    assert device.refresh_count == 1


# --------------------------------------------------------------------------
# Battery gate
# --------------------------------------------------------------------------


def test_low_battery_gets_a_long_sleep(env: Env) -> None:
    body = env.display(BATTERY_VOLTAGE="3.15", PERCENT_CHARGED="4")
    # That six hours is long enough is pinned against the shipped column default
    # in test_the_seeded_row_carries_the_shipped_defaults; comparing the constant
    # in this file against a literal would only compare it with itself.
    assert body["refresh_rate"] == LOW_BATTERY_SLEEP_S

    device = env.device
    assert device.low_battery is True
    assert device.battery_voltage_mv == 3150
    # No repaint was instructed while the cell is flat.
    assert device.refresh_count == 0


def test_the_battery_thresholds_come_from_the_row(env: Env) -> None:
    """All three of them, and none of them from the environment any more.

    3750 mV is comfortably above the 3300 mV default, so a server still reading
    settings would sleep to the next slot instead of gating.
    """
    env.set_device(
        battery_low_mv=3800, battery_recover_mv=3900, low_battery_sleep_s=7200
    )
    body = env.display(BATTERY_VOLTAGE="3750")
    assert body["refresh_rate"] == 7200
    assert env.device.low_battery is True


def test_a_stale_charging_flag_cannot_hold_the_gate_open(env: Env) -> None:
    """The board stops reporting charge state the moment its PMIC goes quiet.

    Every telemetry field is stored through COALESCE, so the last "charging"
    would otherwise stand for the rest of the device's life -- and this gate,
    which exists to stop commissioning repaints on a draining cell, would
    never close again. Only the current wake's answer may open it.
    """
    env.display(BATTERY_VOLTAGE="4050", BATTERY_CHARGING="1")
    assert env.device.is_charging is True

    env.clock.advance(6 * 3600)
    # Cable gone, cell low, and no BATTERY_CHARGING header at all this time.
    body = env.display(BATTERY_VOLTAGE="3150")
    assert body["refresh_rate"] == LOW_BATTERY_SLEEP_S


def test_a_reported_charge_still_opens_the_gate(env: Env) -> None:
    """The other half: a board that says it is on a cable must not be nagged."""
    body = env.display(BATTERY_VOLTAGE="3150", BATTERY_CHARGING="1")
    assert body["refresh_rate"] != LOW_BATTERY_SLEEP_S


def test_low_battery_recovery_needs_the_higher_threshold(env: Env) -> None:
    env.display(BATTERY_VOLTAGE="3150")
    env.clock.advance(6 * 3600)

    # Above the low mark but below the recover mark: still gated, no flapping.
    still = env.display(BATTERY_VOLTAGE="3400")
    assert still["refresh_rate"] == LOW_BATTERY_SLEEP_S

    env.clock.advance(6 * 3600)
    recovered = env.display(BATTERY_VOLTAGE="3600")
    assert recovered["refresh_rate"] != LOW_BATTERY_SLEEP_S
    assert env.device.low_battery is False


def test_gated_wake_keeps_the_frame_the_panel_is_holding(env: Env) -> None:
    """Regression: the gate must not provoke the repaint it exists to prevent.

    A refused repaint still leaves a newer frame on the server, and handing
    *that* name to a flat board would make it download and drive the panel.
    """
    painted = env.display(BATTERY_VOLTAGE="4.0")["filename"]

    env.clock.advance(60)  # inside the panel-care floor: render, do not paint
    env.content["weather"] = "Gewitter"
    env.display(BATTERY_VOLTAGE="4.0")
    newest = main.current_frame()
    assert newest is not None and newest.name != painted

    env.clock.advance(60)
    body = env.display(BATTERY_VOLTAGE="3.10")
    assert body["refresh_rate"] == LOW_BATTERY_SLEEP_S
    assert body["filename"] == painted
    assert body["image_url"].endswith(painted)


def test_charging_never_gates(env: Env) -> None:
    body = env.display(BATTERY_VOLTAGE="3.10", BATTERY_CHARGING="true")
    assert body["refresh_rate"] != LOW_BATTERY_SLEEP_S


# --------------------------------------------------------------------------
# The firmware offer in /api/display
#
# Five conditions, each with its own test, because from the outside every "no"
# looks exactly like every other one: the response is a perfectly ordinary
# display payload with update_firmware false. An operator who has just assigned
# an image and sees nothing happen has no way to tell which condition bit.
# --------------------------------------------------------------------------


def _seed_firmware(
    env: Env,
    *,
    firmware_id: str = "fw01",
    version: str = "0.2.0",
    target: str = "eink",
    payload: bytes = b"OTA image bytes, near enough for a test",
    assign: bool = True,
    on_disk: Optional[bytes] = None,
) -> FirmwareRecord:
    """An uploaded image plus the row that describes it.

    `on_disk` writes different bytes than the row claims, which is how the
    half-written-upload case is built.

    It also stamps the panel as freshly painted, and that is not tidying up: an
    offer is only ever made when panel-care rule 2 is NOT due. A row that has
    never been painted for is due by definition, so without this every offer
    test would be testing the burn-in guard instead of the offer. The
    interaction itself has its own test below.
    """
    root = settings.firmware_dir
    relative = f"{firmware_id}.bin"
    root.mkdir(parents=True, exist_ok=True)
    (root / relative).write_bytes(payload if on_disk is None else on_disk)

    env.set_device(last_painted_at=env.clock(), last_image_fetched_at=env.clock())

    record = FirmwareRecord(
        id=firmware_id,
        target=target,
        version=version,
        file_path=relative,
        file_name=f"eink-{version}.bin",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    env.store.seed_firmware(record)
    if assign:
        env.set_device(firmware_id=firmware_id)
    return record


def test_an_assigned_image_is_offered_with_everything_the_board_needs(env: Env) -> None:
    """Digest and length travel in the response, not in a download header.

    The board checks the digest before it writes the OTA partition, `Update.begin()`
    needs the length up front, and it has neither Range nor resume -- a header
    would arrive only once the transfer had already started.
    """
    firmware = _seed_firmware(env)
    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED="80")

    assert body["update_firmware"] is True
    assert body["firmware_version"] == "0.2.0"
    assert body["firmware_sha256"] == firmware.sha256
    assert body["firmware_size"] == firmware.size_bytes
    # Absolute: net.cpp runs only image_url through absolutise(), so a relative
    # firmware_url would be resolved against nothing.
    assert body["firmware_url"] == f"{settings.public_base_url}/api/firmware/fw01"


def test_a_due_daily_refresh_outranks_an_update(env: Env) -> None:
    """Rule 2 wins, and this is the one that would have been expensive.

    The offer repeats on every wake for as long as the board reports the old
    version -- so an update that can never finish (weak signal, wrong image,
    full disk) would suspend the mandatory daily refresh for ever, because the
    firmware branch returns before should_repaint() is ever reached. The
    documented failure mode is burn-in: permanent, and caused by the feature
    that was meant to make the device easier to look after.

    One slot of delay for the update against a rule whose violation cannot be
    undone.
    """
    _seed_firmware(env)
    # Older than max_image_age_s (23 h by default), so the forced refresh is due.
    stale = env.clock() - timedelta(seconds=env.device.max_image_age_s + 60)
    stale_etag = "a" * 64
    env.set_device(last_painted_at=stale, last_image_fetched_at=stale, last_etag=stale_etag)

    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED="80")

    assert body["update_firmware"] is False
    # And the repaint really happened -- withholding the offer is only worth
    # anything if the panel gets its refresh out of it.
    assert body["filename"].endswith(".png")
    assert body["filename"] != f"{stale_etag}.png"
    assert env.device.last_painted_at == env.clock()


def test_no_offer_without_an_assignment(env: Env) -> None:
    """Condition 1. There is deliberately no "newest image for everyone":
    a staged rollout is the only kind this project does.

    What this can and cannot catch is worth being honest about. It pins the
    named regression -- an image sitting in the store, newer than what the board
    reports, and still not handed out -- and it would go red the moment somebody
    added a "pick the latest" lookup. It cannot catch the narrower mutation of
    deleting the `not device.firmware_id` check alone, because the fetch that
    follows would then be `get_firmware(None)`, which misses as well. The
    assertion below is here so that redundancy is visible rather than mistaken
    for the reason the test passes.
    """
    firmware = _seed_firmware(env, assign=False)
    assert env.store.get_firmware(firmware.id) is not None, "the image really is there"
    assert env.device.firmware_id is None

    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED="80")
    assert body["update_firmware"] is False
    assert body["firmware_url"] is None


def test_no_offer_when_this_request_did_not_report_a_version(env: Env) -> None:
    """Condition 2, and the stored value expressly does not count.

    COALESCE carries the last reported version forward, so after a successful
    update the row would keep saying the old number for as long as the new
    firmware omits the header -- and the server would reissue the same job every
    wake. Here the row says 0.1.0 and this wake says nothing: "unknown" is not
    "out of date".
    """
    _seed_firmware(env)
    env.set_device(fw_version="0.1.0")
    body = env.display(PERCENT_CHARGED="80")

    assert env.device.fw_version == "0.1.0", "the stale value really is on the row"
    assert body["update_firmware"] is False


def test_no_offer_when_the_reported_version_already_matches(env: Env) -> None:
    """Condition 3."""
    _seed_firmware(env, version="0.2.0")
    body = env.display(FW_VERSION="0.2.0", PERCENT_CHARGED="80")
    assert body["update_firmware"] is False


def test_no_offer_for_an_image_built_for_another_device_class(env: Env) -> None:
    """Condition 4. The foreign key cannot express this, so it is checked here
    -- and again where the bytes go out. A CYD image on this board would pass
    the checksum, flash, and not boot."""
    _seed_firmware(env, target="cyd")
    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED="80")
    assert body["update_firmware"] is False


def test_no_offer_on_a_cell_that_cannot_afford_it(env: Env) -> None:
    """Condition 5, and new against the mains-powered sibling device: an update
    is 1.35 MB over WiFi at ~120 mA plus a flash write, on a board that hangs on
    a wall."""
    _seed_firmware(env)
    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED=str(OTA_MIN_BATTERY_PCT - 1))
    assert body["update_firmware"] is False

    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED=str(OTA_MIN_BATTERY_PCT))
    assert body["update_firmware"] is True


def test_an_offered_update_never_shares_its_wake_with_a_repaint(env: Env) -> None:
    """Flashing is 1.35 MB of radio; a cold panel refresh is 35 s of panel drive.

    Both in one cycle would push a healthy wake against the wake watchdog, and
    the firmware powers the panel rail down before the transfer anyway. One slot
    of delay buys the certainty that they cannot overlap.

    The row starts out freshly painted -- that is what _seed_firmware does, and
    it has to, because an offer is only made when rule 2 is not due. So what
    this asserts is that the wake changes NOTHING about the glass: same etag,
    same stamp, same count.
    """
    _seed_firmware(env)
    before = env.device
    env.clock.advance(600)  # past the 180 s floor, so nothing else is holding it back
    body = env.display(FW_VERSION="0.1.0", PERCENT_CHARGED="80")

    assert body["update_firmware"] is True
    device = env.device
    assert device.refresh_count == before.refresh_count
    assert device.last_painted_at == before.last_painted_at
    assert device.last_etag == before.last_etag


# --------------------------------------------------------------------------
# /api/firmware
# --------------------------------------------------------------------------


def test_firmware_is_served_to_the_device_it_is_assigned_to(env: Env) -> None:
    firmware = _seed_firmware(env)
    response = env.get(f"/api/firmware/{firmware.id}")

    assert response.status_code == 200
    assert int(response.headers["content-length"]) == firmware.size_bytes
    assert response.headers["x-firmware-sha256"] == firmware.sha256
    assert response.headers["x-firmware-version"] == firmware.version
    # private/no-store because the image is authorised per device: an edge cache
    # in front of the bearer check would hand it to anyone who guessed the id.
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["accept-ranges"] == "none"
    assert response.headers["content-disposition"] == 'attachment; filename="eink-0.2.0.bin"'
    assert len(response.content) == firmware.size_bytes


def test_the_download_filename_cannot_close_its_own_header(env: Env) -> None:
    """The name grew out of something an operator typed into an upload form, so
    it is untrusted by the time it reaches a Content-Disposition. A quote in it
    would end the quoted string and let the rest be read as header parameters."""
    firmware = _seed_firmware(env)
    env.store.seed_firmware(
        replace(firmware, file_name='ev"il; filename="passwd')
    )
    disposition = env.get(f"/api/firmware/{firmware.id}").headers["content-disposition"]
    assert disposition == 'attachment; filename="ev_il__filename__passwd"'
    assert disposition.count('"') == 2


def test_a_stored_path_that_escapes_the_firmware_dir_is_404(env: Env) -> None:
    """The value comes from our own database, and is still treated as untrusted.
    Same shape of check as `_resolve_image` in sources/database.py."""
    firmware = _seed_firmware(env)
    # The decoy has to be exactly as long as the row claims. Any other length and
    # the size check refuses it before the containment check is ever reached, so
    # the test would go green against a server that had no containment check at
    # all -- which is how it was first written, and what a mutation run caught.
    outside = env.tmp / "secret.bin"
    outside.write_bytes(b"x" * firmware.size_bytes)
    assert outside.is_file() and not outside.is_relative_to(settings.firmware_dir)

    # Backslashes because the stored value is normalised before it is joined, and
    # a leading slash because it is stripped rather than treated as absolute.
    for escape in ("../secret.bin", "..\\secret.bin", "/../secret.bin"):
        env.store.seed_firmware(replace(firmware, file_path=escape))
        assert env.get(f"/api/firmware/{firmware.id}").status_code == 404, escape


def test_an_image_not_assigned_to_this_device_is_404(env: Env) -> None:
    """Without this the staged rollout would be a property of the display
    response and not of the download. Pull a rollout back after it goes wrong on
    one device, and a board that had already been handed the URL would still
    fetch the withdrawn image -- it has neither Range nor resume, so a restart
    mid-download means starting over from whatever URL it remembered.
    """
    _seed_firmware(env, firmware_id="fw01")
    stranger = _seed_firmware(env, firmware_id="fw02", version="0.3.0", assign=False)

    assert env.get(f"/api/firmware/{stranger.id}").status_code == 404
    assert env.get("/api/firmware/does-not-exist").status_code == 404


def test_a_foreign_target_is_refused_at_the_download_too(env: Env) -> None:
    """The offer check is what a well-behaved board obeys; this one is what
    stops a hand-crafted request."""
    firmware = _seed_firmware(env, target="cyd")
    assert env.get(f"/api/firmware/{firmware.id}").status_code == 404


def test_a_half_written_upload_is_refused_before_the_radio_pays_for_it(env: Env) -> None:
    """The board verifies the digest itself, so this is not about safety: it is
    about not spending a wake and 1.35 MB of radio on bytes that cannot match."""
    firmware = _seed_firmware(env, payload=b"x" * 64, on_disk=b"x" * 60)
    assert env.get(f"/api/firmware/{firmware.id}").status_code == 404


# --------------------------------------------------------------------------
# /api/image
# --------------------------------------------------------------------------


def test_image_then_conditional_get_returns_304(env: Env) -> None:
    name = env.display()["filename"]

    first = env.get(f"/api/image/{name}")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    # Never cached anywhere but on the device: the path ends in .png and
    # Cloudflare caches by file extension, so an edge cache would sit in front
    # of every bearer check holding a picture with guests' names on it.
    assert first.headers["cache-control"] == "private, no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    etag = first.headers["etag"]
    assert etag == f'"{name[:-4]}"'
    assert first.content.startswith(b"\x89PNG")

    second = env.get(f"/api/image/{name}", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


@pytest.mark.parametrize("mangle", [lambda e: e.strip('"'), lambda e: f"W/{e}", lambda e: "*"])
def test_conditional_get_tolerates_mangled_validators(env: Env, mangle: Any) -> None:
    name = env.display()["filename"]
    etag = env.get(f"/api/image/{name}").headers["etag"]
    response = env.get(f"/api/image/{name}", headers={"If-None-Match": mangle(etag)})
    assert response.status_code == 304


def test_image_rejects_unknown_and_unsafe_names(env: Env) -> None:
    env.display()
    assert env.get("/api/image/deadbeef.png").status_code == 404
    assert env.get("/api/image/..%2F..%2Fsecret.png").status_code == 404
    assert main._frame_path("../../secret.png") is None


def test_a_frame_that_is_neither_held_nor_current_is_404(env: Env) -> None:
    """Authentication that does not bound *what* may be fetched is half of one.

    Every frame on disk carries guests' names, and the names are the only thing
    that changes between them. Without this rule an authenticated device could
    walk the last twelve simply by guessing ETags off its own history.
    """
    held = env.display()["filename"]
    stranger = _render_a_stray_frame(env, "Gewitter")
    newest = _render_a_stray_frame(env, "Nebel")

    assert stranger not in {held, newest}
    assert (settings.image_dir / stranger).is_file(), "the bytes really are there"
    assert env.get(f"/api/image/{stranger}").status_code == 404

    # The two it may have, so the 404 above is the rule and not an accident.
    assert env.get(f"/api/image/{held}").status_code == 200
    assert env.get(f"/api/image/{newest}").status_code == 200


def _render_a_stray_frame(env: Env, weather: str) -> str:
    """Render a frame through /preview and return its name.

    /preview publishes the server's current frame without touching any device
    row, which is the only way to get a frame onto disk that no board holds.
    """
    env.content["weather"] = weather
    assert env.client.get("/preview").status_code == 200
    frame = main.current_frame()
    assert frame is not None
    return frame.name


def test_the_fetch_stamp_is_written_on_200_and_on_304(env: Env) -> None:
    """A 304 counts as much as a 200 and this is not bookkeeping pedantry.

    The stamp feeds the 180 s panel-care floor, and 304 is the *common* case --
    the whole design aims at it. Recording only the 200 would leave the floor
    reading a paint stamp that is hours older than the last thing the board
    actually did.
    """
    name = env.display()["filename"]
    assert env.device.last_image_fetched_at is None

    env.clock.advance(60)
    assert env.get(f"/api/image/{name}").status_code == 200
    assert env.device.last_image_fetched_at == env.clock.now

    env.clock.advance(60)
    etag = f'"{name[:-4]}"'
    assert env.get(f"/api/image/{name}", headers={"If-None-Match": etag}).status_code == 304
    assert env.device.last_image_fetched_at == env.clock.now


def test_pruning_never_drops_the_frame_a_panel_is_holding(env: Env) -> None:
    painted = env.display()["filename"]

    # Far more previews than the retention limit, each a frame of its own.
    for tick in range(main.FRAME_RETENTION + 6):
        env.clock.advance(60)
        env.content["weather"] = f"Wetterlage {tick}"
        assert env.client.get("/preview").status_code == 200

    assert len(list(settings.image_dir.glob("*.png"))) <= main.FRAME_RETENTION + 1
    assert (settings.image_dir / painted).is_file()
    assert env.get(f"/api/image/{painted}").status_code == 200


def test_image_bytes_survive_a_lost_file(env: Env) -> None:
    """The in-memory frame answers even if the volume went away."""
    name = env.display()["filename"]
    (settings.image_dir / name).unlink()
    assert env.get(f"/api/image/{name}").status_code == 200


def test_the_image_check_precedes_the_304_shortcut(env: Env) -> None:
    """Otherwise the ETag becomes an oracle for which frames exist."""
    name = env.display()["filename"]
    etag = env.get(f"/api/image/{name}").headers["etag"]

    refused = env.client.get(f"/api/image/{name}", headers={"ID": MAC, "If-None-Match": etag})
    assert refused.status_code == 401, "a 304 here would confirm the frame exists"


# --------------------------------------------------------------------------
# /api/log, /healthz, /api/internal/status, /preview
# --------------------------------------------------------------------------


def test_log_returns_204_and_is_stored(env: Env) -> None:
    payload = {
        "log": {
            "message": "wifi connect failed",
            "level": "warn",
            "battery_voltage": 3.91,
            "wakeup_reason": "rtc",
        }
    }
    response = env.post("/api/log", json=payload)
    assert response.status_code == 204
    assert response.content == b""

    entry = env.store.recent_logs(1)[0]
    assert entry.device_id == DEVICE_ID
    assert entry.message == "wifi connect failed"
    assert entry.level == "warn"


def test_log_files_under_the_authenticated_id(env: Env) -> None:
    """Not under the ID header, even when the two disagree.

    Reading `ID` here let any authorised device file its lines under another
    device's name -- and the header is free text the caller sets.
    """
    env.post("/api/log", headers={"ID": "AA:BB:CC:DD:EE:99"}, json={"message": "not mine"})

    entry = env.store.recent_logs(1)[0]
    assert entry.device_id == DEVICE_ID
    assert entry.device_id != "AA:BB:CC:DD:EE:99"


def test_log_accepts_a_body_that_is_not_json(env: Env) -> None:
    response = env.post(
        "/api/log", content=b"panic: brownout", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 204
    assert "brownout" in env.store.recent_logs(1)[0].payload["body"]


def test_log_accepts_an_empty_body(env: Env) -> None:
    assert env.post("/api/log").status_code == 204


def test_healthz_says_nothing_but_alive(env: Env) -> None:
    """It used to publish the current frame's name, and that name IS the ETag IS
    the image path. Unguarded, that was the first half of a chain ending in a
    PNG with guests' names on it, fetched with no credential at all. The
    container health check only ever looked at the status code.
    """
    frame = main.current_frame()
    assert frame is not None, "warm-up rendered one at startup"

    response = env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert frame.key not in response.text
    assert frame.name not in response.text


def test_httpx_does_not_log_request_urls(env: Env) -> None:
    """A private calendar's ICS link carries its secret in the URL.

    httpx logs every request URL at INFO, so the running service must hold that
    logger at WARNING or the link lands in the container log on every wake.
    """
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_healthz_stays_open_for_the_container_probe(env: Env) -> None:
    """Docker's healthcheck has no key, and the body carries no content."""
    with override(admin_api_key="s3cret-key"):
        response = env.client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_internal_status_reports_the_failing_sources(env: Env) -> None:
    """What /healthz used to say, moved behind the internal key.

    This is the half of the deleted status page that was a real assurance: an
    outage has to be visible to whoever is looking after the thing, or the badge
    on the panel is the only place it ever shows up.
    """
    env.content["failures"] = ["weather", "events"]
    env.display()

    body = env.client.get("/api/internal/status").json()
    assert body["status"] == "ok"
    assert body["frame"] == main.current_frame().name  # type: ignore[union-attr]
    assert body["failures"] == ["weather", "events"]
    assert body["frame_age_s"] == 0
    assert isinstance(body["sources"], dict)


def test_the_internal_key_guards_the_endpoints_that_describe_a_frame(env: Env) -> None:
    with override(admin_api_key="s3cret-key"):
        assert env.client.get("/preview").status_code == 401
        assert env.client.get("/api/internal/status").status_code == 401

        for attempt in (
            {"headers": {"X-Admin-Key": "s3cret-key"}},
            {"headers": {"Authorization": "Bearer s3cret-key"}},
        ):
            assert env.client.get("/preview", **attempt).status_code == 200, attempt
            assert env.client.get("/api/internal/status", **attempt).status_code == 200, attempt

        assert env.client.get("/preview", headers={"X-Admin-Key": "wrong"}).status_code == 401


def test_the_query_parameter_is_no_longer_a_way_in(env: Env) -> None:
    """The old status page accepted ?token= so a frame could be opened in a
    browser without an extension. That convenience put a credential into every
    access log and every browser history, guarding a page that renders guests'
    names."""
    with override(admin_api_key="s3cret-key"):
        assert env.client.get("/preview", params={"token": "s3cret-key"}).status_code == 401
        assert env.client.get(
            "/api/internal/status", params={"token": "s3cret-key"}
        ).status_code == 401


def test_a_device_token_does_not_open_the_internal_endpoints(env: Env) -> None:
    """/preview renders on demand and answers with the pixels, so a board that
    could reach it would be a board that could pull any frame it liked."""
    with override(admin_api_key="s3cret-key"):
        assert env.get("/preview").status_code == 401
        assert env.get("/api/internal/status").status_code == 401


def test_preview_returns_a_png_of_panel_size(env: Env) -> None:
    from PIL import Image
    import io

    response = env.client.get("/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    image = Image.open(io.BytesIO(response.content))
    assert image.size == (settings.panel_width, settings.panel_height)


def test_preview_raw_is_the_packed_wire_format(env: Env) -> None:
    response = env.client.get("/preview?raw=1")
    assert response.status_code == 200
    assert len(response.content) == settings.panel_width * settings.panel_height // 2 == 120_000
    # Every nibble must be a real ink code; 0x4 and 0x7 are undefined (4.2).
    nibbles = {b >> 4 for b in response.content} | {b & 0x0F for b in response.content}
    assert nibbles <= {0x0, 0x1, 0x2, 0x3, 0x5, 0x6}


def test_the_probe_is_filtered_out_of_the_access_log() -> None:
    """Two mistakes are possible here and only one of them is loud.

    Dropping too little leaves the log unreadable. Dropping too much loses a
    board's wake, and nothing anywhere would say so - hence the record shape
    check rather than a substring match on the message.
    """
    log_filter = main._SkipHealthzAccessLog()

    def record(args: object) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
            args, None,
        )

    probe = ("127.0.0.1:52000", "GET", "/healthz", "1.1", 200)
    wake = ("127.0.0.1:52001", "GET", "/api/display", "1.1", 200)

    assert log_filter.filter(record(probe)) is False
    assert log_filter.filter(record(wake)) is True
    # A record we do not recognise passes rather than vanishes.
    assert log_filter.filter(record(None)) is True
    assert log_filter.filter(record(("only", "two"))) is True


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------


def test_both_access_token_spellings_are_read() -> None:
    underscore = main.parse_headers(Headers({"ACCESS_TOKEN": "abc"}))
    hyphen = main.parse_headers(Headers({"Access-Token": "abc"}))
    lower = main.parse_headers(Headers({"access_token": "abc"}))
    assert underscore.access_token == hyphen.access_token == lower.access_token == "abc"


def test_full_telemetry_header_set() -> None:
    parsed = main.parse_headers(
        Headers(
            {
                "ID": "aa:bb:cc:dd:ee:01",
                "BATTERY_VOLTAGE": "4.02",
                "PERCENT_CHARGED": "87.5",
                "BATTERY_CHARGING": "false",
                "RSSI": "-67",
                "FW_VERSION": "1.5.8",
                "WIDTH": "400",
                "HEIGHT": "600",
                "MODEL": "PaperColor",
                "REFRESH_RATE": "900",
                "WAKE_TIME": "1756100000",
                "TEMPERATURE": "22.4",
                "HUMIDITY": "48",
                "WAKE_REASON": "rtc_alarm",
            }
        )
    )
    telemetry = parsed.telemetry
    assert telemetry.device_id == MAC
    assert telemetry.battery_voltage_mv == 4020
    assert telemetry.battery_percent == 88  # 87.5 rounds to even
    assert telemetry.is_charging is False
    assert telemetry.rssi_dbm == -67
    assert telemetry.fw_version == "1.5.8"
    assert (telemetry.width, telemetry.height) == (400, 600)
    assert telemetry.temperature_c == 22.4
    assert telemetry.humidity_pct == 48.0
    assert telemetry.wake_reason == "rtc_alarm"
    assert parsed.model == "PaperColor"
    assert parsed.wake_time == "1756100000"
    assert parsed.device_refresh_rate == 900


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("4.02", 4020), ("4020", 4020), ("3.9V", 3900), ("3,95", 3950), ("junk", None), ("", None)],
)
def test_voltage_accepts_volts_and_millivolts(raw: str, expected: int | None) -> None:
    parsed = main.parse_headers(Headers({"BATTERY_VOLTAGE": raw} if raw else {}))
    assert parsed.telemetry.battery_voltage_mv == expected


def test_percent_is_clamped() -> None:
    assert main.parse_headers(Headers({"PERCENT_CHARGED": "127"})).telemetry.battery_percent == 100
    assert main.parse_headers(Headers({"PERCENT_CHARGED": "-3"})).telemetry.battery_percent == 0


def test_empty_headers_yield_all_none() -> None:
    telemetry = main.parse_headers(Headers({})).telemetry
    assert telemetry.device_id == ""
    assert telemetry.battery_voltage_mv is None
    assert telemetry.is_charging is None


# --------------------------------------------------------------------------
# The stored address
#
# `last_ip` is written but never read back into DeviceRecord, so nothing in an
# endpoint test can see it. These two are therefore the only place the cutting
# is checked at all -- and it is the one column in this service that holds
# somebody's personal data.
# --------------------------------------------------------------------------


def _request(headers: dict[str, str], client: str = "203.0.113.9") -> Any:
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (client, 51234),
        }
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.0.2.47", "192.0.2.0/24"),
        ("  192.0.2.47  ", "192.0.2.0/24"),
        ("192.0.2", None),
        ("2001:db8:1234:5678::1", "2001:db8:1234::/48"),
        # The naive version splits on ":" and keeps three groups, which for this
        # address stores interface bits as if they were the network.
        ("fd00::abcd:1", "fd00::/48"),
        ("not-an-address", None),
        ("", None),
        (None, None),
    ],
)
def test_the_address_is_cut_before_it_is_stored(raw: Optional[str], expected: Optional[str]) -> None:
    """/24 and /48.

    What arrives here is the tunnel's or the household's public address, never
    the board's address on the LAN, so storing it whole would be a dated,
    provider-resolvable record of somebody's connection for no diagnostic gain.
    What the column is good for is "which way is it coming in".
    """
    assert main._shorten_ip(raw) == expected


def test_the_client_address_prefers_the_proxy_headers() -> None:
    """Behind a proxy the socket address is the proxy's, not the caller's.

    `cf-connecting-ip` is what Cloudflare sets; `x-forwarded-for` is what
    every other reverse proxy sets.
    """
    assert main._client_ip(_request({"cf-connecting-ip": "192.0.2.47"})) == "192.0.2.0/24"
    assert (
        main._client_ip(_request({"x-forwarded-for": "192.0.2.47, 198.51.100.1"}))
        == "192.0.2.0/24"
    )
    # cf-connecting-ip wins: it is the one header the edge sets itself, while
    # anything upstream can append to the forwarded chain.
    assert (
        main._client_ip(
            _request({"cf-connecting-ip": "192.0.2.47", "x-forwarded-for": "198.51.100.3"})
        )
        == "192.0.2.0/24"
    )
    # No proxy header at all: the socket address, and still cut.
    assert main._client_ip(_request({}, client="198.51.100.3")) == "198.51.100.0/24"


# --------------------------------------------------------------------------
# assemble.build_dashboard
# --------------------------------------------------------------------------

WEATHER = Weather(temp_c=19.0, code=3, description="Bewoelkt", precip_prob_pct=20)
SUN = SunTimes(sunrise=time(6, 1), sunset=time(19, 58))
EVENT = Event(day=date(2026, 8, 26), start="20:00", title="Konzert", location="Mozarteum")
BOOKING = Booking(
    arrival=date(2026, 9, 1), departure=date(2026, 9, 3), nights=2, name="Gast", guests=2
)
SIGHTING = Sighting(
    captured_at=datetime(2026, 8, 24, 3, 12, tzinfo=TZ), caption="Ein Fuchs im Garten"
)

#: A wake that carried sensor readings. Without them the dashboard correctly
#: reports a "telemetry" failure, which would drown out what these tests are
#: actually about.
TELEMETRY = Telemetry(device_id=MAC, temperature_c=22.0, humidity_pct=45.0)


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(f"app.sources.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@contextmanager
def fake_sources(**modules: types.ModuleType) -> Iterator[None]:
    """Put stub source modules in sys.modules, then put the world back.

    The real modules live in the same tree; leaving a stub behind would break
    their tests, and importing theirs would put the network into ours.

    Installing inside the try, and remembering absence with a sentinel rather
    than with None, are both about the failure paths. assemble._load() reads
    sys.modules on every call, so a stub that survives its own test does not
    fail here - it fails in whatever test renders a frame next, in a file
    nobody would think to look at.
    """
    saved: dict[str, Any] = {}
    try:
        for short, module in modules.items():
            full = f"app.sources.{short}"
            saved[full] = sys.modules.get(full, _ABSENT)
            sys.modules[full] = module
        yield
    finally:
        for full, previous in saved.items():
            if previous is _ABSENT:
                sys.modules.pop(full, None)
            else:
                sys.modules[full] = previous


def default_modules(**overrides: types.ModuleType) -> dict[str, types.ModuleType]:
    modules = {
        "weather": _module("weather", fetch_weather=lambda *a: WEATHER),
        "sun": _module("sun", sun_times=lambda *a: SUN),
        "events": _module("events", collect_events=lambda **kw: ([EVENT], [])),
        "database": _module(
            "database",
            fetch_bookings=lambda *a, **kw: [BOOKING],
            fetch_latest_sighting=lambda *a, **kw: SIGHTING,
        ),
    }
    modules.update(overrides)
    return modules


@pytest.fixture()
def clean_cache() -> Iterator[None]:
    assemble.clear_cache()
    yield
    assemble.clear_cache()


def build(telemetry: Telemetry = TELEMETRY, now: datetime = T0) -> Dashboard:
    return assemble.build_dashboard(telemetry, now)


def test_assemble_collects_every_source(clean_cache: None) -> None:
    with override(sources_dsn="postgresql://reader@db.example.invalid/example"):
        with fake_sources(**default_modules()):
            dash = build()

    assert dash.failures == []
    assert dash.weather == WEATHER
    assert dash.sun == SUN
    assert dash.events == [EVENT]
    assert dash.bookings == [BOOKING]
    assert dash.sighting == SIGHTING
    assert dash.next_refresh == datetime(2026, 8, 25, 14, 50, tzinfo=TZ)
    assert dash.now == T0


def test_one_dead_source_does_not_take_the_frame(clean_cache: None) -> None:
    def boom(*args: Any) -> Any:
        raise ConnectionError("open-meteo is down")

    with override(sources_dsn="postgresql://x"):
        with fake_sources(**default_modules(weather=_module("weather", fetch_weather=boom))):
            dash = build()

    assert dash.failures == ["weather"]
    assert dash.weather is None
    assert dash.sun == SUN
    assert dash.events == [EVENT]
    assert dash.bookings == [BOOKING]


def test_a_device_request_gives_up_before_the_board_does(clean_cache: None) -> None:
    """The budget that binds is the board's, not ours.

    _source_budget_s() is thirty seconds at the default timeout; the firmware
    hangs up after device_http_timeout_s, calls the wake failed and does not
    come back until its next slot. The remaining twenty-two seconds buy a frame
    nobody receives. With the prewarm working none of this is reached - the
    cache is minutes old and nothing goes near the network - so this is about
    the morning the prewarm failed, where a frame with "ohne Termine" in the
    footer beats a whole slot of nothing.
    """
    release = threading.Event()

    # collect_events, not fetch_events: the stub has to carry the name
    # _events_job actually loads, or the module simply lacks it, the job fails
    # instantly, and this test is green in 5 ms without ever waiting for
    # anything - which is precisely what it did on the first attempt.
    def hangs(**kwargs: Any) -> Any:
        release.wait(30)
        return [EVENT], []

    with override(device_http_timeout_s=3.0):
        assert assemble.device_budget_s() < settings.device_http_timeout_s
        started = time_module.monotonic()
        try:
            with fake_sources(
                **default_modules(events=_module("events", collect_events=hangs))
            ):
                dash = assemble.build_dashboard(
                    TELEMETRY, T0, budget_s=assemble.device_budget_s()
                )
        finally:
            # In a finally, or the worker sits in the shared pool for thirty
            # seconds and starves whichever test runs next - a different one
            # every run, since the order is randomised.
            release.set()
        elapsed = time_module.monotonic() - started

    assert elapsed < 3.0, f"held the board's socket for {elapsed:.1f}s"
    # And it really did wait: an instant return would mean the stub was never
    # reached and the budget never tested.
    assert elapsed > 1.0, f"returned in {elapsed:.3f}s without exercising the budget"
    assert "events" in dash.failures
    # And everything that did answer is still on the frame.
    assert dash.weather == WEATHER
    assert dash.sun == SUN


def test_the_browser_keeps_the_generous_budget(clean_cache: None) -> None:
    """Only the device path is tightened. /preview and prewarm() are unhurried,
    and prewarm() is precisely the thing that keeps the tight path cheap."""
    with override(device_http_timeout_s=8.0, http_timeout_s=15.0):
        assert assemble.device_budget_s() < assemble._source_budget_s()


def test_missing_source_function_is_a_failure_not_a_crash(clean_cache: None) -> None:
    with fake_sources(**default_modules(weather=_module("weather"))):
        dash = build()
    assert "weather" in dash.failures
    assert dash.weather is None


def test_empty_dsn_disables_the_database_source_without_a_failure(clean_cache: None) -> None:
    with override(sources_dsn=""):
        with fake_sources(**default_modules()):
            dash = build()

    assert dash.failures == []
    assert dash.bookings == []
    assert dash.sighting is None


def test_sources_run_concurrently(clean_cache: None) -> None:
    def slow(value: Any):
        def call(*args: Any, **kwargs: Any) -> Any:
            time_module.sleep(0.30)
            return value

        return call

    modules = default_modules(
        weather=_module("weather", fetch_weather=slow(WEATHER)),
        sun=_module("sun", sun_times=slow(SUN)),
        events=_module("events", collect_events=slow(([EVENT], []))),
    )
    with fake_sources(**modules):
        started = time_module.monotonic()
        dash = build()
        elapsed = time_module.monotonic() - started

    assert dash.failures == []
    # Sequentially this is 0.9 s. The margin is wide on purpose: the assertion
    # is "they overlap", not "the machine is fast".
    assert elapsed < 0.60


def test_results_are_cached_between_wakes(clean_cache: None) -> None:
    calls: list[int] = []

    def counting(*args: Any) -> Weather:
        calls.append(1)
        return WEATHER

    with fake_sources(**default_modules(weather=_module("weather", fetch_weather=counting))):
        build()
        build(now=T0 + timedelta(minutes=5))

    assert len(calls) == 1


def test_a_stale_value_beats_an_empty_panel(clean_cache: None) -> None:
    def boom(*args: Any) -> Any:
        raise TimeoutError("no answer")

    with fake_sources(**default_modules()):
        build()

    # TTL expired and the refresh fails: show yesterday's reading, and say so.
    with override(source_cache_ttl_s=0):
        with fake_sources(**default_modules(weather=_module("weather", fetch_weather=boom))):
            dash = build(now=T0 + timedelta(hours=8))

    assert "weather" in dash.failures
    assert dash.weather == WEATHER


def test_a_hung_source_is_abandoned_within_the_budget(clean_cache: None) -> None:
    # The worker is released explicitly instead of being left in a sleep():
    # pool threads are joined at interpreter exit, so a hang that outlives its
    # own test holds the whole session open for the rest of the 30 s - which
    # looks like a slow suite, not like a test that failed to clean up.
    released = threading.Event()

    def hang(*args: Any, **kwargs: Any) -> Any:
        released.wait(30)

    with override(http_timeout_s=0.1):
        with fake_sources(**default_modules(events=_module("events", collect_events=hang))):
            started = time_module.monotonic()
            try:
                dash = build()
                elapsed = time_module.monotonic() - started
            finally:
                released.set()

    assert "events" in dash.failures
    assert dash.events == []
    assert elapsed < 8.0  # the floor is 5 s, not the source's 30


def test_wrong_types_from_a_source_are_dropped(clean_cache: None) -> None:
    """A source that returns junk must not poison the renderer's contract."""
    modules = default_modules(
        weather=_module("weather", fetch_weather=lambda *a: {"temp": 20}),
        events=_module("events", collect_events=lambda **kw: ([EVENT, "not an event", None], [])),
    )
    with fake_sources(**modules):
        dash = build()

    assert dash.weather is None
    assert "weather" in dash.failures
    assert dash.events == [EVENT]


# -- how the sources actually report trouble --------------------------------


def test_weather_returning_none_counts_as_an_outage(clean_cache: None) -> None:
    """fetch_weather answers None when Open-Meteo let us down; it never raises."""
    modules = default_modules(weather=_module("weather", fetch_weather=lambda *a: None))
    with fake_sources(**modules):
        dash = build()

    assert dash.weather is None
    assert dash.failures == ["weather"]


def test_the_database_source_reports_its_outage_through_the_failure_sink(clean_cache: None) -> None:
    """An unreachable database returns [], the same as "nothing is booked".

    Only the sink separates the two, so the caller has to offer it.
    """

    def bookings_down(dsn: str, limit: int, today: date, *, view: str = "", failures: Any = None) -> list[Booking]:
        if failures is not None:
            failures.append("bookings")
        return []

    def sighting_down(dsn: str, directory: Any, *, view: str = "", failures: Any = None) -> None:
        if failures is not None:
            failures.append("sighting")
        return None

    modules = default_modules(
        database=_module(
            "database", fetch_bookings=bookings_down, fetch_latest_sighting=sighting_down
        )
    )
    with override(sources_dsn="postgresql://x"):
        with fake_sources(**modules):
            dash = build()

    assert dash.bookings == []
    assert dash.sighting is None
    assert set(dash.failures) == {"bookings", "sighting"}


def test_an_empty_but_healthy_database_is_not_a_failure(clean_cache: None) -> None:
    def no_bookings(dsn: str, limit: int, today: date, *, view: str = "", failures: Any = None) -> list[Booking]:
        return []

    def no_sighting(dsn: str, directory: Any, *, view: str = "", failures: Any = None) -> None:
        return None

    modules = default_modules(
        database=_module("database", fetch_bookings=no_bookings, fetch_latest_sighting=no_sighting)
    )
    with override(sources_dsn="postgresql://x"):
        with fake_sources(**modules):
            dash = build()

    assert dash.failures == []


TWO_CALENDARS = ("https://calendar.example.org/a.ics", "https://calendar.example.org/b.ics")


def test_one_dead_calendar_is_logged_but_not_badged(clean_cache: None) -> None:
    """Half the events still fill the panel, and a badge under them would lie."""
    modules = default_modules(
        events=_module(
            "events",
            collect_events=lambda **kw: ([EVENT], ["calendar-1"]),
        )
    )
    with override(events_ics_urls=TWO_CALENDARS):
        with fake_sources(**modules):
            dash = build()

    assert dash.events == [EVENT]
    assert dash.failures == []


def test_both_calendars_down_is_badged(clean_cache: None) -> None:
    modules = default_modules(
        events=_module(
            "events",
            collect_events=lambda **kw: ([], ["calendar-1", "calendar-2"]),
        )
    )
    with override(events_ics_urls=TWO_CALENDARS):
        with fake_sources(**modules):
            dash = build()

    assert dash.events == []
    assert dash.failures == ["events"]


def test_every_calendar_served_from_cache_is_badged(clean_cache: None) -> None:
    """Events on the panel, but all of them out of the disk cache: say so.

    A panel that prints last week with complete confidence is worse than one
    that admits its age.
    """
    modules = default_modules(
        events=_module(
            "events",
            collect_events=lambda **kw: ([EVENT], ["calendar-1", "calendar-2"]),
        )
    )
    with override(events_ics_urls=TWO_CALENDARS):
        with fake_sources(**modules):
            dash = build()

    assert dash.events == [EVENT]
    assert dash.failures == ["events"]


def test_a_reported_failure_survives_the_cache(clean_cache: None) -> None:
    """The badge must not disappear at 15:00 for an outage that is still on."""
    calls: list[int] = []

    def bookings_down(dsn: str, limit: int, today: date, *, view: str = "", failures: Any = None) -> list[Booking]:
        calls.append(1)
        if failures is not None:
            failures.append("bookings")
        return []

    modules = default_modules(
        database=_module(
            "database", fetch_bookings=bookings_down, fetch_latest_sighting=lambda *a, **kw: None
        )
    )
    with override(sources_dsn="postgresql://x"):
        with fake_sources(**modules):
            build()
            dash = build(now=T0 + timedelta(minutes=5))

    # Asked again rather than served from cache, and that is deliberate: a
    # reported failure is never stored, so `stored_at` stays where it was and
    # the entry stays expired. What this test guards is the badge, and the
    # badge survives because the outage is still on - not because the failure
    # was cached. Caching it is what would blank a slot the prewarm warmed for.
    assert len(calls) == 2
    assert "bookings" in dash.failures


def test_a_reported_failure_does_not_evict_a_good_value(clean_cache: None) -> None:
    """The promise the `if reported:` block exists to keep."""
    calls: list[int] = []

    def flaky(dsn: str, limit: int, today: date, *, view: str = "", failures: Any = None) -> list[Booking]:
        calls.append(1)
        if len(calls) == 1:
            return [BOOKING]
        if failures is not None:
            failures.append("bookings")
        return []

    modules = default_modules(
        database=_module("database", fetch_bookings=flaky, fetch_latest_sighting=lambda *a, **kw: None)
    )
    with override(sources_dsn="postgresql://x", source_cache_ttl_s=0):
        with fake_sources(**modules):
            build()
            dash = build(now=T0 + timedelta(minutes=5))

    assert dash.bookings == [BOOKING]  # yesterday's reading, not nothing
    assert "bookings" in dash.failures


def test_a_failed_prewarm_does_not_blank_the_wake_it_warmed_for(clean_cache: None) -> None:
    """No predecessor is the normal case at the first slot of any day.

    `sun`, `events` and `bookings` key on the date, so at 06:55 no entry for
    today has ever existed. Storing the blip would hand the 07:00 wake a
    *fresh* empty entry - valid for the full TTL, so it never asks again, and
    the frame goes out without bookings until 15:00. Without prewarming the
    same blip was harmless, because nobody asked at 06:55.
    """
    calls: list[int] = []

    def blips_once(dsn: str, limit: int, today: date, *, view: str = "", failures: Any = None) -> list[Booking]:
        calls.append(1)
        if len(calls) == 1:
            if failures is not None:
                failures.append("bookings")
            return []
        return [BOOKING]

    modules = default_modules(
        database=_module("database", fetch_bookings=blips_once, fetch_latest_sighting=lambda *a, **kw: None)
    )
    with override(sources_dsn="postgresql://x"):
        with fake_sources(**modules):
            build()  # the 06:55 prewarm, upstream blips
            dash = build(now=T0 + timedelta(minutes=5))  # the 07:00 wake

    assert len(calls) == 2  # the wake retried instead of trusting the blip
    assert dash.bookings == [BOOKING]
    assert "bookings" not in dash.failures


def test_missing_sensor_readings_are_reported(clean_cache: None) -> None:
    with fake_sources(**default_modules()):
        dash = build(telemetry=Telemetry(device_id=MAC))
    assert dash.failures == ["telemetry"]


def test_failure_names_are_the_names_the_renderer_knows() -> None:
    """Dashboard.failures is a shared vocabulary, not free text."""
    layout = pytest.importorskip("app.render.layout")
    labels = getattr(layout, "FAILURE_LABELS", None)
    if not isinstance(labels, dict):
        pytest.skip("layout exposes no FAILURE_LABELS")

    known = {
        assemble.SOURCE_BOOKINGS,
        assemble.SOURCE_EVENTS,
        assemble.SOURCE_SIGHTING,
        assemble.SOURCE_SUN,
        assemble.SOURCE_TELEMETRY,
        assemble.SOURCE_WEATHER,
    }
    assert known <= set(labels), f"renderer cannot label {known - set(labels)}"


# --------------------------------------------------------------------------
# Source prewarming
# --------------------------------------------------------------------------
#
# The point of this task is battery, not freshness: the board holds its radio
# up at ~120 mA for every second the server spends scraping upstreams, so a
# round trip paid in the background is a round trip the cell does not pay.
# That only holds if the task actually refreshes (not merely "runs"), and it
# is only safe if it stays a cache filler - the moment it publishes frames or
# writes to the store it can move what is on the glass, and worse, the state
# the panel-care gates read.


def _stub_sources(mp: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Replace every source job with something instant and offline.

    Deliberately at the job layer rather than at build_dashboard: the prewarm
    goes through assemble.prewarm(), and stubbing one layer up would test the
    stub instead of the cache behaviour that matters here.
    """

    def plan(now_local: datetime) -> list[Any]:
        def job() -> tuple[Any, list[str]]:
            calls.append("weather")
            return "sonnig", []

        return [assemble._Job(assemble.SOURCE_WEATHER, "weather", job, None)]

    mp.setattr(assemble, "_plan", plan)


def test_prewarm_refreshes_even_a_still_fresh_entry(tmp_path: Path) -> None:
    """The whole feature turns on this.

    build_dashboard skips any job whose cache entry is still inside the TTL,
    so a prewarm routed through it could only ever refill a hole it happened
    to land in - at an interval of half the TTL, two runs in three would be
    no-ops and the cache would still be cold a third of the time. prewarm()
    passes a TTL of zero for exactly that reason.
    """
    calls: list[str] = []
    with override(data_dir=tmp_path, source_cache_ttl_s=3600):
        # Also unlatches the pool: a preceding test may have ended a lifespan.
        main.reset_state()
        try:
            with pytest.MonkeyPatch.context() as mp:
                _stub_sources(mp, calls)

                assemble.prewarm(T0)
                assert calls == ["weather"]
                assert "weather" in assemble.cache_state()

                # The entry is seconds old against an hour of TTL, so
                # build_dashboard must reuse it...
                assemble.build_dashboard(Telemetry(device_id=""), T0)
                assert calls == ["weather"], "build_dashboard should have used the cache"

                # ...and prewarm must not.
                assemble.prewarm(T0)
                assert calls == ["weather", "weather"], "prewarm did not refresh"
        finally:
            assemble.clear_cache()


def test_prewarming_publishes_no_frame(env: Env) -> None:
    before = main.current_frame()
    # The env fixture stubs build_dashboard, which is exactly the layer
    # prewarm bypasses - without stubbing the job plan too this test would
    # reach the real upstreams, against this file's opening promise.
    with pytest.MonkeyPatch.context() as mp:
        _stub_sources(mp, [])
        main._prewarm_sources()
    # Whatever is on the glass is the device's business. A background task that
    # moved _frame would also write a PNG and run the pruner, and the pruner
    # only protects frames a device is already holding.
    assert main.current_frame() is before


def test_prewarming_never_touches_the_device_store(env: Env) -> None:
    """It must not be able to weaken a panel-care gate, even by accident.

    The 180 s gate reads painted_or_fetched_at and the 15 C gate reads the
    persisted telemetry. Both live in the store, so the behavioural half is that
    the store is exactly as it was - plus the structural reason it cannot be
    otherwise, because the behavioural half would also pass against a source
    layer that simply had nothing to say.
    """
    env.display(TEMPERATURE="21.5")
    before = env.device

    # Stubbed for the same reason as above, and here it is load-bearing: with
    # real sources every failure is swallowed by _collect, so an offline run
    # would do nothing at all and the assertions below would pass vacuously.
    with pytest.MonkeyPatch.context() as mp:
        _stub_sources(mp, [])
        main._prewarm_sources()

    after = env.device
    assert after.last_painted_at == before.last_painted_at
    assert after.last_image_fetched_at == before.last_image_fetched_at
    assert after.last_etag == before.last_etag
    assert after.temperature_c == before.temperature_c

    # The structural guarantee that _prewarm_sources leans on in its docstring.
    source = Path(assemble.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*from\s+\.store\s+import|^\s*from\s+\.\s+import\s+.*\bstore\b|"
        r"^\s*import\s+.*\bstore\b",
        source,
        re.MULTILINE,
    ), "assemble must not be able to reach the store"


def test_a_failing_prewarm_does_not_stop_the_loop() -> None:
    """A cold cache is the fallback, so a raising source is a non-event.

    If one exception could end the loop, a single upstream hiccup would
    silently disable the saving for the rest of the container's life.

    Deliberately without the `env` fixture. `asyncio.sleep` is patched
    process-wide below, and env keeps a TestClient portal thread alive with a
    real _prewarm_loop task of its own in another event loop -- there is no
    reason to reach across threads when this test needs nothing from the app.
    """
    attempts = 0
    real_sleep = asyncio.sleep

    def explode() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("upstream said no")

    async def drive() -> None:
        async def no_wait(delay: float = 0, *a: Any, **kw: Any) -> Any:
            # The loop waits for the next slot and backs off a minute after a
            # failure; without collapsing those this test would take hours.
            return await real_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "_prewarm_sources", explode)
            mp.setattr(asyncio, "sleep", no_wait)
            task = asyncio.create_task(main._prewarm_loop(300))
            loop = asyncio.get_running_loop()
            # A deadline, not a bare spin: if the loop dies on the first
            # exception the counter stops, and an unbounded wait would hang the
            # whole suite at a random point instead of failing here.
            deadline = loop.time() + 5.0
            while attempts < 3 and loop.time() < deadline:
                await real_sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(drive())
    assert attempts >= 3, "the loop stopped at the first failure"


def test_prewarm_waits_until_shortly_before_the_next_slot() -> None:
    """The lead time is the whole reason this is cheap.

    A fixed interval short enough to beat SOURCE_CACHE_TTL_S would mean dozens
    of daily fetches against an undocumented endpoint; firing once per slot
    means three. So the schedule itself is worth pinning: warm `lead` seconds
    before the slot, then sleep past the slot rather than re-warming for the
    whole lead window.

    Own clock rather than the `env` fixture, for the same reason as the test
    above: nothing here needs the app, and patching asyncio.sleep while another
    thread runs a live loop is a reach across threads for no gain.

    `_prewarm_slots` is pinned rather than left to whatever `main._store` holds
    at this point. Without the fixture there is no store of this test's own,
    and with pytest-randomly the leftover is a different one every run - which
    stayed invisible only while the rows and `settings` happened to agree on
    the same three times. This test is about the lead time; which slots it
    aims at has tests of its own.
    """
    lead = 300
    clock = Clock(T0)
    slot = next_slot(clock.now, settings.refresh_slots, TZ)
    gap = (slot - clock.now).total_seconds()

    waits: list[float] = []
    warmed = 0
    real_sleep = asyncio.sleep

    def count() -> None:
        nonlocal warmed
        warmed += 1

    async def drive() -> None:
        async def record(delay: float = 0, *a: Any, **kw: Any) -> Any:
            waits.append(delay)
            return await real_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "_now", clock)
            mp.setattr(main, "_prewarm_sources", count)
            mp.setattr(main, "_prewarm_slots", lambda: settings.refresh_slots)
            mp.setattr(asyncio, "sleep", record)
            task = asyncio.create_task(main._prewarm_loop(lead))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while len(waits) < 2 and loop.time() < deadline:
                await real_sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(drive())

    assert warmed >= 1, "the prewarm never ran"
    assert waits, "the loop did not wait at all"
    # First wait: right up to `lead` seconds before the slot.
    assert waits[0] == pytest.approx(gap - lead, abs=1.0), (
        f"expected to wake {lead} s before the slot ({gap - lead:.0f} s from now), "
        f"waited {waits[0]:.0f} s"
    )
    # Second wait: past the slot, so next_slot advances instead of returning
    # the same one and warming in a tight circle for the whole lead window.
    assert len(waits) >= 2 and waits[1] > 0


def test_prewarm_is_cancelled_before_the_pool_is_shut_down(tmp_path: Path) -> None:
    """Ordering, asserted where it is actually observable.

    Cancelling does not stop a prewarm already inside its worker thread -
    asyncio.to_thread is not cancellable - so what contains that thread is
    shutdown() latching the pool closed. The task must therefore be cancelled
    before shutdown() runs, and nothing about task.done() at the end of the
    lifespan can show that: asyncio.run cancels every pending task on its way
    out anyway, so the naive assertion passes even with the cancel removed.
    """
    seen: dict[str, Any] = {}
    real_shutdown = assemble.shutdown

    def spy_shutdown() -> None:
        task = main._prewarm_task_for_test
        seen["cancelled_before_shutdown"] = task is not None and task.cancelled()
        real_shutdown()

    with override(data_dir=tmp_path, warm_on_start=False, prewarm_lead_s=300):
        main.reset_state()
        # The lifespan calls get_store(); without this it would build a real
        # Store in tmp_path and migrate it, which is not what this test is about.
        main.set_store(InMemoryStore())
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(assemble, "build_dashboard", dashboard_factory({"weather": "x"}))
            mp.setattr(assemble, "shutdown", spy_shutdown)
            with TestClient(main.app):
                assert main._prewarm_task_for_test is not None
        main.reset_state()

    assert seen.get("cancelled_before_shutdown") is True


def test_prewarm_can_be_switched_off(tmp_path: Path) -> None:
    called = 0

    def spy(telemetry: Telemetry, now: datetime, **_: Any) -> Dashboard:
        nonlocal called
        called += 1
        return dashboard_factory({"weather": "x"})(telemetry, now)

    with override(data_dir=tmp_path, prewarm_lead_s=0, warm_on_start=False):
        main.reset_state()
        main.set_store(InMemoryStore())
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(assemble, "build_dashboard", spy)
            with TestClient(main.app):
                assert main._prewarm_task_for_test is None
        main.reset_state()

    assert called == 0


def test_the_pool_refuses_work_once_shut_down(tmp_path: Path) -> None:
    """An abandoned worker must not resurrect the executor.

    Without the latch it would build a fresh ThreadPoolExecutor whose
    non-daemon threads concurrent.futures joins at interpreter exit - holding
    the container open for exactly as long as the hung upstream that
    shutdown(wait=False) exists to escape.
    """
    with override(data_dir=tmp_path):
        assemble.shutdown()
        try:
            with pytest.raises(assemble.ShuttingDown):
                assemble._pool()
        finally:
            assemble.reopen()
        assert assemble._pool() is not None


# --------------------------------------------------------------------------
# The seam between the cached window and the time-of-day cut
# --------------------------------------------------------------------------


def _window_module(window: list[Event], calls: list[int]) -> types.ModuleType:
    """An events source that counts its network round trips.

    Carries the REAL trim_events: the point of these tests is where the cut
    runs, not what it does, and a stubbed cut could not tell the two apart.
    """
    from app.sources.events import trim_events

    def collect(**_: Any) -> tuple[list[Event], list[str]]:
        calls.append(1)
        return list(window), []

    return _module("events", collect_events=collect, trim_events=trim_events)


MORNING_EVENT = Event(day=date(2026, 8, 25), start="07:00", title="Kapitelmarkt")
EVENING_EVENT = Event(day=date(2026, 8, 25), start="20:00", title="Die Zauberflöte")


def test_two_builds_from_one_cached_window_print_different_days(clean_cache: None) -> None:
    """The cut belongs to the frame, not to the cached value.

    Baked into the cache, the 14:50 frame would print whatever was still ahead
    when the cache was filled - up to SOURCE_CACHE_TTL_S = 1 h wrong, and
    wrong in the invisible direction: a panel confidently listing a market
    that closed hours ago.
    """
    calls: list[int] = []
    with fake_sources(**default_modules(events=_window_module([MORNING_EVENT, EVENING_EVENT], calls))):
        morning = build(now=datetime(2026, 8, 25, 6, 50, tzinfo=TZ))
        afternoon = build(now=datetime(2026, 8, 25, 14, 50, tzinfo=TZ))

    assert len(calls) == 1, "the second build re-fetched instead of reading the cache"
    assert [e.title for e in morning.events] == ["Kapitelmarkt", "Die Zauberflöte"]
    assert [e.title for e in afternoon.events] == ["Die Zauberflöte"]


def test_the_wake_reads_what_the_prewarm_fetched_and_still_cuts_correctly(
    clean_cache: None,
) -> None:
    """The whole reason the module comes in two halves.

    The prewarm runs PREWARM_LEAD_S before the slot so the board never holds
    its radio up at ~120 mA waiting for a calendar feed. A cut that lived inside the
    cached value would make the wake either miss the entry - and pay for the
    round trip the prewarm was meant to save - or print the prewarm's clock.
    """
    calls: list[int] = []
    with fake_sources(**default_modules(events=_window_module([MORNING_EVENT, EVENING_EVENT], calls))):
        assemble.prewarm(datetime(2026, 8, 25, 14, 45, tzinfo=TZ))
        wake = build(now=datetime(2026, 8, 25, 14, 50, tzinfo=TZ))

    assert len(calls) == 1, "the wake paid for a round trip the prewarm had already made"
    assert [e.title for e in wake.events] == ["Die Zauberflöte"]


def test_a_source_without_trim_events_costs_the_cut_not_the_frame(clean_cache: None) -> None:
    """`_load` raises when the attribute is missing, and build_dashboard must
    not. Every source stub in this file is that case."""
    with fake_sources(**default_modules()):
        dash = build()

    assert dash.events == [EVENT]
    assert dash.failures == []


def test_the_printed_next_refresh_comes_from_the_device_row(clean_cache: None) -> None:
    """Same list as the countdown, or the panel names a time it will not wake at."""
    with fake_sources(**default_modules()):
        dash = assemble.build_dashboard(TELEMETRY, T0, slots=((6, 0), (18, 0)))

    assert dash.next_refresh == datetime(2026, 8, 25, 18, 0, tzinfo=TZ)


# --------------------------------------------------------------------------
# Which slots the prewarm aims at
# --------------------------------------------------------------------------


def test_the_prewarm_aims_at_the_wake_times_in_the_rows(env: Env) -> None:
    """It used to read `settings`, while the boards read their row.

    Both held the same three times, so nothing showed - until the operator
    moved a wake time with `device set`, which is exactly where that decision
    belongs. From then on the prewarm fires at times no board wakes at: every
    wake back to a full round trip with the radio up, and nothing anywhere
    turning red.
    """
    env.set_device(slots=(5 * 60, 17 * 60))

    assert main._prewarm_slots() == ((5, 0), (17, 0))


def test_the_prewarm_covers_every_active_device(env: Env) -> None:
    """The union, because the cache is one per process: warming early enough
    for the earliest waker covers the others for free."""
    env.set_device(slots=(6 * 60 + 50,))
    env.store.seed_device(
        make_device("second", token_hash="x", mac="AA:BB:CC:DD:EE:FF", slots=(9 * 60,))
    )

    assert main._prewarm_slots() == ((6, 50), (9, 0))


def test_an_inactive_device_does_not_pull_the_prewarm_around(env: Env) -> None:
    """`list_devices` carries no WHERE - `device list` wants those rows too."""
    env.set_device(slots=(6 * 60 + 50,))
    env.store.seed_device(
        make_device(
            "retired", token_hash="x", mac="AA:BB:CC:DD:EE:00", slots=(3 * 60,), active=False
        )
    )

    assert main._prewarm_slots() == ((6, 50),)


def test_the_prewarm_falls_back_to_the_configured_slots_when_the_store_is_down(
    env: Env,
) -> None:
    """A cold cache is a worse answer than a slightly misaimed warm one."""
    _swap_store(env, _WritesFail())

    assert main._prewarm_slots() == settings.refresh_slots


def test_the_prewarm_falls_back_when_no_device_has_a_row(env: Env) -> None:
    """A fresh installation. An empty slot list would make next_slot raise and
    the loop sleep an hour at a time - the prewarm silently off."""
    env.store.devices.clear()

    assert main._prewarm_slots() == settings.refresh_slots


def test_the_prewarm_loop_aims_at_the_rows_not_at_the_configuration() -> None:
    """The helper is only worth having if the loop actually asks it.

    Written after the obvious version of these tests failed a mutation probe:
    with `_prewarm_slots` tested on its own, putting `settings.refresh_slots`
    back into the loop left the whole file green. This one drives the loop and
    measures where it aims.

    11:00 is in no configuration anywhere, so the wait can only come from the
    row: 09:13 -> 11:00 is 1 h 47 min, while the configured slots would give
    5 h 37 min.
    """
    lead = 300
    clock = Clock(T0)
    store = InMemoryStore()
    seed_device_row(store, slots=(11 * 60,))
    gap = (datetime(2026, 8, 25, 11, 0, tzinfo=TZ) - T0).total_seconds()

    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def drive() -> None:
        async def record(delay: float = 0, *a: Any, **kw: Any) -> Any:
            waits.append(delay)
            return await real_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "_now", clock)
            mp.setattr(main, "_prewarm_sources", lambda: None)
            mp.setattr(main, "get_store", lambda: store)
            mp.setattr(asyncio, "sleep", record)
            task = asyncio.create_task(main._prewarm_loop(lead))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while not waits and loop.time() < deadline:
                await real_sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(drive())

    assert waits, "the loop did not wait at all"
    assert waits[0] == pytest.approx(gap - lead, abs=1.0), (
        f"expected to aim at the row's 11:00 slot ({gap - lead:.0f} s from now), "
        f"waited {waits[0]:.0f} s"
    )


def test_an_events_module_without_collect_events_is_badged_not_fatal(
    clean_cache: None,
) -> None:
    """The `fetch_events` rungs are gone, and this is what replaced them.

    They trimmed internally and build_dashboard trimmed again; after the first
    collapse a run is one row on its earliest day, so the second pass could no
    longer see it as a run and simply cut it - a five-day series vanished
    instead of moving to tomorrow. A module that only offers the old names now
    takes the ordinary source-failure path: one badged panel, no stack trace.
    """
    with fake_sources(
        **default_modules(events=_module("events", fetch_events=lambda *a: [EVENT]))
    ):
        dash = build()

    assert "events" in dash.failures
    assert dash.events == []


def test_the_prewarm_reads_the_slots_off_the_event_loop() -> None:
    """`_prewarm_slots` opens a Postgres connection, and psycopg is synchronous.

    On the loop, a slow database would stall every request for up to the
    connect plus statement timeout - and this loop wakes about 30 s after each
    slot, exactly while the boards are polling against an 8 s header budget.
    Before the slots moved into the rows the same line was an attribute read
    and could not block, so nothing here used to be worth testing.

    Measured, not asserted structurally: a ticker counts how often the loop
    gets to run while the slot lookup takes 250 ms. Off the loop it spins
    freely; on the loop it would get essentially nothing.
    """
    clock = Clock(T0)

    def slow_slots() -> tuple[tuple[int, int], ...]:
        time_module.sleep(0.25)
        return settings.refresh_slots

    ticks = 0
    real_sleep = asyncio.sleep

    async def drive() -> None:
        nonlocal ticks

        async def record(delay: float = 0, *a: Any, **kw: Any) -> Any:
            return await real_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "_now", clock)
            mp.setattr(main, "_prewarm_slots", slow_slots)
            mp.setattr(main, "_prewarm_sources", lambda: None)
            mp.setattr(asyncio, "sleep", record)
            task = asyncio.create_task(main._prewarm_loop(300))
            started = time_module.monotonic()
            while time_module.monotonic() - started < 0.20:
                await real_sleep(0)
                ticks += 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(drive())

    assert ticks > 100, (
        f"the event loop only ran {ticks} times in 200 ms while the slot "
        "lookup was blocking - it is being called synchronously"
    )
