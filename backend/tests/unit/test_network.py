import pytest
from starlette.requests import Request

from app.api.client import client_ip
from app.core.network import ip_prefix, parse_ip


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ("203.0.113.77", "203.0.113.0/24"),
        ("2001:db8:abcd:12::1", "2001:db8:abcd::/48"),
        ("::ffff:198.51.100.9", "198.51.100.0/24"),
        (" 203.0.113.77 ", "203.0.113.0/24"),
        ("not-an-ip", None),
        ("", None),
        (None, None),
    ],
)
def test_ip_prefix_keeps_only_the_network(value: str | None, prefix: str | None) -> None:
    assert ip_prefix(value) == prefix


def test_parse_ip_rejects_host_names_and_ports() -> None:
    assert parse_ip("example.com") is None
    assert parse_ip("203.0.113.7:443") is None
    assert parse_ip("203.0.113.7") == "203.0.113.7"


def request_from(peer: str | None, *forwarded_for: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded_for]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_without_trusted_proxies_the_peer_is_the_client() -> None:
    request = request_from("198.51.100.1", "203.0.113.9")

    assert client_ip(request, trusted_proxy_count=0) == "198.51.100.1"


def test_one_trusted_proxy_uses_the_entry_it_appended() -> None:
    # A client can put anything in the header; the trusted proxy appends the real address last.
    request = request_from("10.0.0.5", "6.6.6.6, 203.0.113.9")

    assert client_ip(request, trusted_proxy_count=1) == "203.0.113.9"


def test_two_trusted_proxies_count_from_the_right_across_headers() -> None:
    request = request_from("10.0.0.5", "6.6.6.6", "203.0.113.9, 10.0.0.2")

    assert client_ip(request, trusted_proxy_count=2) == "203.0.113.9"


@pytest.mark.parametrize("forwarded", [(), ("garbage",)])
def test_missing_or_invalid_forwarded_address_falls_back_to_the_peer(
    forwarded: tuple[str, ...],
) -> None:
    request = request_from("10.0.0.5", *forwarded)

    assert client_ip(request, trusted_proxy_count=1) == "10.0.0.5"


def test_no_address_at_all() -> None:
    assert client_ip(request_from(None), trusted_proxy_count=0) is None
