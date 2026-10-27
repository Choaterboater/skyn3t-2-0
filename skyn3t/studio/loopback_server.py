"""Stdlib-only static preview launcher with no DNS dependency at bind time.

Executed by absolute path so a project's interpreter need not install SkyN3t.
"""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Serve a numeric loopback address without reverse DNS before listening."""

    def server_bind(self) -> None:
        if self.server_address[0] != "127.0.0.1":
            raise ValueError("Static previews must bind to 127.0.0.1")
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("--bind", choices=["127.0.0.1"], default="127.0.0.1")
    args = parser.parse_args()
    with LoopbackHTTPServer((args.bind, args.port), SimpleHTTPRequestHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
