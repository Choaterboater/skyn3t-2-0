"""A numeric loopback preview must not depend on external name resolution."""

import socket
from http.server import SimpleHTTPRequestHandler

import pytest

from skyn3t.studio.loopback_server import LoopbackHTTPServer


def test_bind_does_not_resolve_names(monkeypatch):
    def unexpected_lookup(*args):
        raise AssertionError("numeric loopback binding must not resolve DNS")

    monkeypatch.setattr(socket, "getfqdn", unexpected_lookup)
    with LoopbackHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler) as server:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "localhost", "::"])
def test_bind_rejects_non_loopback_hosts(host):
    with pytest.raises(ValueError, match="127.0.0.1"):
        LoopbackHTTPServer((host, 0), SimpleHTTPRequestHandler)
