import ipaddress


def parse_ip(value: str | None) -> str | None:
    """The address in canonical form, or None if `value` is not an IP address."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def ip_prefix(value: str | None) -> str | None:
    """The /24 (IPv4) or /48 (IPv6) network of an address: enough to spot an attack from one
    network, not enough to identify a person. Raw IPs are never stored (CLAUDE.md §7)."""
    ip = parse_ip(value)
    if ip is None:
        return None
    address = ipaddress.ip_address(ip)
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
