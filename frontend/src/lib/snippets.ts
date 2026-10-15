/**
 * Gera exemplos de integração com o contrato estável do agent-service
 * (`POST /chat`, `POST /chat/stream` e `POST /analyze`) — são chamadas diretas
 * ao backend, como outro módulo da plataforma faria, não ao BFF deste frontend.
 *
 * O corpo vem pronto de `GET /agents/{tipo}/integration`: é o backend que sabe
 * qual endpoint serve o agente e quais `dependencies` ele declara. Montar isso
 * aqui foi o que deixou o console mostrando um exemplo de `/chat` para agente
 * analista, e sem o header de autenticação quando as chaves estão ligadas.
 *
 * Os consumidores de stream tratam `error`: um exemplo que só lê `message` engole
 * o 409/503/504 que chega como evento (o status HTTP do stream já saiu 200).
 */

export type SnippetLanguage = "curl" | "javascript" | "python" | "php";
export type ChatEndpoint = "/chat" | "/chat/stream" | "/analyze";

export interface SnippetOptions {
  baseUrl: string;
  endpoint: ChatEndpoint;
  /** Corpo da requisição, já montado pelo backend. */
  payload: Record<string, unknown>;
  /** O serviço exige chave: o exemplo precisa do header, senão não funciona. */
  requiresAuth?: boolean;
}

export const SNIPPET_LANGUAGES: { value: SnippetLanguage; label: string }[] = [
  { value: "curl", label: "cURL" },
  { value: "javascript", label: "JavaScript" },
  { value: "python", label: "Python" },
  { value: "php", label: "PHP" },
];

const AUTH_ENV = "$KURO_RUNTIME_API_KEY";

function indent(text: string, spaces: number): string {
  const pad = " ".repeat(spaces);
  return text
    .split("\n")
    .map((line, i) => (i === 0 ? line : pad + line))
    .join("\n");
}

