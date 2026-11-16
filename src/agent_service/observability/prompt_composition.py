"""Quanto do prompt de um run veio de cada parte.

Mede as mensagens que o Agno realmente montou para a **primeira** chamada ao modelo
do run (as que vêm antes da primeira resposta do assistente nesta execução). Caracteres
são a medida exata; `tokens_estimate` é só `chars / 4`, uma estimativa grosseira para
ter uma ordem de grandeza — o número real de tokens da chamada está na span do modelo.
Fora da conta: as definições das tools (vão ao provedor à parte, não são mensagens) e o
binário dos anexos (só a contagem).

Partes (`parts`, em caracteres):

- `instructions`: o prompt de sistema sem o contexto da sessão, sem as memórias e sem o resumo;
- `session_context`: o texto que `agents/system_context.py` acrescenta ao sistema;
- `memories`, `session_summary`: os blocos que o Agno põe no sistema (`<memories_from_previous_interactions>`
  e `<summary_of_previous_interactions>`);
- `history`: falas de usuário e assistente de turnos anteriores (`from_history`);
- `history_tools`: chamadas e resultados de tool vindos do histórico;
- `turn`: a mensagem do turno (sem as dependencies que o Agno acrescenta a ela);
- `dependencies`: o bloco `<additional context>` que o Agno acrescenta à mensagem do turno;
- `other`: qualquer outra mensagem da primeira chamada.

A medição nunca derruba o run: qualquer falha vira `None` e um aviso no log.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

PARTS = (
    "instructions",
    "session_context",
    "memories",
    "session_summary",
    "history",
    "history_tools",
    "turn",
    "dependencies",
    "other",
)
CHARS_PER_TOKEN = 4

_MEMORIES = re.compile(r"<memories_from_previous_interactions>.*?</memories_from_previous_interactions>", re.S)
_SUMMARY = re.compile(r"<summary_of_previous_interactions>.*?</summary_of_previous_interactions>", re.S)
_DEPENDENCIES = re.compile(r"<additional context>.*?</additional context>", re.S)


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            (part.get("text") or "") if isinstance(part, dict) else (part if isinstance(part, str) else "")
            for part in content
        )
    return str(content)


def _tool_calls_chars(message: Any) -> int:
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return 0
    try:
        return len(json.dumps(calls, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(calls))


def first_call_messages(messages: list[Any]) -> list[Any]:
    """As mensagens da primeira chamada: tudo antes da primeira resposta do assistente
    **desta** execução (as do histórico, `from_history`, ficam)."""
    for index, message in enumerate(messages):
        if getattr(message, "role", None) == "assistant" and not getattr(message, "from_history", False):
            return messages[:index]
    return messages


def measure(
    messages: list[Any] | None, *, system_context: str | None = None, attachments: int = 0
) -> dict[str, Any] | None:
    """A composição do prompt, ou `None` se não há mensagens ou a medição falhou."""
    if not messages:
        return None
    try:
        return _measure(messages, system_context, attachments)
    except Exception:
        logger.warning("Não consegui medir a composição do prompt", exc_info=True)
        return None


def _measure(messages: list[Any], system_context: str | None, attachments: int) -> dict[str, Any] | None:
    sent = first_call_messages(messages)
    if not sent:
        return None
    parts = dict.fromkeys(PARTS, 0)
    for message in sent:
        role = getattr(message, "role", None)
        text = _text(getattr(message, "content", None))
        from_history = bool(getattr(message, "from_history", False))
        if role in ("system", "developer"):
            context = len(system_context) if system_context and system_context in text else 0
            memories = sum(len(m) for m in _MEMORIES.findall(text))
            summary = sum(len(m) for m in _SUMMARY.findall(text))
            # As dependencies do run vão no fim do prompt de sistema (`agents/system_context.py`),
            # depois do contexto da sessão: procure o bloco fora dele.
            outside = text.replace(system_context, "", 1) if context and system_context else text
            dependencies = sum(len(m) for m in _DEPENDENCIES.findall(outside))
            parts["session_context"] += context
            parts["memories"] += memories
            parts["session_summary"] += summary
            parts["dependencies"] += dependencies
            parts["instructions"] += max(len(text) - context - memories - summary - dependencies, 0)
        elif from_history:
            if role == "tool":
                parts["history_tools"] += len(text)
            else:
                parts["history"] += len(text)
                parts["history_tools"] += _tool_calls_chars(message)
        elif role == "user":
            dependencies = sum(len(m) for m in _DEPENDENCIES.findall(text))
            parts["dependencies"] += dependencies
            parts["turn"] += len(text) - dependencies
        else:
            parts["other"] += len(text) + _tool_calls_chars(message)
    total = sum(parts.values())
    return {
        "chars": total,
        "parts": parts,
        "tokens_estimate": round(total / CHARS_PER_TOKEN),
        "tokens_estimate_method": f"chars/{CHARS_PER_TOKEN}",
        "messages": len(sent),
        "attachments": attachments,
    }


def history_chars(composition: dict[str, Any] | None) -> int | None:
    """O que é histórico (conversa e tools antigas) numa composição gravada."""
    if not composition:
        return None
    parts = composition.get("parts") or {}
    return int(parts.get("history", 0)) + int(parts.get("history_tools", 0))
