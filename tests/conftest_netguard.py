"""Fail loudly if a test opens a socket to the outside world.

Not wired into conftest.py: run it with `-p tests.conftest_netguard` to check
the claim in test_server.py's docstring ("No test touches the network"). It is
opt-in because blocking sockets globally would also break anything that binds
a loopback port, and the point here is evidence, not enforcement.
"""

from __future__ import annotations

import socket

import pytest

_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo

OUTBOUND: list[str] = []


def _local(host: object) -> bool:
    return str(host) in {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def _forbid_outbound(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    def guard_connect(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not _local(host):
            OUTBOUND.append(f"{request.node.nodeid} -> {host}")
            raise AssertionError(f"{request.node.nodeid} opened a connection to {host}")
        return _real_create_connection(address, *args, **kwargs)

    def guard_getaddrinfo(host, *args, **kwargs):
        if not _local(host):
            OUTBOUND.append(f"{request.node.nodeid} -> resolve {host}")
            raise AssertionError(f"{request.node.nodeid} resolved {host}")
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guard_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guard_getaddrinfo)
    yield