function toPython(value: unknown, level = 0): string {
  const pad = "    ".repeat(level + 1);
  const closePad = "    ".repeat(level);
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return `[\n${value.map((v) => pad + toPython(v, level + 1)).join(",\n")},\n${closePad}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return "{}";
    return `{\n${entries
      .map(([k, v]) => `${pad}${JSON.stringify(k)}: ${toPython(v, level + 1)}`)
      .join(",\n")},\n${closePad}}`;
  }
  return JSON.stringify(String(value));
}

function curl(opts: SnippetOptions): string {
  const body = JSON.stringify(opts.payload, null, 2).replace(/'/g, "'\\''");
  const flags = opts.endpoint === "/chat/stream" ? "-N " : "";
  const auth = opts.requiresAuth ? `  -H "Authorization: Bearer ${AUTH_ENV}" \\\n` : "";
  return `curl ${flags}-X POST ${opts.baseUrl}${opts.endpoint} \\
  -H "Content-Type: application/json" \\
${auth}  -d '${body}'`;
}

function javascript(opts: SnippetOptions): string {
  const body = indent(JSON.stringify(opts.payload, null, 2), 2);
  const headers = opts.requiresAuth
    ? `{ "Content-Type": "application/json", Authorization: \`Bearer \${process.env.KURO_RUNTIME_API_KEY}\` }`
    : `{ "Content-Type": "application/json" }`;
  const request = `const response = await fetch("${opts.baseUrl}${opts.endpoint}", {
  method: "POST",
  headers: ${headers},
  body: JSON.stringify(${body}),
});`;
  const check = `if (!response.ok) throw new Error(\`Kuro respondeu \${response.status}: \${await response.text()}\`);`;

  if (opts.endpoint === "/analyze") {
    return `${request}
${check}

// Guarde run_id, agent_version e config_hash junto da decisão.
const { result, run_id, agent_version } = await response.json();
console.log(result, run_id, agent_version);`;
  }

  if (opts.endpoint === "/chat") {
    return `${request}
${check}

const { content, run_id, agent_version, sources } = await response.json();
console.log(content, run_id, agent_version, sources);`;
  }

  return `${request}
${check}

// SSE via POST: leia o stream manualmente (EventSource só faz GET).
const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer = (buffer + value).replaceAll("\\r\\n", "\\n");
  let index;
  while ((index = buffer.indexOf("\\n\\n")) !== -1) {
    const rawEvent = buffer.slice(0, index);
    buffer = buffer.slice(index + 2);
    const event = rawEvent.match(/^event: ?(.*)$/m)?.[1];
    const data = JSON.parse(rawEvent.match(/^data: ?(.*)$/m)?.[1] ?? "{}");
    if (event === "run") console.log("run_id:", data.run_id, "versão:", data.agent_version);
    if (event === "message") process.stdout.write(data.content);
    if (event === "state") console.log("\\nestado:", data); // só em agente procedural
    if (event === "sources") console.log("\\nfontes:", data.sources);
    if (event === "usage") console.log("\\ntokens:", data);
    // O status HTTP já saiu 200: falhas (409, 503, 504...) chegam como evento.
    if (event === "error") throw new Error(\`Kuro: \${data.message} (\${data.status ?? "erro"})\`);
  }
}`;
}

function python(opts: SnippetOptions): string {
  const payload = toPython(opts.payload);
  const headersArg = opts.requiresAuth
    ? `, headers={"Authorization": f"Bearer {os.environ['KURO_RUNTIME_API_KEY']}"}`
    : "";
  const osImport = opts.requiresAuth ? "import os\n" : "";

  if (opts.endpoint === "/analyze" || opts.endpoint === "/chat") {
    const read =
      opts.endpoint === "/analyze"
        ? `# Guarde run_id, agent_version e config_hash junto da decisão.
print(data["result"], data["run_id"], data["agent_version"])`
        : `print(data["content"], data["run_id"], data["agent_version"], data["sources"])`;
    return `${osImport}import requests

payload = ${payload}

response = requests.post("${opts.baseUrl}${opts.endpoint}", json=payload, timeout=120${headersArg})
response.raise_for_status()
data = response.json()
${read}`;
  }

  return `import json
${osImport}import httpx

payload = ${payload}

with httpx.stream("POST", "${opts.baseUrl}/chat/stream", json=payload, timeout=None${headersArg}) as response:
    response.raise_for_status()
    event = None
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
            continue
        if not line.startswith("data:"):
            continue
        data = json.loads(line.removeprefix("data:").strip() or "{}")
        if event == "run":
            print("run_id:", data["run_id"], "versão:", data.get("agent_version"))
        elif event == "message":
            print(data["content"], end="", flush=True)
        elif event == "state":  # só em agente procedural
            print("\\nestado:", data)
        elif event == "sources":
            print("\\nfontes:", data["sources"])
        elif event == "error":
            # O status HTTP já saiu 200: falhas (409, 503, 504...) chegam como evento.
            raise RuntimeError(f"Kuro: {data['message']} ({data.get('status', 'erro')})")`;
}

function php(opts: SnippetOptions): string {
  // Nowdoc: o JSON vai literal, sem o PHP interpretar `$` nem barras.
  const body = JSON.stringify(opts.payload, null, 2);
  const headers = opts.requiresAuth
    ? `["Content-Type: application/json", "Authorization: Bearer " . getenv("KURO_RUNTIME_API_KEY")]`
    : `["Content-Type: application/json"]`;
  const open = `<?php
$body = <<<'JSON'
${body}
JSON;

$ch = curl_init("${opts.baseUrl}${opts.endpoint}");`;

  if (opts.endpoint === "/analyze" || opts.endpoint === "/chat") {
    const read =
      opts.endpoint === "/analyze"
        ? `// Guarde run_id, agent_version e config_hash junto da decisão.
print_r($data["result"]);`
        : `echo $data["content"], PHP_EOL;`;
    return `${open}
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => ${headers},
    CURLOPT_POSTFIELDS => $body,
    CURLOPT_TIMEOUT => 120,
]);
$response = curl_exec($ch);
$status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
curl_close($ch);

if ($response === false || $status !== 200) {
    // Fallback: veja a tabela de erros (503 com Retry-After, 504, 502...).
    throw new RuntimeException("Kuro respondeu $status: $response");
}
$data = json_decode($response, true);
${read}
echo "run_id: {$data["run_id"]}, versão: {$data["agent_version"]}", PHP_EOL;`;
  }

  return `${open}
$buffer = "";
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => ${headers},
    CURLOPT_POSTFIELDS => $body,
    CURLOPT_TIMEOUT => 0,
    // SSE: cada pedaço chega aqui; um evento termina numa linha em branco.
    CURLOPT_WRITEFUNCTION => function ($ch, $chunk) use (&$buffer) {
        $buffer = str_replace("\\r\\n", "\\n", $buffer . $chunk);
        while (($pos = strpos($buffer, "\\n\\n")) !== false) {
            $raw = substr($buffer, 0, $pos);
            $buffer = substr($buffer, $pos + 2);
            $event = preg_match('/^event: ?(.*)$/m', $raw, $m) ? $m[1] : null;
            $data = preg_match('/^data: ?(.*)$/m', $raw, $m) ? json_decode($m[1], true) : [];
            if ($event === "run") echo "run_id: {$data["run_id"]}", PHP_EOL;
            if ($event === "message") echo $data["content"];
            if ($event === "state") print_r($data); // só em agente procedural
            if ($event === "sources") print_r($data["sources"]);
            // O status HTTP já saiu 200: falhas (409, 503, 504...) chegam como evento.
            if ($event === "error") fwrite(STDERR, "Kuro: {$data["message"]}" . PHP_EOL);
        }
        return strlen($chunk); // outro valor faz o cURL abortar
    },
]);
curl_exec($ch);
curl_close($ch);`;
}

export function buildSnippet(language: SnippetLanguage, opts: SnippetOptions): string {
  if (language === "javascript") return javascript(opts);
  if (language === "python") return python(opts);
  if (language === "php") return php(opts);
  return curl(opts);
}
