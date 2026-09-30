"""Device authentication: bearer token `<id>.<secret>`, only the hash stored.

What an earlier version of this service did is worth writing down, because it
looked like authentication and was not. `_authorise()` used to hang off `device.paired_at`,
and a MAC the server had never seen was **let through**: the reasoning was that
demanding a token from a row the board was never told about would lock out the
real device. That reasoning was sound while the server minted keys itself, and
it evaporates the moment the operator creates the row and hands over the token
(`python -m app.cli device add`).
What was left behind was an open door -- and, chained with the frame name that
`/healthz` used to publish, a way for anyone to fetch a PNG carrying guests'
names off a public host.

The id sits in front of the dot so the lookup is a primary-key hit rather than a
full-table hash comparison, and so a malformed token can be rejected before
anything reaches the database.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from .store import DbUnavailable, DeviceRecord, hash_device_token

#: Hex, and bounded. Checked before the value goes anywhere near a query --
#: not because the driver would interpolate it (it will not), but because a
#: 4 kB "id" is a firmware bug or a probe, and either way the answer is 401
#: without a round trip.
_ID_RE = re.compile(r"^[a-fA-F0-9]+$")
_ID_MAX = 64

#: Compared against when the id is unknown, so the work done -- and therefore
#: the time taken -- does not depend on whether the id exists.
_DUMMY_HASH = "0" * 64


class Reason(str, Enum):
    """Why a request was refused. Three values, not two, and it matters.

    `DISABLED` is answered with 403 rather than 401 because the two mean
    opposite things to the firmware: 401 says "your identity is wrong, ask a
    human to re-provision you", 403 says "you are who you say you are, and the
    operator has switched you off". A device disabled with `device set
    --disable` must not walk into a provisioning loop over it.
    """

    NO_TOKEN = "no_token"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


@dataclass(frozen=True)
class AuthResult:
    device: Optional[DeviceRecord]
    reason: Optional[Reason]

    @property
    def ok(self) -> bool:
        return self.device is not None and self.reason is None


class _Lookup(Protocol):
    def get_device(self, device_id: str) -> Optional[DeviceRecord]: ...


def parse_device_token_id(token: str) -> Optional[str]:
    """Pull the id out of a token without trusting it.

    Only the part before the FIRST dot. The secret is base64url and carries no
    dot of its own, but a manipulated token must not be able to change that.
    """
    raw = (token or "").strip()
    dot = raw.find(".")
    if dot <= 0:
        return None
    ident = raw[:dot]
    if len(ident) > _ID_MAX or not _ID_RE.match(ident):
        return None
    return ident


def bearer_from_headers(headers: object) -> str:
    """Accept `Authorization: Bearer <token>` and the legacy `ACCESS_TOKEN`.

    Both are read, permanently, and there is no switch between them. The board
    sends the same secret over the same TLS connection either way, so neither
    header is weaker than the other -- and a configuration flag would be a third
    thing to set wrongly on hardware whose BOOT button sits behind a wall mount.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return ""
    raw = str(get("Authorization") or get("authorization") or "").strip()
    if raw:
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        # A bare token without the scheme. Tolerated: it costs one branch, and
        # the alternative is a device that is silently unauthenticated because
        # somebody pasted the value without the word in front of it.
        return raw
    for name in ("ACCESS_TOKEN", "Access-Token", "access_token"):
        value = str(get(name) or "").strip()
        if value:
            return value
    return ""


def authenticate(lookup: _Lookup, offered: Optional[str]) -> AuthResult:
    """Resolve a token to a device, or say why not.

    Raises `DbUnavailable` rather than swallowing it. The caller answers that
    with 503; treating a database outage as "unknown token" would tell the
    firmware its identity is broken and, once the portal ladder is in place,
    would send a working device into setup mode over a restart of Postgres.
    """
    token = (offered or "").strip()
    if not token:
        return AuthResult(None, Reason.NO_TOKEN)

    ident = parse_device_token_id(token)
    if ident is None:
        return AuthResult(None, Reason.NO_TOKEN)

    device = lookup.get_device(ident)  # may raise DbUnavailable -> 503

    expected = device.token_hash if device is not None else _DUMMY_HASH
    matches = hmac.compare_digest(hash_device_token(token), expected)

    if device is None or not matches:
        return AuthResult(None, Reason.UNKNOWN)
    if not device.active:
        return AuthResult(device, Reason.DISABLED)
    return AuthResult(device, None)


__all__ = [
    "AuthResult",
    "DbUnavailable",
    "Reason",
    "authenticate",
    "bearer_from_headers",
    "parse_device_token_id",
]
