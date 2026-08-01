from __future__ import annotations

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cpip.network.http import NetworkSession


def test_session_decodes_gzip_responses() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            body = gzip.compress(b"compressed")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        response = session.get(url)
        assert response.content == b"compressed"
        assert response.headers.get("Content-Encoding") is None
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_session_reuses_direct_http_connection() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        connections = 0
        requests = 0

        def setup(self) -> None:
            super().setup()
            type(self).connections += 1

        def do_GET(self) -> None:
            type(self).requests += 1
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        assert session.get(url).content == b"ok"

        responses: list[bytes] = []

        def request() -> None:
            responses.append(session.get(url).content)

        worker = threading.Thread(target=request)
        worker.start()
        worker.join()
        assert responses == [b"ok"]
        assert Handler.requests == 2
        assert Handler.connections == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_session_coalesces_concurrent_gets() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            time.sleep(0.1)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        barrier = threading.Barrier(2)
        responses: list[bytes] = []

        def request() -> None:
            barrier.wait()
            responses.append(session.get(url).content)

        workers = [threading.Thread(target=request) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert responses == [b"ok", b"ok"]
        assert Handler.requests == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
