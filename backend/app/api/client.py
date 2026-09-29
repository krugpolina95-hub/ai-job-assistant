"""Who is calling: the client IP behind the proxies (TD-05) and the User-Agent."""

from fastapi import Request

from app.core.network import parse_ip


def client_ip(request: Request, trusted_proxy_count: int) -> str | None:
    """With N trusted proxies, the N-th X-Forwarded-For entry from the right is the address
    the outermost trusted proxy saw. Entries further left come from the client and can be
    forged, so they are never used. Without trusted proxies the TCP peer is the client."""
    peer = request.client.host if request.client else None
    if trusted_proxy_count == 0:
        return parse_ip(peer)
    forwarded = [
        entry.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for entry in header.split(",")
        if entry.strip()
    ]
    if len(forwarded) < trusted_proxy_count:
        return parse_ip(peer)
    return parse_ip(forwarded[-trusted_proxy_count]) or parse_ip(peer)
