"""Compatibility adapter for the stock Hermes heartbeat on ASR's Telegram profile."""

import hashlib
import inspect
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from types import FunctionType, SimpleNamespace

LOG = logging.getLogger(__name__)
NATIVE_TIMER = "a4465c2af3c1f94a7bfe9a4565aba26a22b724ec5a75e225024e983ed642e434"
INTERVAL = 120
EFFORT = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
TOOLS = {
    "read_file": ("Consulter la pièce de travail.", "Lecture de la pièce terminée."),
    "search_files": ("Rechercher les pièces utiles.", "Recherche des pièces terminée."),
    "web_search": ("Rechercher des sources.", "Recherche des sources terminée."),
    "terminal": ("Vérifier le résultat de l’opération en cours.", None),
    "preparer_travail_cabinet": ("Vérifier le dossier ou la messagerie.", None),
}


def telegram(platform):
    return getattr(platform, "value", platform) == "telegram"


@dataclass
class Progress:
    turn: str
    runtime: tuple = ("modèle non confirmé", "niveau non confirmé", False)
    active: dict = field(default_factory=dict)
    done: deque = field(default_factory=lambda: deque(maxlen=4))
    blocker: str = ""


class TelegramStup:
    def __init__(self):
        self.lock = Lock()
        self.turns = {}

    def start(self, *, platform="", session_id="", turn_id="", **_):
        if telegram(platform) and session_id and turn_id:
            with self.lock:
                if session_id not in self.turns or self.turns[session_id].turn != turn_id:
                    self.turns[session_id] = Progress(turn_id)

    def finish(self, *, session_id="", turn_id="", **_):
        with self.lock:
            state = self.turns.get(session_id)
            if state is not None and state.turn == turn_id:
                self.turns.pop(session_id)

    def request(self, *, request, next_call, session_id="", turn_id="", **_):
        with self.lock:
            state = self.turns.get(session_id)
            if state is not None and state.turn == turn_id:
                extra = request.get("extra_body")
                body = request | extra if isinstance(extra, dict) else request
                model = body.get("model")
                reasoning = body.get("reasoning")
                effort = reasoning.get("effort") if isinstance(reasoning, dict) else body.get("reasoning_effort")
                state.runtime = (
                    model if isinstance(model, str) and re.fullmatch(r"[\w./:-]{1,100}", model) else "modèle non confirmé",
                    effort if isinstance(effort, str) and effort in EFFORT else "niveau non confirmé",
                    body.get("service_tier") in ("fast", "priority"),
                )
        return next_call(request)

    def tool_start(self, *, session_id="", turn_id="", tool_name="", tool_call_id="", **_):
        name = tool_name.rsplit("__", 1)[-1]
        if name not in TOOLS:
            return
        with self.lock:
            state = self.turns.get(session_id)
            if state is not None and state.turn == turn_id:
                state.active[tool_call_id] = TOOLS[name][0]

    def tool_end(self, *, session_id="", turn_id="", tool_name="", tool_call_id="", status="", result=None, **_):
        name = tool_name.rsplit("__", 1)[-1]
        if name not in TOOLS:
            return
        if isinstance(result, str):
            try:
                result = json.loads(result) if len(result) <= 2_000_000 else None
            except ValueError:
                result = None
        result = result if isinstance(result, dict) else {}
        with self.lock:
            state = self.turns.get(session_id)
            if state is None or state.turn != turn_id or state.active.pop(tool_call_id, None) is None:
                return
            failed = (status in {"error", "blocked", "cancelled"} or result.get("error")
                      or result.get("success") is False or result.get("status") in ("ERROR", "error", "failed")
                      or isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
            state.blocker = "Vérifier l’échec de la dernière opération avant de poursuivre." if failed else ""
            outcome = TOOLS[name][1] if status == "ok" and result and not failed else None
            if outcome and outcome not in state.done:
                state.done.append(outcome)

    def render(self, session_id):
        with self.lock:
            state = self.turns.get(session_id) or Progress("")
            model, effort, fast = state.runtime
            done = [f"- ✅ {fact}" for fact in state.done]
            followup = state.blocker or next(reversed(state.active.values()), None)
        return "\n\n".join([
            "**✅ Réalisé :**",
            f"**Modèle :** `{model}, {effort}{' . fast' if fast else ''}`",
            *done,
            "**🧭 À faire :**",
            "- ⬜ " + (followup or "Vérifier le résultat de la demande avant de le remettre."),
            "**🎯 Objectif :**",
            "Terminer la demande de ce fil et remettre le résultat vérifié.",
        ])


def install_native_timer(mixin, progress, expected=NATIVE_TIMER):
    original = mixin._run_agent_notify_long_running
    if getattr(original, "_alexys_stup", False):
        return False
    try:
        source = inspect.getsource(original).rstrip()
    except (OSError, TypeError):
        return False
    if hashlib.sha256(source.encode()).hexdigest() != expected:
        return False

    @wraps(original)
    async def notify(self, disp, turn, executor):
        source = turn.source
        display = (disp.user_config or {}).get("display", {})
        config = (display.get("platforms") or {}).get("telegram", {})
        if not telegram(source.platform) or config.get("stup_updates") is not True:
            return await original(self, disp, turn, executor)

        native = original.__globals__

        def translate(key, *args, **kwargs):
            if key == "gateway.progress.working_heartbeat":
                agent = turn.agent_holder[0]
                return progress.render(getattr(agent, "session_id", None) or turn.session_id)
            return native["t"](key, *args, **kwargs)

        # The pinned native function has one asyncio.sleep. Keep its task, delivery, edit and cancellation.
        globals_for_turn = {**native, "t": translate,
                            "asyncio": SimpleNamespace(sleep=lambda _interval: native["asyncio"].sleep(INTERVAL))}
        invoke = FunctionType(original.__code__, globals_for_turn, closure=original.__closure__)
        return await invoke(self, disp, turn, executor)

    notify._alexys_stup = True
    mixin._run_agent_notify_long_running = notify
    return True


def register(ctx):
    from gateway.run_turn import GatewayTurnMixin

    progress = TelegramStup()
    if not install_native_timer(GatewayTurnMixin, progress):
        LOG.warning("Telegram STUP compatibility not enabled: native timer changed or already installed")
        return
    ctx.register_hook("pre_llm_call", progress.start)
    ctx.register_hook("post_llm_call", progress.finish)
    ctx.register_hook("pre_tool_call", progress.tool_start)
    ctx.register_hook("post_tool_call", progress.tool_end)
    ctx.register_middleware("llm_execution", progress.request)
    LOG.info("Telegram STUP compatibility enabled (native timer, 120 seconds)")
