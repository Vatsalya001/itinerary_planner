"""client_ip: right-most X-Forwarded-For hop behind one trusted appending proxy, else the peer address."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.api.security import client_ip

PEER = "10.1.2.3"


def _request(*xff: str, peer: str | None = PEER) -> Request:
    headers = [(b"x-forwarded-for", v.encode()) for v in xff]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers,
             "client": (peer, 5555) if peer else None}
    return Request(scope)


def test_spoofed_leading_hops_are_ignored() -> None:
    assert client_ip(_request("6.6.6.6, 7.7.7.7, 203.0.113.9"), True) == "203.0.113.9"
    assert client_ip(_request("6.6.6.6,203.0.113.9"), True) == "203.0.113.9"


def test_single_hop() -> None:
    assert client_ip(_request("203.0.113.9"), True) == "203.0.113.9"
    assert client_ip(_request(" 2001:db8::1 "), True) == "2001:db8::1"
    assert client_ip(_request("203.0.113.9:51234"), True) == "203.0.113.9"
    assert client_ip(_request("[2001:db8::1]:443"), True) == "2001:db8::1"


def test_repeated_headers_use_the_last_value() -> None:
    assert client_ip(_request("6.6.6.6", "203.0.113.9"), True) == "203.0.113.9"


@pytest.mark.parametrize("header", ["not-an-ip", "203.0.113.9, garbage", "203.0.113.9,", "", " , ",
                                    "999.1.1.1", "203.0.113.9, <script>"])
def test_malformed_header_falls_back_to_peer(header: str) -> None:
    assert client_ip(_request(header), True) == PEER


def test_absent_header_uses_peer() -> None:
    assert client_ip(_request(), True) == PEER
    assert client_ip(_request(peer=None), True) == "unknown"


def test_header_ignored_when_not_trusted() -> None:
    assert client_ip(_request("203.0.113.9"), False) == PEER
    assert client_ip(_request("6.6.6.6, 203.0.113.9"), False) == PEER
