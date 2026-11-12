/**
 * O corpo do `/chat` do Playground (com e sem entrada estruturada), a validação dos
 * blocos de contexto e a conversa reaberta pela linha do tempo. `npm test`.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  applyTimeline,
  buildChatBody,
  contextToSend,
  emptyBlock,
  eventsBefore,
  humanAgentEvent,
  parseContextBlocks,
  parseSentContext,
  serializeContextBlocks,
  sessionNameFromSpeech,
  validateContextBlocks,
} from "./chat-request.ts";
import type { ChatMessage, TimelineTurn } from "./types";

const base = { agentType: "suporte", userId: "u1", sessionId: "s1", message: "oi" };

describe("buildChatBody", () => {
  it("sem blocos nem eventos, o corpo é o de antes (sem events vazio nem context)", () => {
    const body = JSON.parse(
      JSON.stringify(buildChatBody({ ...base, dependencies: { cpf: "1" }, context: undefined, events: [] }))
    );
    assert.deepEqual(body, {
      agent_type: "suporte",
      user_id: "u1",
      session_id: "s1",
      message: "oi",
      dependencies: { cpf: "1" },
    });
  });

  it("só message, sem dependencies, anexos nem dry_run", () => {
    assert.deepEqual(JSON.parse(JSON.stringify(buildChatBody(base))), {
      agent_type: "suporte",
      user_id: "u1",
      session_id: "s1",
      message: "oi",
    });
  });

  it("com blocos e eventos, os dois vão no corpo e message continua sendo a fala", () => {
    const context = [{ key: "aluno", title: "Dados do aluno", content: "Maria" }];
    const events = [humanAgentEvent("Maria, recebi seus dados", new Date("2026-11-11T12:00:00Z"))];
    const body = JSON.parse(JSON.stringify(buildChatBody({ ...base, dryRun: true, context, events })));
    assert.equal(body.message, "oi");
    assert.equal(body.dry_run, true);
    assert.deepEqual(body.context, context);
    assert.deepEqual(body.events, [
      { role: "human_agent", content: "Maria, recebi seus dados", at: "2026-11-11T12:00:00.000Z" },
    ]);
  });
});

describe("contextToSend", () => {
  const dados = { key: "aluno", title: "Dados do aluno", content: "Maria" };
  const divida = { key: "divida", title: "Dívida", content: "SEM parcela vencida", volatile: true };

  it("primeiro turno: manda os blocos; sem blocos, nada", () => {
    assert.deepEqual(contextToSend([dados], null), [dados]);
    assert.equal(contextToSend([], null), undefined);
  });

  it("blocos iguais ao último envio não vão de novo (o servidor herda)", () => {
    assert.equal(contextToSend([{ ...dados }, { ...divida }], [dados, divida]), undefined);
  });

  it("qualquer mudança manda a lista inteira", () => {
    const mudou = { ...divida, content: "1 parcela vencida" };
    assert.deepEqual(contextToSend([dados, mudou], [dados, divida]), [dados, mudou]);
    assert.deepEqual(contextToSend([divida, dados], [dados, divida]), [divida, dados], "a ordem conta");
    assert.deepEqual(contextToSend([dados, { ...divida, volatile: undefined }], [dados, divida]), [
      dados,
      { ...divida, volatile: undefined },
    ]);
  });

  it("apagou todos os blocos de uma conversa que tinha contexto: manda [] para limpar", () => {
    assert.deepEqual(contextToSend([], [dados]), []);
    assert.equal(contextToSend([], []), undefined, "já estava limpo");
  });

  it("[] vai no corpo; undefined fica de fora", () => {
    assert.deepEqual(JSON.parse(JSON.stringify(buildChatBody({ ...base, context: [] }))).context, []);
    assert.equal("context" in JSON.parse(JSON.stringify(buildChatBody({ ...base, context: undefined }))), false);
  });

  it("o último envio guardado volta igual; vazio ou inválido é 'nunca mandou'", () => {
    assert.deepEqual(parseSentContext(JSON.stringify([dados, divida])), [dados, divida]);
    assert.deepEqual(parseSentContext("[]"), []);
    assert.equal(parseSentContext(""), null);
    assert.equal(parseSentContext("{"), null);
    assert.equal(parseSentContext(null), null);
  });
});

describe("validateContextBlocks", () => {
  it("apara key e title, mantém o content e só manda volatile quando marcado", () => {
    const result = validateContextBlocks([
      { key: " aluno ", title: " Dados ", content: " Maria ", volatile: false },
      { key: "agora", title: "Contexto temporal", content: "", volatile: true },
    ]);
    assert.equal(result.error, null);
    assert.deepEqual(result.blocks, [
      { key: "aluno", title: "Dados", content: " Maria " },
      { key: "agora", title: "Contexto temporal", content: "", volatile: true },
    ]);
    assert.deepEqual(result.errors, [null, null]);
  });

  it("ignora linha toda em branco", () => {
    const result = validateContextBlocks([emptyBlock()]);
    assert.equal(result.error, null);
    assert.deepEqual(result.blocks, []);
  });

  it("aponta key vazia, key repetida e título vazio, campo a campo", () => {
    const result = validateContextBlocks([
      { key: "aluno", title: "A", content: "x", volatile: false },
      { key: "aluno", title: "B", content: "y", volatile: false },
      { key: "", title: "C", content: "z", volatile: false },
      { key: "d", title: "  ", content: "w", volatile: false },
    ]);
    assert.equal(result.errors[0], null);
    assert.match(result.errors[1]?.key ?? "", /repetida/);
    assert.match(result.errors[2]?.key ?? "", /key/);
    assert.match(result.errors[3]?.title ?? "", /título/);
    assert.equal(result.error, "Bloco 2: Key repetida (bloco 1).");
    assert.deepEqual(result.blocks.map((b) => b.key), ["aluno"]);
  });

  it("limites do servidor: key até 64, title até 200, até 30 blocos", () => {
    const long = validateContextBlocks([{ key: "k".repeat(65), title: "t".repeat(201), content: "", volatile: false }]);
    assert.ok(long.errors[0]?.key && long.errors[0]?.title);
    const many = validateContextBlocks(
      Array.from({ length: 31 }, (_, i) => ({ key: `k${i}`, title: "t", content: "", volatile: false }))
    );
    assert.match(many.error ?? "", /30 blocos/);
  });
});

describe("armazenamento dos blocos", () => {
  it("ida e volta pelo texto do localStorage; lixo vira lista vazia", () => {
    const blocks = [{ key: "a", title: "A", content: "x", volatile: true }];
    assert.deepEqual(parseContextBlocks(serializeContextBlocks(blocks)), blocks);
    assert.deepEqual(parseContextBlocks("não é json"), []);
    assert.deepEqual(parseContextBlocks('{"key": "a"}'), []);
    assert.deepEqual(parseContextBlocks('[{"key": 1, "title": "T"}]'), [
      { key: "", title: "T", content: "", volatile: false },
    ]);
  });
});

function turn(partial: Partial<TimelineTurn>): TimelineTurn {
  return {
    run_id: "r1",
    at: "2026-11-11T12:00:00Z",
    agent_type: "suporte",
    status: "COMPLETED" as TimelineTurn["status"],
    dry_run: true,
    structured: true,
    events: [],
    context: [],
    steps: [],
    sources: [],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    ...partial,
  };
}

describe("applyTimeline", () => {
  const fromRuns: ChatMessage[] = [
    { id: "r1:user", role: "user", content: "# Contexto\n\n## Dados\nMaria\n\n# Mensagem nova do contato\n\noi" },
    { id: "r1:assistant", role: "assistant", content: "Olá", runId: "r1" },
    { id: "r2:user", role: "user", content: "legado" },
    { id: "r2:assistant", role: "assistant", content: "ok", runId: "r2" },
  ];

  it("turno estruturado: a bolha é só a fala, e o atendente aparece antes dela", () => {
    const result = applyTimeline(fromRuns, [
      turn({
        run_id: "r1",
        message: "oi",
        events: [
          { role: "human_agent", content: "Recebi seus dados", at: "2026-11-11T11:59:00Z" },
          { role: "contact", content: "oi", speech: true },
        ],
      }),
      turn({ run_id: "r2", structured: false, raw_input: "legado" }),
    ]);
    assert.deepEqual(
      result.map((m) => [m.role, m.content]),
      [
        ["human_agent", "Recebi seus dados"],
        ["user", "oi"],
        ["assistant", "Olá"],
        ["user", "legado"],
        ["assistant", "ok"],
      ]
    );
    assert.equal(result[0].createdAt, Date.parse("2026-11-11T11:59:00Z"));
  });

  it("sem turno estruturado, nada muda", () => {
    assert.equal(applyTimeline(fromRuns, [turn({ structured: false })]), fromRuns);
  });
});

describe("sessionNameFromSpeech", () => {
  it("a fala numa linha, cortada com reticências", () => {
    assert.equal(sessionNameFromSpeech("  boa tarde,\n me envia  o comprovante "), "boa tarde, me envia o comprovante");
    assert.equal(sessionNameFromSpeech("a".repeat(100), 10), `${"a".repeat(9)}…`);
  });
});

describe("eventsBefore", () => {
  it("os atendentes logo antes da fala voltam como eventos (para tentar de novo)", () => {
    const at = Date.parse("2026-11-11T12:00:00Z");
    const messages: ChatMessage[] = [
      { id: "a", role: "assistant", content: "x" },
      { id: "h", role: "human_agent", content: "Recebi", createdAt: at },
      { id: "u", role: "user", content: "oi" },
    ];
    assert.deepEqual(eventsBefore(messages, 2), [
      { role: "human_agent", content: "Recebi", at: "2026-11-11T12:00:00.000Z" },
    ]);
    assert.deepEqual(eventsBefore(messages, 1), []);
  });
});
