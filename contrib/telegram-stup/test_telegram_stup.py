from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HERE = Path(__file__).parent
PLUGIN = HERE / "alexys-telegram-stup" / "__init__.py"
if not PLUGIN.is_file():
    PLUGIN = HERE.parents[1] / "plugins" / "messaging" / "alexys-telegram-stup" / "__init__.py"
spec = importlib.util.spec_from_file_location("telegram_stup_test", PLUGIN)
stup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = stup
spec.loader.exec_module(stup)


@pytest.mark.parametrize(("platform", "enabled", "interval"), [
    ("telegram", True, 120), ("telegram", False, 180), ("discord", True, 180),
])
def test_stock_native_timer_delivery(monkeypatch, platform, enabled, interval):
    import gateway.run_turn as native_module

    class Mixin(native_module.GatewayTurnMixin):
        pass

    progress = stup.TelegramStup()
    assert stup.install_native_timer(Mixin, progress)
    adapter = Mock()
    sent, edited, sleeps = [], [], []
    running = True

    async def sleep(seconds):
        nonlocal running
        sleeps.append(seconds)
        if len(sleeps) == 3:
            running = False

    async def send(chat, text, metadata):
        sent.append((chat, text, metadata))
        return SimpleNamespace(success=True, message_id="fixture-message")

    async def edit(chat, message_id, text):
        edited.append((chat, message_id, text))
        return SimpleNamespace(success=True)

    adapter.send = send
    adapter.edit_message = edit
    monkeypatch.setattr(native_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "180")
    gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter,
                              _should_emit_long_running_notification=lambda *_: running,
                              _agent_activity_summary=lambda _: {})
    user_config = {"display": {"platforms": {"telegram": {"stup_updates": enabled}}}}
    display = SimpleNamespace(user_config=user_config, platform_key=platform,
                              _display_surface_mode=lambda *a, **kw: "on",
                              resolve_display_setting=lambda *a: True)
    turn = SimpleNamespace(source=SimpleNamespace(platform=platform, chat_id="fixture"),
                           session_key="key", session_id="session",
                           agent_holder=[SimpleNamespace(session_id="session")],
                           _status_thread_metadata={"thread_id": "23"}, _cleanup_progress=False)
    progress.start(platform="telegram", session_id="session", turn_id="one")
    progress.request(request={"model": "gpt-fixture", "reasoning": {"effort": "medium"}, "service_tier": "priority"},
                     next_call=lambda request: request, session_id="session", turn_id="one")
    asyncio.run(Mixin._run_agent_notify_long_running(gateway, display, turn, [None]))
    assert sleeps == [interval] * 3
    assert len(sent) == len(edited) == 1
    assert sent[0][0] == edited[0][0] == "fixture"
    assert sent[0][2]["thread_id"] == "23"
    if interval == 120:
        assert "gpt-fixture, medium . fast" in sent[0][1]
        assert "🎯 Objectif" in edited[0][2]
    else:
        assert "🎯 Objectif" not in sent[0][1]


def test_native_change_disables_compatibility():
    from gateway.run_turn import GatewayTurnMixin

    class Mixin(GatewayTurnMixin):
        pass

    original = Mixin._run_agent_notify_long_running
    assert not stup.install_native_timer(Mixin, stup.TelegramStup(), expected="stale")
    assert Mixin._run_agent_notify_long_running is original


def test_turn_metadata_isolated_and_unverified_results_not_reported():
    progress = stup.TelegramStup()
    ids = dict(session_id="session", turn_id="one")
    progress.start(platform="telegram", **ids)
    progress.tool_start(tool_name="read_file", tool_call_id="read", **ids)
    progress.tool_end(tool_name="read_file", tool_call_id="read", status="error",
                      result={"content": "private-fixture", "error": "fictitious"}, **ids)
    report = progress.render("session")
    assert "private-fixture" not in report and "Lecture de la pièce terminée" not in report
    assert "échec de la dernière opération" in report
    progress.start(platform="telegram", session_id="session", turn_id="two")
    progress.finish(**ids)
    assert progress.turns["session"].turn == "two"
