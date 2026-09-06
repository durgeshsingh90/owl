"""Serve the static UI and forward API requests to the local FastAPI server."""

import argparse
import http.client
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


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
        ):
            if self.command in ("GET", "HEAD"):
                return getattr(super(), "do_" + self.command)()
            self.send_error(405)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in ("host", "connection", "origin", "accept-encoding", "content-length")
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=40)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            self.send_response(response.status)
            self.send_header(
                "Content-Type", response.getheader("Content-Type", "application/json")
            )
            if response.getheader("X-Request-ID"):
                self.send_header("X-Request-ID", response.getheader("X-Request-ID"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self.send_error(502, "OWL backend unavailable")
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = dispatch


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
