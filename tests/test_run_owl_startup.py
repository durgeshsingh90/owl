from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.core.management.base import CommandError

from bookmark_manager.management.commands import run_owl


class _Thread:
    def __init__(self, *, name: str, events: list[str], **_kwargs) -> None:
        self.name = name
        self.events = events

    def start(self) -> None:
        self.events.append(f"start:{self.name}")

    def join(self, *, timeout: int) -> None:
        self.events.append(f"join:{self.name}:{timeout}")


def test_run_owl_repairs_pdf_search_before_starting_any_services(monkeypatch):
    events: list[str] = []

    def ensure_search_index() -> bool:
        events.append("ensure-search-index")
        return True

    def thread_factory(**kwargs):
        return _Thread(events=events, **kwargs)

    def command(name, *args, **kwargs):
        events.append(f"command:{name}")
        assert args == ("127.0.0.1:9017",)
        assert kwargs == {"use_reloader": False}

    monkeypatch.setattr(run_owl, "ensure_search_index_available", ensure_search_index)
    monkeypatch.setattr(run_owl.threading, "Thread", thread_factory)
    monkeypatch.setattr(run_owl, "call_command", command)

    run_owl.Command().handle(addrport="127.0.0.1:9017")

    assert events == [
        "ensure-search-index",
        "start:owl-refresh-scheduler",
        "start:owl-bitbucket-supervisor",
        "command:runserver",
        "join:owl-refresh-scheduler:5",
        "join:owl-bitbucket-supervisor:7",
    ]


@pytest.mark.parametrize("failure", [False, RuntimeError("synthetic repair failure")])
def test_run_owl_starts_nothing_when_pdf_search_cannot_be_repaired(monkeypatch, failure):
    ensure_search_index = Mock(side_effect=failure if isinstance(failure, Exception) else None)
    if not isinstance(failure, Exception):
        ensure_search_index.return_value = False
    thread = Mock(side_effect=AssertionError("No service thread may be created"))
    command = Mock(side_effect=AssertionError("The web server may not be started"))
    monkeypatch.setattr(run_owl, "ensure_search_index_available", ensure_search_index)
    monkeypatch.setattr(run_owl.threading, "Thread", thread)
    monkeypatch.setattr(run_owl, "call_command", command)

    with pytest.raises(CommandError, match="could not prepare its local PDF search index"):
        run_owl.Command().handle(addrport="127.0.0.1:9017")

    ensure_search_index.assert_called_once_with()
    thread.assert_not_called()
    command.assert_not_called()
