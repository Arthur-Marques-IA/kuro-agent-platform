"""O título de uma conversa, gravado na sessão do Agno.

Sem `session_name`, o AgentOS usa a primeira mensagem do usuário como título — e
o Agno junta a ela as `dependencies` (`<additional context>{"cpf": ...}`). Quem
lê `/sessions` recebia o título com os dados do cliente. Aqui o título é gravado
uma vez, com só o texto da pessoa, logo depois do primeiro run da conversa.

Um nome que já existe (dado pelo console, pela CLI ou por este código) não é
trocado. O cache em memória evita reler a sessão a cada mensagem: depois da
primeira vez, nem consulta o banco.
"""

import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

MAX_TITLE = 80
_named: "OrderedDict[str, None]" = OrderedDict()
_lock = threading.Lock()
_CACHE_SIZE = 10_000


def title_from(message: str | None) -> str | None:
    line = next((ln.strip() for ln in (message or "").splitlines() if ln.strip()), "")
    if not line:
        return None
    return line if len(line) <= MAX_TITLE else line[: MAX_TITLE - 1].rstrip() + "…"


def _remember(session_id: str) -> None:
    with _lock:
        _named[session_id] = None
        _named.move_to_end(session_id)
        while len(_named) > _CACHE_SIZE:
            _named.popitem(last=False)


def ensure_session_name(agent: Any, session_id: str | None, user_id: str | None, message: str | None) -> None:
    """Grava o título se a sessão ainda não tem um. Nunca levanta: um título é enfeite."""
    if not session_id or session_id in _named or getattr(agent, "db", None) is None:
        return
    title = title_from(message)
    if title is None:
        return
    try:
        session = agent.get_session(session_id=session_id, user_id=user_id)
    except Exception:  # noqa: BLE001 - o Agno levanta Exception para sessão que não existe
        return
    if session is None:
        return
    try:
        data = dict(getattr(session, "session_data", None) or {})
        if not data.get("session_name"):
            data["session_name"] = title
            session.session_data = data
            agent.db.upsert_session(session)
        _remember(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("Não foi possível gravar o título da sessão %r", session_id, exc_info=True)


def ensure_session_name_later(agent: Any, session_id: str | None, user_id: str | None, message: str | None) -> None:
    """O mesmo, numa thread: fora do caminho da resposta."""
    if not session_id or session_id in _named or getattr(agent, "db", None) is None:
        return
    threading.Thread(
        target=ensure_session_name, args=(agent, session_id, user_id, message), daemon=True, name="kuro-session-name"
    ).start()
