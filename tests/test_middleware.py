"""Access control: the allowlist, and whether forwarding headers are believed."""

import pytest

from middleware import is_ip_allowed


@pytest.mark.parametrize(
    ("address", "allowed", "expected"),
    [
        ("127.0.0.1", ["127.0.0.1"], True),
        ("127.0.0.2", ["127.0.0.1"], False),
        ("192.168.1.50", ["192.168.0.0/16"], True),
        ("10.0.0.1", ["192.168.0.0/16"], False),
        ("::1", ["::1"], True),
        # A v4 address must not match a v6 block, or vice versa.
        ("127.0.0.1", ["::1"], False),
        (None, ["127.0.0.1"], False),
        ("not-an-ip", ["127.0.0.1"], False),
        ("127.0.0.1", [], False),
        ("127.0.0.1", ["garbage", "127.0.0.1"], True),
    ],
)
def test_is_ip_allowed(address, allowed, expected):
    assert is_ip_allowed(address, allowed) is expected


def test_allowed_peer_gets_through(client):
    test_client, _ = client
    assert test_client.get("/healthz").status_code == 200


def test_denied_request_returns_403_not_500(app_module):
    """Raising HTTPException in middleware produced an unhandled 500."""
    from fastapi.testclient import TestClient

    from tests.conftest import with_peer

    with TestClient(with_peer(app_module.app, "203.0.113.9")) as outsider:
        denied = outsider.get("/healthz")

    assert denied.status_code == 403
    assert "Access denied" in denied.json()["detail"]


def test_forwarded_header_cannot_forge_an_allowed_address(app_module):
    """The bypass: X-Forwarded-For was preferred over the real peer."""
    from fastapi.testclient import TestClient

    from tests.conftest import with_peer

    with TestClient(with_peer(app_module.app, "203.0.113.9")) as outsider:
        response = outsider.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 403


def test_forwarded_header_is_used_when_the_peer_is_a_trusted_proxy(monkeypatch, app_module):
    import dataclasses

    from fastapi.testclient import TestClient

    import middleware
    from tests.conftest import with_peer

    monkeypatch.setattr(
        middleware,
        "settings",
        dataclasses.replace(
            middleware.settings, trusted_proxies=("203.0.113.9",), allowed_ips=("127.0.0.1",)
        ),
    )

    with TestClient(with_peer(app_module.app, "203.0.113.9")) as proxied:
        allowed = proxied.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})
        # A client prepending fake hops must not win: the rightmost untrusted
        # hop is the one that counts.
        spoofed = proxied.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.7"})

    assert allowed.status_code == 200
    assert spoofed.status_code == 403
