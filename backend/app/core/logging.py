"""Bounded diagnostic logs without request bodies, credentials or exception text."""

import json
import logging
import os
import socket
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

request_id = ContextVar("request_id", default="-")
logger = logging.getLogger("owl")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                "request_id": getattr(record, "request_id", "-"),
                **getattr(record, "fields", {}),
            }
        )


def configure_logging():
    directory = Path(
        os.environ.get(
            "OWL_LOG_DIR", Path(__file__).resolve().parents[2] / "data" / "logs"
        )
    )
    directory.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        directory / "backend.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("OWL_LOG_LEVEL", "INFO").upper())
    logger.propagate = False


def event(name, level=logging.INFO, **fields):
    logger.log(level, name, extra={"request_id": request_id.get(), "fields": fields})


def error_details(error):
    """Keep types and numeric OS codes; exception strings can contain secrets."""
    chain, codes, seen = [], [], set()
    current = error
    dns = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(type(current).__name__)
        dns = dns or isinstance(current, socket.gaierror)
        code = getattr(current, "errno", None)
        if isinstance(code, int):
            codes.append(code)
        current = current.__cause__ or current.__context__
    return {"error_types": chain, "os_error_codes": codes, "dns_failure": dns}
