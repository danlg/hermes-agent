"""Display-only, per-turn observations for the opted-in Telegram heartbeat."""

from __future__ import annotations

from collections import deque
import json
import logging
import re
from threading import Lock


STUP_INTERVAL_SECONDS = 120
logger = logging.getLogger(__name__)

_OPERATIONS = {
    "read_file": ("Consulter les pièces nécessaires.", "Lecture d’une pièce terminée."),
    "write_file": ("Préparer le livrable demandé.", "Fichier de travail écrit."),
    "patch": ("Appliquer la modification en cours.", "Modification locale appliquée."),
    "search_files": ("Rechercher les références nécessaires.", "Recherche dans les fichiers terminée."),
    "web_search": ("Rechercher des sources.", "Recherche de sources terminée."),
    "web_extract": ("Consulter les sources trouvées.", "Consultation des sources terminée."),
    "terminal": ("Vérifier le résultat de la commande en cours.", None),
}


def _objective(message):
    if not isinstance(message, str):
        return "Traiter la demande de ce tour."
    text = re.sub(r"\[[^\]\n]*\|[^\]\n]*\]", "", message)
    text = re.sub(r"(?i)https?://\S+|\bBearer\s+\S+|"
                  r"\b(?:token|password|secret|api[_ -]?key|mot de passe|code d['’]accès)\s*[:=]\s*\S+",
                  "[masqué]", text)
    text = " ".join(text.split()).replace("MEDIA:", "").replace("[[", "").replace("]]", "")
    return text[:220] or "Traiter la demande de ce tour."


def _outcome(name, args, result):
    if isinstance(result, str):
        if len(result) > 2_000_000:
            return None, None
        try:
            result = json.loads(result)
        except ValueError:
            return None, None
    if not isinstance(result, dict):
        return None, None
    failed = (result.get("error") or result.get("success") is False
              or result.get("status") in ("ERROR", "error", "failed")
              or result.get("guardrail_refusal"))
    if failed:
        return None, "Examiner l’échec de la dernière opération avant de poursuivre."
    if name == "terminal":
        code = result.get("exit_code")
        if isinstance(code, int) and code:
            return None, f"Diagnostiquer l’échec de la commande (code {code})."
        if code == 0 and re.search(r"\bpytest\b", str(args.get("command", ""))):
            output = result.get("output")
            match = re.search(r"\b(\d+) passed\b", output) if isinstance(output, str) else None
            if match:
                return f"{match[1]} tests réussis.", None
        return None, None
    if name in {"write_file", "patch"}:
        from agent.tool_result_classification import file_mutation_result_landed

        if not file_mutation_result_landed(name, json.dumps(result)):
            return None, None
    elif (name == "read_file" and not isinstance(result.get("content"), str)
          or name == "search_files" and not isinstance(result.get("total_count"), int)
          or name == "web_search" and not isinstance(result.get("data"), dict)):
        return None, None
    elif name == "web_extract":
        pages = result.get("results")
        if not isinstance(pages, list) or not any(
                isinstance(page, dict) and page.get("content") and not page.get("error") for page in pages):
            return None, None
    return _OPERATIONS[name][1], None


class TelegramStupProgress:
    def __init__(self, message):
        self._objective = _objective(message)
        self._lock = Lock()
        self._active = {}
        self._facts = deque(maxlen=4)
        self._blocker = None
        self._runtime = ("modèle non confirmé", "niveau non confirmé", "")

    def record_runtime(self, model, effort, tier):
        if not isinstance(model, str) or not re.fullmatch(r"[\w./:-]{1,100}", model):
            model = "modèle non confirmé"
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            effort = "niveau non confirmé"
        with self._lock:
            self._runtime = model, effort, " . fast" if tier in ("fast", "priority") else ""

    def start(self, call_id, name):
        if name in _OPERATIONS:
            with self._lock:
                self._active[call_id] = _OPERATIONS[name][0]

    def complete(self, call_id, name, args, result):
        if name not in _OPERATIONS:
            return
        fact, blocker = _outcome(name, args if isinstance(args, dict) else {}, result)
        with self._lock:
            if self._active.pop(call_id, None) is None:
                return
            self._blocker = blocker
            if fact and fact not in self._facts:
                self._facts.append(fact)

    def observe_agent(self, agent, still_current):
        start, complete = agent.tool_start_callback, agent.tool_complete_callback

        def on_start(call_id, name, args):
            if still_current():
                self.start(call_id, name)
            if start:
                start(call_id, name, args)

        def on_complete(call_id, name, args, result):
            if still_current():
                self.complete(call_id, name, args, result)
            if complete:
                complete(call_id, name, args, result)

        agent.request_runtime_callback = self.record_runtime
        agent.tool_start_callback = on_start
        agent.tool_complete_callback = on_complete

    def render(self):
        with self._lock:
            model, effort, tier = self._runtime
            achieved = ["☑ " + fact for fact in self._facts] or ["Aucun résultat vérifié à ce stade."]
            next_step = self._blocker or next(reversed(self._active.values()), None)
        return "\n".join([
            f"model : {model} . {effort}{tier}",
            "**Acquis**",
            *achieved,
            "**Suite**",
            "☐ " + (next_step or "Poursuivre la demande et vérifier le résultat avant de le remettre."),
            "**Objectif** " + self._objective,
        ])


async def send_stup(adapter, chat_id, progress, metadata):
    try:
        result = await adapter.send(chat_id, progress.render(), metadata=metadata)
    except Exception as error:
        logger.warning("Telegram STUP delivery raised %s", type(error).__name__)
        return
    if not getattr(result, "success", False):
        logger.warning("Telegram STUP delivery was not acknowledged")
