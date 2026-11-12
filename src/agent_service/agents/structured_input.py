"""Entrada estruturada do `/chat`: `events` e `context` separados por campo.

Sem isto o sistema integrado concatena tudo em `message` (configuração, dados do
contato, histórico com `[ALUNO]`/`[ATENDENTE]`, a fala nova), e quem lê o run
depois precisa adivinhar onde termina cada trecho. Aqui cada trecho chega no seu
campo e o servidor monta o que o modelo lê com gabaritos fixos:

- **o contexto é estado da sessão**, não parte da mensagem: os blocos vigentes
  (os que vieram, ou os herdados do turno anterior — `agents/session_context.py`)
  vão uma vez só no prompt de sistema, depois das instruções (`render_context`).
  Não entram na mensagem do usuário, então o histórico do Agno não os repete;
- **a mensagem do turno** (`build_turn_text`) traz os `events` anteriores à fala,
  rotulados pelo papel, a fala nova e, se houver, os eventos que vieram depois
  dela (só quando a fala é um evento). Um `agent_sent` igual à última resposta do
  agente (`echo_indices`) sai do texto: o histórico já traz essa resposta.

Os gabaritos são determinísticos (mesma entrada, mesmo texto) e moram só aqui; os
testes de snapshot (`tests/test_structured_input.py`) seguram qualquer mudança. Não
há parser do texto montado: quem precisa dos trechos lê as colunas do run
(`input_events`, `input_context`, `input_message`), nunca o texto.

Este módulo não conhece FastAPI: valida e monta. A rota decide o status HTTP.
"""

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_CONTEXT_BLOCKS = 30
MAX_EVENTS = 200

EventRole = Literal["contact", "human_agent", "agent_sent", "system"]
EventKind = Literal["text", "document"]

ROLE_LABELS: dict[str, str] = {
    "contact": "Contato",
    "human_agent": "Atendente humano",
    "agent_sent": "Enviado em seu nome",
    "system": "Sistema",
}
"""Como cada papel aparece para o modelo. `agent_sent` é o que a instituição de
fato enviou em nome do agente — pode diferir do que o agente respondeu."""

CONTEXT_HEADING = "# Contexto"
CONTEXT_NOTE = "Este é o contexto atual desta conversa. Ele substitui qualquer versão anterior."
"""A frase fixa sob o título do contexto, no prompt de sistema."""
EVENTS_HEADING = "# Conversa desde a sua última resposta"
MESSAGE_HEADING = "# Mensagem nova do contato"
AFTER_HEADING = "# Depois dessa mensagem"


class EventAttachment(BaseModel):
    """O que veio anexado a um evento, só descrito: nome, tipo, link. O conteúdo
    não passa por aqui — para o modelo ver o arquivo, use `attachments` do corpo."""

    filename: str | None = Field(default=None, max_length=300)
    mime_type: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _sem_conteudo(cls, value: Any) -> Any:
        if isinstance(value, dict) and "content_base64" in value:
            raise ValueError(
                "attachments de um evento só descrevem o arquivo (filename, mime_type, url, size_bytes); "
                "para o modelo ler o conteúdo, mande-o em attachments do corpo"
            )
        return value


class InputEvent(BaseModel):
    """Uma coisa que aconteceu desde a última resposta do agente, em ordem."""

    role: EventRole = Field(
        description="contact (o contato), human_agent (um atendente humano), agent_sent (o que a "
        "instituição de fato enviou em nome do agente) ou system (aviso do sistema)."
    )
    content: str = Field(default="", description="O texto do evento.")
    at: datetime | None = Field(default=None, description="Quando aconteceu (ISO 8601). Com fuso, é guardado em UTC.")
    kind: EventKind = Field(default="text", description="text ou document (o contato mandou um documento).")
    attachments: list[EventAttachment] = Field(
        default_factory=list,
        max_length=20,
        description="Descrição dos anexos do evento ({filename, mime_type, url, size_bytes}), sem o conteúdo.",
    )

    @model_validator(mode="after")
    def _nao_vazio(self) -> "InputEvent":
        if not self.content.strip() and not self.attachments:
            raise ValueError("evento sem content e sem attachments")
        return self


