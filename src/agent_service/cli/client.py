"""Cliente HTTP fino da API do agent-service — uma função por endpoint.

Compartilhado pelos comandos da CLI e por um futuro TUI. Devolve os dicts
da resposta; erros viram `ApiError` (HTTP 4xx/5xx) ou `ServiceUnavailable`
(serviço fora do ar).
"""

import json
import mimetypes
import os
from collections.abc import Iterator
from typing import Any

import httpx

from agent_service.config import current_key
from agent_service.public_url import is_loopback


class ApiError(Exception):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class ServiceUnavailable(Exception):
    pass


class TlsError(ServiceUnavailable):
    """O serviço respondeu, mas o certificado não foi aceito.

    Separado de `ServiceUnavailable` porque a saída dos dois é oposta: ali a
    pergunta certa é "o serviço subiu?", aqui o serviço está de pé e o que falta
    é confiar na CA. Tratar os dois igual manda quem opera olhar o lugar errado."""


class PortBlocked(ServiceUnavailable):
    """A conexão ficou sem resposta até o timeout: um firewall descartando os pacotes.

    Também separado: "o serviço está no ar? (docker compose up -d)" manda olhar a máquina,
    e o serviço pode estar de pé atrás de um firewall que nem roda nela."""


def _transport_failure(base_url: str, exc: Exception) -> ServiceUnavailable:
    texto = str(exc)
    if "SSL" in texto.upper() or "CERTIFICATE" in texto.upper():
        return TlsError(
            f"o certificado de {base_url} não foi aceito: {texto.strip()}. "
            "Se ele vem de uma CA própria, aponte-a com KURO_CA_BUNDLE=/caminho/ca.pem "
            "(ou --ca-bundle). Para um teste local com certificado autoassinado, --insecure."
        )
    if isinstance(exc, httpx.ConnectTimeout):
        # Recusa (nada escutando) volta na hora; ficar sem resposta até o timeout é um
        # firewall descartando os pacotes — quase sempre o do painel do provedor, que
        # fica fora da máquina e não aparece no `ufw`.
        return PortBlocked(
            f"{base_url} não respondeu: a porta parece bloqueada por um firewall no caminho "
            "(o do painel do provedor da VPS, por exemplo). Libere a porta lá, ou conecte o MCP "
            "pelo SSH com `kuro connect usuario@servidor`, que não precisa de porta aberta."
        )
    return ServiceUnavailable(f"não consegui falar com {base_url} ({type(exc).__name__})")


