from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.turn_api_request import report_request_runtime
from gateway.config import Platform
from gateway.display_config import resolve_display_setting
from gateway.run import GatewayRunner
from gateway.stup_progress import TelegramStupProgress
from gateway.turn_context import TurnContext


def test_telegram_progress_reports_only_active_request_and_observed_outcomes():
    progress = TelegramStupProgress("Vérifier le dossier. https://example.test/download?token=secret")
    agent = SimpleNamespace(tool_start_callback=Mock(), tool_complete_callback=Mock())
    delegated_start = agent.tool_start_callback
    delegated_complete = agent.tool_complete_callback
    progress.observe_agent(agent, lambda: True)

    report_request_runtime(agent, {
        "model": "original-model", "reasoning": {"effort": "low"},
        "extra_body": {"model": "active-model", "reasoning": {"effort": "medium"}, "service_tier": "priority"},
    })
    agent.tool_start_callback("read", "read_file", {"path": "document.pdf"})
    agent.tool_complete_callback("read", "read_file", {}, '{"content":"Private file details"}')
    agent.tool_start_callback("terminal", "terminal", {"command": "pytest"})
    text = progress.render()
    assert text.startswith("model : active-model . medium . fast\n")
    assert "☑ Lecture d’une pièce terminée." in text
    assert "☐ Vérifier le résultat de la commande en cours." in text
    assert "**Objectif** Vérifier le dossier. [masqué]" in text
    assert all(word not in text for word in ("Private file details", "document.pdf", "pytest", "secret"))
    delegated_start.assert_called_with("terminal", "terminal", {"command": "pytest"})
    delegated_complete.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform, opted_in, restarting, delays, expected",
    [
        (Platform.TELEGRAM, True, False, [120, 120, 120], 2),
        (Platform.TELEGRAM, True, True, [120, 120, 120], 2),
        (Platform.TELEGRAM, False, False, [180, 180, 180], 1),
        (Platform.DISCORD, False, True, [180, 180], 1),
    ],
)
async def test_native_heartbeat_uses_telegram_opt_in_cadence_and_live_turn_owner(
    monkeypatch, platform, opted_in, restarting, delays, expected,
):
    from gateway import run_turn
    from gateway.drain_control import RESTART_WAIT_REASON

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running = True
    runner._draining = runner._restart_requested = restarting and opted_in
    runner.session_runtime_descriptor = {"drain_reason": RESTART_WAIT_REASON} if runner._draining else {}
    agent = object()
    runner._running_agents["scope"] = agent
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="hb")),
                              edit_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner._delivery_adapter_for = lambda source: adapter
    runner._agent_activity_summary = lambda active: None
    config = {"display": {"platforms": {"telegram": {"stup_updates": opted_in}}}}
    disp = SimpleNamespace(user_config=config, platform_key=platform.value,
                           _display_surface_mode=lambda *args, **kwargs: "raw",
                           resolve_display_setting=resolve_display_setting)
    ctx = TurnContext(source=SimpleNamespace(chat_id="chat", platform=platform), session_key="scope",
                      _status_thread_metadata={"thread_id": "topic"}, _run_still_current=lambda: True)
    if platform == Platform.TELEGRAM and resolve_display_setting(config, platform.value, "stup_updates"):
        ctx.stup_progress = TelegramStupProgress("Vérifier les pièces")
    ctx.agent_holder[0] = agent
    executor = Mock()
    executor.done.return_value = False
    observed = []

    async def advance(seconds):
        observed.append(seconds)
        if len(observed) == len(delays):
            if opted_in and restarting:
                runner._running = False  # actual stop begins, after-turn wait is closed
            else:
                executor.done.return_value = True
        if restarting and not opted_in:
            runner._restart_requested = len(observed) > 1

    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "180")
    monkeypatch.setattr(run_turn.asyncio, "sleep", advance)
    await runner._run_agent_notify_long_running(disp, ctx, [executor])
    assert observed == delays
    assert adapter.send.await_count == expected
    for sent in adapter.send.await_args_list:
        assert sent.args[0] == "chat"
        assert sent.kwargs["metadata"]["thread_id"] == "topic"
        assert sent.kwargs["metadata"]["_interim_send"] is True
        assert ("**Acquis**" in sent.args[1]) is opted_in
    assert adapter.edit_message.await_count == (0 if opted_in or restarting else 1)
