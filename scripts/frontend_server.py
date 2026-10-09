"""Serve the static UI and forward API requests to the local FastAPI server."""

import argparse
import http.client
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


CHUNK = 1024 * 1024
# Response headers the browser needs from the backend.
PASS_THROUGH = (
    "Content-Type", "Content-Length", "Content-Disposition", "X-Request-ID",
    "X-File-Name", "X-File-Size", "Location",
)


class Handler(SimpleHTTPRequestHandler):
    backend_port = 8000

    def end_headers(self):
        # Development assets must not mix cached preview code with new handlers.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def dispatch(self):
        if not (
            self.path.startswith("/api/")
            or self.path.startswith("/bitbucket/settings/")
            or self.path.startswith("/bitbucket/workspace/")
            or self.path.startswith("/bookmarks/settings/")
            or self.path.startswith("/bookmarks/connection/")
        ):
            if self.command in ("GET", "HEAD"):
                return getattr(super(), "do_" + self.command)()
            self.send_error(405)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send_error(403)
            return
        # Bodies are streamed both ways: big-file uploads (JSON Visualizer server
        # mode, snapshots) and downloads can be gigabytes.
        length = int(self.headers.get("Content-Length", "0") or 0)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in ("host", "connection", "origin", "accept-encoding", "content-length", "transfer-encoding")
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=600)
        try:
            conn.putrequest(self.command, self.path, skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            if length:
                conn.putheader("Content-Length", str(length))
            conn.endheaders()
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    break
                conn.send(chunk)
                remaining -= len(chunk)
            response = conn.getresponse()
            self.send_response(response.status)
            for key in PASS_THROUGH:
                value = response.getheader(key)
                if value is not None:
                    self.send_header(key, value)
            if response.getheader("Content-Type") is None:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            try:
                self.send_error(502, "OWL backend unavailable")
            except OSError:
                pass
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend-port", type=int, required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    Handler.backend_port = args.backend_port
    ThreadingHTTPServer(
        ("127.0.0.1", args.port), partial(Handler, directory=args.directory)
    ).serve_forever()
