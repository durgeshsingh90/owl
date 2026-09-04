from __future__ import annotations

from bookmark_manager.management.commands import run_owl


class _Thread:
    def __init__(self, *, name: str, events: list[str], **_kwargs) -> None:
        self.name = name
        self.events = events

    def start(self) -> None:
        self.events.append(f"start:{self.name}")

    def join(self, *, timeout: int) -> None:
        self.events.append(f"join:{self.name}:{timeout}")


def test_run_owl_starts_current_schedulers_and_semantic_supervisor(monkeypatch, settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    events: list[str] = []

    def thread_factory(**kwargs):
        return _Thread(events=events, **kwargs)

    def command(name, *args, **kwargs):
        events.append(f"command:{name}")
        assert args == ("127.0.0.1:9017",)
        assert kwargs == {"use_reloader": False}

    monkeypatch.setattr(run_owl.threading, "Thread", thread_factory)
    monkeypatch.setattr(run_owl, "call_command", command)

    run_owl.Command().handle(addrport="127.0.0.1:9017")

    assert events == [
        "start:owl-refresh-scheduler",
        "start:owl-bitbucket-scheduler",
        "start:owl-semantic-supervisor",
        "command:runserver",
        "join:owl-refresh-scheduler:7",
        "join:owl-bitbucket-scheduler:7",
        "join:owl-semantic-supervisor:7",
    ]


def test_run_owl_skips_semantic_supervisor_when_disabled(monkeypatch, settings):
    settings.SEMANTIC_SEARCH_ENABLED = False
    events: list[str] = []

    monkeypatch.setattr(
        run_owl.threading,
        "Thread",
        lambda **kwargs: _Thread(events=events, **kwargs),
    )
    monkeypatch.setattr(run_owl, "call_command", lambda *_args, **_kwargs: None)

    run_owl.Command().handle(addrport="127.0.0.1:9017")

    assert "start:owl-semantic-supervisor" not in events
