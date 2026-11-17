const LOCALE = "pt-BR";

/** Aceita ISO string, Date ou epoch (segundos ou milissegundos — o AgentOS usa os dois). */
export function toDate(value: string | number | Date): Date {
  if (value instanceof Date) return value;
  if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value);
  return new Date(value);
}

const relativeFormatter = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" });

export function formatRelativeTime(value: string | number | Date, now = Date.now()): string {
  const diffSeconds = Math.round((toDate(value).getTime() - now) / 1000);
  const abs = Math.abs(diffSeconds);
  if (abs < 45) return "agora";
  if (abs < 3600) return relativeFormatter.format(Math.round(diffSeconds / 60), "minute");
  if (abs < 86400) return relativeFormatter.format(Math.round(diffSeconds / 3600), "hour");
  if (abs < 86400 * 7) return relativeFormatter.format(Math.round(diffSeconds / 86400), "day");
  return formatDate(value);
}

export function formatDate(value: string | number | Date): string {
  return new Intl.DateTimeFormat(LOCALE, { day: "2-digit", month: "short", year: "numeric" }).format(
    toDate(value)
  );
}

export function formatDateTime(value: string | number | Date): string {
  return new Intl.DateTimeFormat(LOCALE, { dateStyle: "short", timeStyle: "short" }).format(toDate(value));
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(LOCALE).format(value);
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toLocaleString(LOCALE, { maximumFractionDigits: 1 })} ${units[unit]}`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toLocaleString(LOCALE, { maximumFractionDigits: 1 })} s`;
}

export function formatMs(ms: number | null | undefined): string {
  return ms == null ? "—" : formatDuration(ms / 1000);
}

/** Custo em dólar; valores de fração de centavo (o comum por chamada) mantêm 2 dígitos significativos. */
export function formatCost(usd: number | null | undefined): string {
  if (usd == null) return "—";
  if (usd === 0) return "US$ 0";
  const digits = usd < 0.01 ? { maximumSignificantDigits: 2 } : { maximumFractionDigits: 2 };
  return `US$ ${usd.toLocaleString(LOCALE, digits)}`;
}

/** Agrupa itens por recência ("Hoje", "Ontem", ...), preservando a ordem de entrada. */
export function groupByRecency<T>(
  items: T[],
  getDate: (item: T) => string | number,
  now = new Date()
): { label: string; items: T[] }[] {
  const day = 86_400_000;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const buckets = [
    { label: "Hoje", from: startOfToday },
    { label: "Ontem", from: startOfToday - day },
    { label: "Últimos 7 dias", from: startOfToday - 7 * day },
    { label: "Últimos 30 dias", from: startOfToday - 30 * day },
    { label: "Mais antigas", from: -Infinity },
  ];
  const groups = buckets.map((bucket) => ({ label: bucket.label, items: [] as T[] }));
  for (const item of items) {
    const time = toDate(getDate(item)).getTime();
    groups[buckets.findIndex((bucket) => time >= bucket.from)].items.push(item);
  }
  return groups.filter((group) => group.items.length > 0);
}

/**
 * Data relativa que cabe num rodapé de cartão: "há 5 min", "há 3 dias", "há 2 meses".
 * Diferente de `formatRelativeTime`, nunca vira data por extenso ("07 de out. de 2026").
 */
export function formatRelativeShort(value: string | number | Date, now = Date.now()): string {
  const seconds = Math.round((now - toDate(value).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 45) return "agora";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `há ${minutes} min`;
  const hours = Math.round(seconds / 3600);
  if (hours < 24) return `há ${hours} h`;
  const days = Math.round(seconds / 86400);
  if (days === 1) return "ontem";
  if (days < 7) return `há ${days} dias`;
  if (days < 30) return `há ${Math.round(days / 7)} sem.`;
  if (days < 365) {
    const months = Math.max(1, Math.round(days / 30));
    return months === 1 ? "há 1 mês" : `há ${months} meses`;
  }
  const years = Math.round(days / 365);
  return years === 1 ? "há 1 ano" : `há ${years} anos`;
}

const ALNUM = /[\p{L}\p{N}]/gu;

/** As palavras de `value` só com letras e números; palavra sem nenhum dos dois ("—", "·") some. */
function alnumWords(value: string): string[] {
  return value
    .split(/[\s_-]+/)
    .map((word) => (word.match(ALNUM) ?? []).join(""))
    .filter(Boolean);
}

/**
 * Duas letras para o avatar. Separadores ("R6 — Agente") não contam como palavra; um código
 * com número no começo ("R6", "R8") vale sozinho, para os agentes de uma família não virarem
 * todos "RA". Quando o nome não dá duas letras, o slug (`fallback`) desempata.
 */
export function initials(name: string, fallback?: string): string {
  const words = alnumWords(name);
  const first = words[0] ?? "";
  if (/\p{N}/u.test(first) && first.length >= 2) return first.slice(0, 2).toUpperCase();
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (first.length >= 2) return first.slice(0, 2).toUpperCase();
  if (fallback && fallback !== name) {
    const fromSlug = initials(fallback);
    if (fromSlug !== "?") return fromSlug;
  }
  return first ? first.toUpperCase() : "?";
}

export function slugify(value: string): string {
  return value
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export function pluralize(count: number, singular: string, plural: string): string {
  return `${formatNumber(count)} ${count === 1 ? singular : plural}`;
}
