"""client_ip: right-most X-Forwarded-For hop behind one trusted appending proxy, else the peer address."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from app.api.security import client_ip, rate_limit_subject

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


def test_rate_limit_subject_groups_ipv6_by_64() -> None:
    a = rate_limit_subject("2001:db8:aaaa:bbbb:1:2:3:4")
    b = rate_limit_subject("2001:db8:aaaa:bbbb:ffff:ffff:ffff:ffff")
    assert a == b == "2001:db8:aaaa:bbbb::/64"
    assert rate_limit_subject("2001:db8:aaaa:bbbc::1") != a  # a different /64
    assert rate_limit_subject("203.0.113.9") == "203.0.113.9"
    assert rate_limit_subject("::ffff:203.0.113.9") == "203.0.113.9"  # IPv4-mapped
    assert rate_limit_subject("unknown") == "unknown"


async def test_ipv6_clients_in_one_64_share_a_plan_bucket(make_client: Any, fake_runner: Any,
                                                          clocked_store: Any) -> None:
    brief = {"city": "Bangalore"}
    async with make_client(store=clocked_store, rate_limit_per_10min=1, trust_proxy_headers=True) as (client, _a, _d):
        r1 = await client.post("/api/plan", json=brief, headers={"X-Forwarded-For": "2001:db8:1:2::10"})
        r2 = await client.post("/api/plan", json=brief, headers={"X-Forwarded-For": "2001:db8:1:2:ffff::99"})
        r3 = await client.post("/api/plan", json=brief, headers={"X-Forwarded-For": "2001:db8:1:3::10"})
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 429, 200)