class ContextBlock(BaseModel):
    """Um bloco nomeado de texto para o modelo ler (dados do contato, configuração...)."""

    key: str = Field(
        min_length=1,
        max_length=64,
        description="Identificador estável do bloco: o mesmo a cada turno, único na requisição.",
    )
    title: str = Field(min_length=1, max_length=200, description="Rótulo para humanos; é o título do bloco no prompt.")
    content: str = Field(description="O texto do bloco.")
    volatile: bool = Field(
        default=False,
        description="Muda todo turno (ex.: contexto temporal) e não conta como 'contexto mudou'.",
    )

    @field_validator("key", "title")
    @classmethod
    def _sem_espacos_nas_pontas(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("não pode ser vazio")
        return value


def check_structure(message: str | None, events: list[InputEvent] | None, context: list[ContextBlock] | None) -> None:
    """As regras entre campos. `ValueError` com a mensagem para quem chama."""
    if context:
        seen: set[str] = set()
        for block in context:
            if block.key in seen:
                raise ValueError(f"context: key {block.key!r} repetida (cada bloco tem a sua)")
            seen.add(block.key)
    if message is None and not any(event.role == "contact" for event in events or []):
        raise ValueError("informe message, ou events com ao menos um evento role='contact' (a fala nova)")


def is_structured(events: list[InputEvent] | None, context: list[ContextBlock] | None) -> bool:
    """Estruturada quando `events` ou `context` veio, mesmo vazio. Só `message` é o legado."""
    return events is not None or context is not None


def text_size(message: str | None, events: list[InputEvent] | None, context: list[ContextBlock] | None) -> int:
    """A soma dos textos mandados (fala, eventos, títulos e conteúdos dos blocos) —
    o que conta para `MAX_INPUT_CHARS`. Os rótulos do gabarito não entram."""
    total = len(message or "")
    total += sum(len(event.content) for event in events or [])
    total += sum(len(block.title) + len(block.content) for block in context or [])
    return total


def speech_index(message: str | None, events: list[InputEvent]) -> int | None:
    """Qual evento é a fala nova, se algum é.

    - Sem `message`: o último evento `contact`.
    - Com `message`: o último evento, se for `contact` com o mesmo texto (quem manda
      a fala nos dois lugares não a vê repetida no prompt). Senão nenhum: a fala veio
      só em `message`, depois de todos os eventos.

    Comparação de campo com campo, sem interpretar texto."""
    if message is None:
        return max(i for i, event in enumerate(events) if event.role == "contact")
    if events and events[-1].role == "contact" and events[-1].content.strip() == message.strip():
        return len(events) - 1
    return None


def pure_message(message: str | None, events: list[InputEvent]) -> str:
    """A fala nova, sem contexto nem histórico — o que vai para `runs.input_message`."""
    if message is not None:
        return message
    return events[speech_index(None, events)].content  # type: ignore[index]


def _at_text(at: datetime | None) -> str | None:
    if at is None:
        return None
    if at.tzinfo is not None:
        return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return at.strftime("%Y-%m-%dT%H:%M:%S")


def _attachment_text(attachment: EventAttachment) -> str:
    parts = [attachment.filename or "sem nome"]
    if attachment.mime_type:
        parts.append(attachment.mime_type)
    if attachment.url:
        parts.append(attachment.url)
    return ", ".join(parts)


def _event_text(event: InputEvent) -> str:
    header = [ROLE_LABELS[event.role]]
    if (at := _at_text(event.at)) is not None:
        header.append(at)
    if event.kind == "document":
        header.append("documento")
    lines = [f"[{' · '.join(header)}]"]
    if event.content.strip():
        lines.append(event.content.strip())
    lines.extend(f"(anexo: {_attachment_text(a)})" for a in event.attachments)
    return "\n".join(lines)


def _same_text(a: str, b: str) -> bool:
    """Iguais ignorando espaços nas pontas e diferenças de quebra de linha e espaço."""
    return " ".join(a.split()) == " ".join(b.split())


def echo_indices(events: list[InputEvent] | None, last_reply: str | None) -> set[int]:
    """Os eventos `agent_sent` cujo texto é a última resposta do agente na sessão.

    O sistema integrado manda o que de fato enviou em nome do agente; quando é o
    que o agente respondeu, o histórico da sessão já traz esse texto, e repeti-lo na
    mensagem só duplica o prompt. Diferente (a instituição editou, ou mandou outra
    coisa), o evento fica: é informação nova para o modelo."""
    if not last_reply or not last_reply.strip():
        return set()
    return {
        i
        for i, event in enumerate(events or [])
        if event.role == "agent_sent" and event.content.strip() and _same_text(event.content, last_reply)
    }


def build_turn_text(
    message: str | None, events: list[InputEvent] | None, echoes: set[int] | frozenset[int] = frozenset()
) -> str:
    """A mensagem do turno (e o que fica em `runs.message`): os eventos e a fala, sem
    o contexto, que vai no prompt de sistema. Seções vazias somem; os índices em
    `echoes` (ver `echo_indices`) não entram."""
    events = list(events or [])
    index = speech_index(message, events)
    kept = [(i, e) for i, e in enumerate(events) if i not in echoes and i != index]
    before = [e for i, e in kept if index is None or i < index]
    after_events = [e for i, e in kept if index is not None and i > index]
    speech = pure_message(message, events)

    sections: list[str] = []
    if before:
        sections.append(EVENTS_HEADING + "\n\n" + "\n\n".join(_event_text(e) for e in before))
    speech_lines = [speech.strip()] if speech.strip() else []
    if index is not None:
        speech_event = events[index]
        speech_lines.extend(f"(anexo: {_attachment_text(a)})" for a in speech_event.attachments)
    sections.append(MESSAGE_HEADING + "\n\n" + "\n".join(speech_lines))
    if after_events:
        sections.append(AFTER_HEADING + "\n\n" + "\n\n".join(_event_text(e) for e in after_events))
    return "\n\n".join(sections)


def render_context(blocks: list[dict[str, Any]] | None) -> str | None:
    """O contexto vigente como vai no prompt de sistema: o título, a frase fixa e um
    `## <title>` por bloco, na ordem. `blocks` no formato de `runs.input_context`
    (`context_for_storage`). Sem bloco, `None`: nada vai para o prompt."""
    rendered = [
        f"## {block.get('title') or block.get('key')}\n{str(block.get('content') or '').strip()}"
        for block in blocks or []
        if isinstance(block, dict)
    ]
    if not rendered:
        return None
    return CONTEXT_HEADING + "\n\n" + CONTEXT_NOTE + "\n\n" + "\n\n".join(rendered)


def events_for_storage(
    message: str | None, events: list[InputEvent] | None, echoes: set[int] | frozenset[int] = frozenset()
) -> list[dict[str, Any]]:
    """`runs.input_events`: todos os eventos, na ordem recebida, como JSON simples.
    `speech: true` marca o evento que é a fala nova (no máximo um); `echo: true`, o
    `agent_sent` que repetia a última resposta e por isso ficou fora do texto do modelo."""
    events = list(events or [])
    index = speech_index(message, events) if events else None
    stored = []
    for i, event in enumerate(events):
        item: dict[str, Any] = {
            "role": event.role,
            "content": event.content,
            "at": _at_text(event.at),
            "kind": event.kind,
            "attachments": [a.model_dump(mode="json", exclude_none=True) for a in event.attachments],
            "speech": i == index,
        }
        if i in echoes:
            item["echo"] = True
        stored.append(item)
    return stored


def content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def context_for_storage(context: list[ContextBlock] | None) -> list[dict[str, Any]]:
    """`runs.input_context`: cada bloco com o sha256 do `content` — comparar o hash
    entre turnos diz se o bloco mudou sem comparar o texto inteiro."""
    return [
        {
            "key": block.key,
            "title": block.title,
            "content": block.content,
            "volatile": block.volatile,
            "hash": content_hash(block.content),
        }
        for block in context or []
    ]
