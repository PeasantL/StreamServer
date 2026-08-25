"""Network access control.

Two things the previous version got wrong, both fixed here:

  * ``X-Forwarded-For`` is attacker-controlled unless the request actually came
    from a proxy you run. It is now consulted only when the immediate peer is
    listed in ``trusted_proxies``, and even then the *rightmost* untrusted hop
    is used, because a client can prepend as many fake hops as it likes.
  * Raising ``HTTPException`` from an ``@app.middleware("http")`` function does
    not produce a 403. FastAPI installs its exception handlers inside the user
    middleware stack, so the exception escapes to ServerErrorMiddleware and
    becomes an unhandled 500. The middleware returns a response instead.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address, ip_network

from fastapi import Request
from fastapi.responses import JSONResponse

from config import settings

log = logging.getLogger(__name__)


def is_ip_allowed(remote_addr: str | None, allowed: tuple[str, ...] | list[str]) -> bool:
    """True when *remote_addr* falls inside any allowed address or CIDR block."""
    if not remote_addr:
        return False
    try:
        client = ip_address(remote_addr)
    except ValueError:
        return False

    for entry in allowed:
        try:
            network = ip_network(entry, strict=False)
        except ValueError:
            continue
        if client.version == network.version and client in network:
            return True
    return False


def client_ip(request: Request) -> str | None:
    """Resolve the caller's address.

    The transport-level peer is authoritative. Forwarding headers are believed
    only when that peer is a configured trusted proxy; the value taken is the
    last hop the proxy chain appended that is not itself a trusted proxy.
    """
    peer = request.client.host if request.client else None
    if not peer or not settings.trusted_proxies:
        return peer
    if not is_ip_allowed(peer, settings.trusted_proxies):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not is_ip_allowed(hop, settings.trusted_proxies):
            return hop
    return peer


async def whitelist_middleware(request: Request, call_next):
    address = client_ip(request)

    if not is_ip_allowed(address, settings.allowed_ips):
        log.warning("Denied request from %s for %s", address, request.url.path)
        return JSONResponse(
            status_code=403,
            content={"detail": f"Access denied for IP {address}"},
        )

    return await call_next(request)
