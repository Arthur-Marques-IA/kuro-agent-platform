/**
 * Lógica pura da busca global (`Ctrl+K`, `components/workspace/command-palette.tsx`):
 * reconhecer um id colado e filtrar/ordenar os resultados. Fica aqui, sem React
 * nem ícones, para os testes de unidade (`command-search.test.ts`).
 */

export type IdTarget = "run" | "session";

export interface DetectedId {
  /** O id como vai na URL: sem espaços em volta e, se for UUID, em minúsculas. */
  id: string;
  /** O que o id pode ser, na ordem em que as opções aparecem. */
  targets: IdTarget[];
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** `analyze-<32 hex>` (/analyze sem sessão), `cli-<12 hex>` (CLI), `mcp-<12 hex>` (servidor MCP). */
const PREFIXED_SESSION = /^(analyze|cli|mcp)-[0-9a-f]{6,64}$/i;

/**
 * Reconhece um `run_id` ou `session_id` colado na busca, pelo formato que o serviço gera:
 *
 * - UUID com hífens (`8-4-4-4-12`): o `run_id` é sempre um `uuid4`, e o Playground também
 *   cria a sessão com um UUID. Ambíguo, então devolve as duas opções (execução primeiro).
 * - `analyze-…`, `cli-…`, `mcp-…` seguidos de hex: sessão.
 * - Qualquer outra coisa: não é id. Um `session_id` livre, mandado por um sistema
 *   integrado, não tem formato — esse se acha na lista de sessões dos Logs.
 */
export function detectId(text: string): DetectedId | null {
  const value = text.trim();
  if (!value || /\s/.test(value)) return null;
  if (UUID.test(value)) return { id: value.toLowerCase(), targets: ["run", "session"] };
  if (PREFIXED_SESSION.test(value)) return { id: value, targets: ["session"] };
  return null;
}

export function normalize(text: string): string {
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

export interface SearchEntry {
  group: string;
  label: string;
  hint?: string;
  keywords?: string;
}

/**
 * Quanto o item casa com a busca (já normalizada): 0 = não casa. O nome pesa mais que
 * o resto — começar igual > uma palavra começar igual > conter; depois a dica (o
 * identificador técnico) e, por último, as palavras-chave e o grupo.
 */
export function matchScore(entry: SearchEntry, q: string): number {
  if (!q) return 1;
  const label = normalize(entry.label);
  const hint = normalize(entry.hint ?? "");
  if (label === q || hint === q) return 100;
  if (label.startsWith(q)) return 80;
  if (label.split(/[\s:·/_-]+/).some((word) => word.startsWith(q))) return 60;
  if (label.includes(q)) return 50;
  if (hint.startsWith(q)) return 40;
  if (hint.includes(q)) return 30;
  if (normalize(`${entry.keywords ?? ""} ${entry.group}`).includes(q)) return 10;
  return 0;
}

export interface FilterOptions {
  /** Grupos que só aparecem com algo digitado (ex.: tools e collections, que são muitos). */
  searchOnlyGroups?: readonly string[];
  /** Máximo de itens por grupo quando há busca; sem chave = sem limite. */
  limitPerGroup?: Readonly<Record<string, number>>;
  /** Sem busca, quantos itens destes grupos aparecem (ex.: só as 5 conversas mais recentes). */
  emptyQueryLimit?: Readonly<Record<string, number>>;
}

/**
 * Filtra e ordena os itens da busca. A ordem dos grupos é a da primeira aparição na
 * lista de entrada (quem monta a lista decide), e cada grupo fica contíguo — a paleta
 * põe o título do grupo quando ele muda. Dentro do grupo, o melhor casamento primeiro;
 * empate mantém a ordem de entrada.
 */
export function filterEntries<T extends SearchEntry>(items: readonly T[], query: string, options: FilterOptions = {}): T[] {
  const q = normalize(query.trim());
  const groupOrder: string[] = [];
  const byGroup = new Map<string, { item: T; score: number; index: number }[]>();
  items.forEach((item, index) => {
    if (!byGroup.has(item.group)) {
      byGroup.set(item.group, []);
      groupOrder.push(item.group);
    }
    if (!q && options.searchOnlyGroups?.includes(item.group)) return;
    const score = matchScore(item, q);
    if (score > 0) byGroup.get(item.group)!.push({ item, score, index });
  });

  return groupOrder.flatMap((group) => {
    const matches = byGroup.get(group)!;
    if (q) matches.sort((a, b) => b.score - a.score || a.index - b.index);
    const limit = q ? options.limitPerGroup?.[group] : options.emptyQueryLimit?.[group];
    return (limit === undefined ? matches : matches.slice(0, limit)).map((m) => m.item);
  });
}

/**
 * Quem o id colado é, pelo que `GET /observability/runs?search=<id>` devolveu: a busca
 * casa o `run_id` inteiro e trechos do `session_id`, então só vale igualdade exata.
 */
export function idLookup(
  id: string,
  runs: readonly { run_id?: string | null; session_id?: string | null }[]
): Record<IdTarget, boolean> {
  return {
    run: runs.some((r) => r.run_id === id),
    session: runs.some((r) => r.session_id === id),
  };
}