class Client:
    def __init__(
        self,
        base_url: str,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        api_key: str | None = None,
        verify: bool | str = True,
    ):
        self.base_url = base_url.rstrip("/")
        # A chave vai no cliente, não em cada chamada: esquecer de passá-la em
        # um comando novo viraria um 401 sem explicação. No container o compose copia
        # `ADMIN_API_KEY` para `KURO_API_KEY`, e numa rotação ela é `nova,antiga`: vai a nova.
        api_key = current_key(api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        # CA que não existe: o httpx estoura um FileNotFoundError aqui mesmo, antes de
        # qualquer comando tratar o erro. Vira um TlsError na primeira chamada, com a saída
        # de "certificado não aceito" (sai com 3 na CLI, erro da tool no MCP).
        self._missing_ca = verify if isinstance(verify, str) and not os.path.exists(verify) else None
        if self._missing_ca:
            verify = True
        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport, headers=headers, verify=verify
        )

    def _check_ca(self) -> None:
        if self._missing_ca:
            raise TlsError(
                f"o arquivo da CA não existe: {self._missing_ca}. Confira o caminho em KURO_CA_BUNDLE "
                "(ou --ca-bundle); se o certificado nunca foi salvo, rode de novo o bloco de "
                "configuração que `kuro mcp-config` imprime no servidor."
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._check_ca()
        # Uma nova tentativa para falhas de transporte transitórias (port-forward do
        # Docker). POST só repete se nem chegou a conectar, para não duplicar criação.
        for attempt in (1, 2):
            try:
                response = self._http.request(method, path, **kwargs)
                break
            except httpx.TransportError as exc:
                retryable = method in ("GET", "PUT", "DELETE") or isinstance(exc, httpx.ConnectError)
                # Certificado recusado não melhora na segunda tentativa.
                falha = _transport_failure(self.base_url, exc)
                if attempt == 2 or not retryable or isinstance(falha, (TlsError, PortBlocked)):
                    raise falha from exc
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- saúde / config ----------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def observability_config(self) -> dict[str, Any]:
        return self._request("GET", "/observability/config")

    def python_tools_config(self) -> dict[str, Any]:
        return self._request("GET", "/tools/python-config")

    # -- agentes -------------------------------------------------------------

    def list_agents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/agents")

    def get_agent(self, agent_type: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_type}")

    def create_agent(self, body: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        return self._request("POST", "/agents", json=body, params={"dry_run": "true"} if dry_run else None)

    def update_agent(self, agent_type: str, changes: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """PUT parcial: só as chaves enviadas mudam (a API usa `exclude_unset`)."""
        return self._request("PUT", f"/agents/{agent_type}", json=changes, params={"dry_run": "true"} if dry_run else None)

    def delete_agent(self, agent_type: str) -> None:
        self._request("DELETE", f"/agents/{agent_type}")

    def agent_versions(self, agent_type: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/agents/{agent_type}/versions")

    def agent_revisions(self, agent_type: str) -> list[dict[str, Any]]:
        """Versões da configuração inteira (não só do prompt)."""
        return self._request("GET", f"/agents/{agent_type}/revisions")

    def agent_revision(self, agent_type: str, version: int) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_type}/revisions/{version}")

    def procedure_state(self, agent_type: str, session_id: str) -> dict[str, Any]:
        """Estado de uma conversa com um agente procedural (etapa, coletado, faltando)."""
        return self._request("GET", f"/agents/{agent_type}/procedures/{session_id}")

    def procedure_funnel(self, agent_type: str, *, include_dry_run: bool = False) -> dict[str, Any]:
        return self._request(
            "GET", f"/agents/{agent_type}/procedures", params={"include_dry_run": "true"} if include_dry_run else None
        )

    def promote_agent(self, agent_type: str, to: str) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_type}/promote", json={"to": to})

    def integration(self, agent_type: str) -> dict[str, Any]:
        """Contrato pronto pra quem vai chamar o agente de outro módulo."""
        # O endereço pelo qual esta CLI chegou só serve de exemplo se for de fora: de dentro
        # do container (ou pelo loopback de um túnel) quem sabe a URL pública é o servidor.
        params = None if is_loopback(self.base_url) else {"base_url": self.base_url}
        return self._request("GET", f"/agents/{agent_type}/integration", params=params)

    def send_feedback(self, agent_type: str, session_id: str, feedback: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/agents/{agent_type}/feedback", json={"session_id": session_id, "feedback": feedback}
        )

    def get_feedback(self, agent_type: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_type}/feedback")

    def replace_feedback(self, agent_type: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        """Substitui as regras à mão, sem passar pelo modelo."""
        return self._request("PUT", f"/agents/{agent_type}/feedback", json={"rules": rules})

    def clear_feedback(self, agent_type: str) -> None:
        self._request("DELETE", f"/agents/{agent_type}/feedback")

    def feedback_versions(self, agent_type: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/agents/{agent_type}/feedback/versions")

    def rollback_feedback(self, agent_type: str, version: int) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_type}/feedback/rollback/{version}")

    # -- chat ----------------------------------------------------------------

    def chat_stream(self, body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Consome o SSE de `POST /chat/stream`, rendendo `(evento, dados)`:
        `run`, `message`, `usage`, `error` e `done`."""
        self._check_ca()
        try:
            with self._http.stream("POST", "/chat/stream", json=body, timeout=httpx.Timeout(10.0, read=None)) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ApiError(response.status_code, _detail(response))
                yield from _parse_sse(response.iter_lines())
        except httpx.TransportError as exc:
            raise _transport_failure(self.base_url, exc) from exc

    def analyze(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/analyze", json=body)

    # -- tools ---------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tools")

    def get_tool(self, tool_name: str) -> dict[str, Any]:
        return self._request("GET", f"/tools/{tool_name}")

    def tool_catalog(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tools/catalog")

    def create_tool(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/tools", json=body)

    def update_tool(self, tool_name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """PUT parcial: campo ausente (ou `null`) fica como está. `kind` não muda."""
        return self._request("PUT", f"/tools/{tool_name}", json=changes)

    def delete_tool(self, tool_name: str) -> None:
        self._request("DELETE", f"/tools/{tool_name}")

    def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        function_name: str | None,
        dependencies: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/tools/{tool_name}/invoke",
            json={
                "arguments": arguments,
                "function_name": function_name,
                "dependencies": dependencies or {},
                "dry_run": dry_run,
            },
        )

    # -- provedores e credenciais de modelo ----------------------------------

    def list_providers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/model-providers")

    def provider_models(self, provider: str, **params: Any) -> dict[str, Any]:
        """Modelos que o provedor oferece agora (lidos da API dele)."""
        return self._request(
            "GET", f"/model-providers/{provider}/models", params={k: v for k, v in params.items() if v is not None}
        )

    def list_credentials(self) -> list[dict[str, Any]]:
        return self._request("GET", "/model-credentials")

    def create_credential(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/model-credentials", json=body)

    def update_credential(self, credential_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/model-credentials/{credential_id}", json=body)

    def delete_credential(self, credential_id: str) -> None:
        self._request("DELETE", f"/model-credentials/{credential_id}")

    def test_credential(self, credential_id: str) -> dict[str, Any]:
        """Testa a chave salva (sem gastar tokens) e grava o resultado."""
        return self._request("POST", f"/model-credentials/{credential_id}/test", json={})

    # -- collections (bases de conhecimento) ---------------------------------

    def list_collections(self) -> list[dict[str, Any]]:
        return self._request("GET", "/collections")

    def list_embedders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/collections/embedders")

    def create_collection(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/collections", json=body)

    def get_collection(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/collections/{name}")

    def update_collection(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/collections/{name}", json=changes)

    def delete_collection(self, name: str) -> None:
        self._request("DELETE", f"/collections/{name}")

    def add_document(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/collections/{name}/documents", json=body)

    def add_collection_file(self, name: str, *, filename: str, content: bytes, title: str | None) -> dict[str, Any]:
        """Upload de arquivo para a collection (multipart) — PDF, DOCX, CSV...

        O MIME vai junto porque é dele que o Agno escolhe o leitor do arquivo;
        sem ele um PDF seria lido como texto."""
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return self._request(
            "POST",
            f"/collections/{name}/files",
            files={"file": (filename, content, mime)},
            data={"name": title} if title else None,
        )

    def list_collection_documents(self, name: str, limit: int = 100) -> dict[str, Any]:
        """`{data: [...], meta: {...}}` — a mesma forma que o console consome."""
        return self._request("GET", f"/collections/{name}/documents", params={"limit": limit})

    def delete_collection_document(self, name: str, content_id: str) -> None:
        self._request("DELETE", f"/collections/{name}/documents/{content_id}")

    def search_collection(self, name: str, query: str, limit: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/collections/{name}/search", params={"query": query, "limit": limit})

    # -- fontes de site (documents/web_sync.py) ------------------------------

    def list_sources(self, name: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/collections/{name}/sources")

    def create_source(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/collections/{name}/sources", json=payload)

    def get_source(self, name: str, source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/collections/{name}/sources/{source_id}")

    def update_source(self, name: str, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/collections/{name}/sources/{source_id}", json=payload)

    def sync_source(self, name: str, source_id: str) -> dict[str, Any]:
        return self._request("POST", f"/collections/{name}/sources/{source_id}/sync")

    def delete_source(self, name: str, source_id: str, keep_documents: bool = False) -> None:
        self._request(
            "DELETE",
            f"/collections/{name}/sources/{source_id}",
            params={"keep_documents": str(keep_documents).lower()},
        )

    # -- observabilidade -----------------------------------------------------

    def list_runs(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/observability/runs", params={k: v for k, v in params.items() if v is not None})

    def save_reference(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/observability/references", json=body)

    def agreement(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/observability/agreement", params={k: v for k, v in params.items() if v is not None})

    def export_cases(self, **params: Any) -> list[dict[str, Any]]:
        return self._request("GET", "/observability/export", params={k: v for k, v in params.items() if v is not None})

    def overview(self, **params: Any) -> dict[str, Any]:
        """Panorama do dashboard: totais × período anterior, série, agentes, versões
        e tools falhando. Exige escopo admin."""
        return self._request("GET", "/observability/overview", params={k: v for k, v in params.items() if v is not None})

    def run_trace(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/observability/runs/{run_id}/trace")

    def run_sessions(self, **params: Any) -> dict[str, Any]:
        """Execuções agrupadas por sessão (trace store) — não confundir com
        `list_sessions`, que é a conversa no Postgres."""
        return self._request(
            "GET", "/observability/sessions", params={k: v for k, v in params.items() if v is not None}
        )

    def run_stats(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/observability/stats", params={k: v for k, v in params.items() if v is not None})

    def score(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/observability/scores", json=body)

    # -- sessões (rotas do AgentOS, no Postgres) -----------------------------
    #
    # Não confundir com `/observability/sessions`: aquilo é um agrupamento das
    # execuções registradas; isto é a conversa em si — é o que `kuro sessions` precisa.

    def list_sessions(self, **params: Any) -> dict[str, Any]:
        """`{data: [...], meta: {...}}` — sempre `type=agent`, o único que o serviço cria.
        O `session_name` vem limpo (ver `clean_session_name`)."""
        query = {"type": "agent", **{k: v for k, v in params.items() if v is not None}}
        page = self._request("GET", "/sessions", params=query)
        for item in page.get("data") or []:
            if isinstance(item, dict) and "session_name" in item:
                item["session_name"] = clean_session_name(item["session_name"])
        return page

    def session_runs(self, session_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        params = {"type": "agent", **({"user_id": user_id} if user_id else {})}
        return self._request("GET", f"/sessions/{session_id}/runs", params=params)

    def rename_session(self, session_id: str, name: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/sessions/{session_id}/rename", params={"type": "agent"}, json={"session_name": name}
        )

    def delete_session(self, session_id: str, user_id: str | None = None) -> None:
        self._request("DELETE", f"/sessions/{session_id}", params={"user_id": user_id} if user_id else {})

    # -- fila de jobs -------------------------------------------------------------

    def list_jobs(self, *, status: str | None = None, kind: str | None = None, limit: int = 20) -> dict[str, Any]:
        params = {k: v for k, v in {"status": status, "kind": kind, "limit": limit}.items() if v is not None}
        return self._request("GET", "/jobs", params=params)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/retry")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/cancel")


_CONTEXT_MARK = "<additional context>"


def clean_session_name(name: Any) -> Any:
    """O título de uma conversa sem o contexto interno.

    Sem nome definido, o AgentOS usa a primeira mensagem do usuário como título —
    e o Agno junta a ela as `dependencies` (`<additional context>{...}`). O título
    de qualquer conversa com dados do cliente (CPF, nome) saía com eles inteiros.
    O mesmo corte está em `frontend/src/lib/sessions.ts::sessionTitle`."""
    if not isinstance(name, str) or _CONTEXT_MARK not in name:
        return name
    return name.split(_CONTEXT_MARK, 1)[0].strip() or None


def _detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    return payload.get("detail", payload) if isinstance(payload, dict) else payload


def _parse_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    event, data = "message", []
    for line in lines:
        if not line:
            if data:
                yield event, _loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, _loads("\n".join(data))


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}
