"""Resolve www.deribit.com to a reachable Cloudflare IP when DNS is poisoned."""
import socket

_REAL_IP = "104.18.0.0"
_orig = socket.getaddrinfo


def _getaddrinfo(host, port, *args, **kwargs):
    if host == "www.deribit.com":
        host = _REAL_IP
    return _orig(host, port, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo
