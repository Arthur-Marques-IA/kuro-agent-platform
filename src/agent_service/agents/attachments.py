"""Anexos multimodais de `/chat` e `/analyze` — imagem, áudio, vídeo ou
arquivo (PDF, DOCX, CSV, TXT...), transportados como base64 dentro do JSON da
requisição (o projeto inteiro é JSON-only; nenhuma rota usa multipart), e
convertidos pras classes de mídia do Agno (`agno.media`) que `Agent.arun`
já aceita nativamente.
"""

import base64
import binascii
import mimetypes
from typing import Any

from agno.media import Audio, File, Image, Video
from pydantic import BaseModel, ValidationError, model_validator

from agent_service.config import get_settings


class AttachmentError(ValueError):
    """Anexo inválido — 422 na rota, mesmo padrão de DependencyValidationError."""


class AttachmentIn(BaseModel):
    content_base64: str | None = None
    url: str | None = None
    mime_type: str | None = None
    filename: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "AttachmentIn":
        if bool(self.content_base64) == bool(self.url):
            raise ValueError("informe exatamente um de content_base64 ou url")
        return self


def _guess_mime(attachment: AttachmentIn) -> str | None:
    if attachment.mime_type:
        return attachment.mime_type
    if attachment.filename:
        return mimetypes.guess_type(attachment.filename)[0]
    return None


def _check_size(attachment: AttachmentIn) -> None:
    if attachment.content_base64 is None:
        return
    # Estimativa sem decodificar: base64 expande ~4/3 o tamanho original.
    approx_bytes = len(attachment.content_base64) * 3 // 4
    limit = get_settings().max_attachment_mb * 1024 * 1024
    if approx_bytes > limit:
        raise AttachmentError(
            f"anexo {attachment.filename or ''!r} excede o limite de {get_settings().max_attachment_mb}MB"
        )


def _media_kwargs(attachment: AttachmentIn) -> dict[str, Any]:
    if attachment.url:
        return {"url": attachment.url}
    try:
        return {"content": base64.b64decode(attachment.content_base64, validate=True)}  # type: ignore[arg-type]
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(f"anexo {attachment.filename or ''!r}: content_base64 não é base64 válido") from exc


def describe_attachments(attachments: list[AttachmentIn]) -> list[dict[str, Any]]:
    """O que o registro da execução guarda de cada anexo: nome, tipo, tamanho e,
    se veio por URL, a URL — **nunca o conteúdo**. É o que o console mostra como
    "📎 contrato.pdf" na mensagem, no chat e nos Logs."""
    described = []
    for attachment in attachments:
        mime = _guess_mime(attachment)
        kind = next(
            (
                k
                for prefix, k in (("image/", "image"), ("audio/", "audio"), ("video/", "video"))
                if (mime or "").startswith(prefix)
            ),
            "file",
        )
        item: dict[str, Any] = {"filename": attachment.filename, "mime_type": mime, "kind": kind}
        if attachment.content_base64 is not None:
            item["size_bytes"] = len(attachment.content_base64) * 3 // 4
        if attachment.url:
            item["url"] = attachment.url
        described.append(item)
    return described


def build_media(attachments: list[AttachmentIn]) -> dict[str, list[Any]]:
    """Bucketiza os anexos em `{"images": [...], "audio": [...], "videos": [...],
    "files": [...]}`, pelo prefixo do mime type — o formato que `Agent.arun`
    espera em `images=`/`audio=`/`videos=`/`files=`."""
    result: dict[str, list[Any]] = {"images": [], "audio": [], "videos": [], "files": []}
    for attachment in attachments:
        _check_size(attachment)
        mime = _guess_mime(attachment)
        kwargs = _media_kwargs(attachment)

        try:
            if mime and mime.startswith("image/"):
                result["images"].append(Image(mime_type=mime, **kwargs))
            elif mime and mime.startswith("audio/"):
                result["audio"].append(Audio(mime_type=mime, **kwargs))
            elif mime and mime.startswith("video/"):
                result["videos"].append(Video(mime_type=mime, **kwargs))
            else:
                result["files"].append(File(mime_type=mime, filename=attachment.filename, **kwargs))
        except ValidationError as exc:
            raise AttachmentError(f"anexo {attachment.filename or ''!r} inválido (mime_type={mime!r}): {exc}") from exc
    return result
