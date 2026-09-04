#!/usr/bin/env python3
"""Loopback-only Confluence fixture for visible OWL acceptance journeys.

The fixture accepts any non-empty Bearer value without storing or logging it. It must
never be used as an authentication example or exposed beyond an exact loopback bind.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def _person(name: str) -> dict[str, str]:
    return {"displayName": name}


ANCESTORS = [
    {
        "id": "900",
        "title": "Engineering",
        "_links": {"webui": "/wiki/spaces/OWL/pages/900/Engineering"},
        "extensions": {"position": 0},
    },
    {
        "id": "950",
        "title": "Network knowledge",
        "_links": {"webui": "/wiki/spaces/OWL/pages/950/Network+knowledge"},
        "extensions": {"position": 2},
    },
]


def _page(page_id: str, title: str, position: int) -> dict[str, object]:
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"key": "OWL", "name": "OWL acceptance space"},
        "version": {
            "number": 7,
            "when": "2026-08-24T10:15:00Z",
            "by": _person("Morgan Modifier"),
        },
        "history": {
            "createdDate": "2026-08-20T08:30:00Z",
            "createdBy": _person("Alex Author"),
        },
        "author": _person("Alex Author"),
        "ancestors": ANCESTORS,
        "extensions": {"position": position},
        "_links": {"webui": f"/wiki/spaces/OWL/pages/{page_id}/{title.replace(' ', '+')}"},
    }


PAGES = {
    "1001": _page("1001", "Network Architecture", 3),
    "1002": _page("1002", "Network Architecture Guide", 4),
}


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "OWLConfluenceFixture/1"

    def log_message(self, format: str, *args: object) -> None:
        """Disable request logging so an accidental header can never reach output."""

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        authorization = self.headers.get("Authorization", "")
        if (
            not authorization.startswith("Bearer ")
            or not authorization.removeprefix("Bearer ").strip()
        ):
            self._send_json(401, {"message": "Synthetic credential required"})
            return

        path = urlsplit(self.path).path
        if path == "/wiki/rest/api/user/current":
            self._send_json(200, {"displayName": "OWL Synthetic User", "type": "known"})
            return
        prefix = "/wiki/rest/api/content/"
        if path.startswith(prefix):
            page_id = path.removeprefix(prefix)
            page = PAGES.get(page_id)
            if page is not None:
                self._send_json(200, page)
                return
            self._send_json(404, {"message": "Synthetic page not found"})
            return
        self._send_json(404, {"message": "Synthetic endpoint not found"})

    def do_POST(self) -> None:
        self._send_json(405, {"message": "Fixture is read only"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OWL's loopback Confluence fixture.")
    parser.add_argument("--port", type=int, default=9876)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), FixtureHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
