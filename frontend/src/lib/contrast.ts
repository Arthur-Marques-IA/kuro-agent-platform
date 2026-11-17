/**
 * Razão de contraste WCAG 2.x entre duas cores, para conferir os tokens de
 * `globals.css` (os testes em `contrast.test.ts` guardam os pares que importam:
 * texto secundário de 11–12 px sobre página, cartão e barra lateral, nos dois temas).
 */

export type Rgb = [number, number, number];

/** `#rgb` ou `#rrggbb` → [r, g, b] de 0 a 255. */
export function parseHex(hex: string): Rgb {
  const raw = hex.trim().replace(/^#/, "");
  const full = raw.length === 3 ? raw.split("").map((c) => c + c).join("") : raw;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`cor inválida: ${hex}`);
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as Rgb;
}

/** Cor com opacidade `alpha` (0–1) pintada sobre `background` — o que `text-x/70` vira na tela. */
export function blend(foreground: string | Rgb, background: string | Rgb, alpha: number): Rgb {
  const f = typeof foreground === "string" ? parseHex(foreground) : foreground;
  const b = typeof background === "string" ? parseHex(background) : background;
  return f.map((v, i) => v * alpha + b[i] * (1 - alpha)) as Rgb;
}

/** Luminância relativa (WCAG 2.x). */
export function relativeLuminance(color: string | Rgb): number {
  const rgb = typeof color === "string" ? parseHex(color) : color;
  const [r, g, b] = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Razão de contraste de 1 a 21. Texto normal (inclusive 11–12 px) pede 4,5:1 no AA. */
export function contrastRatio(a: string | Rgb, b: string | Rgb): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

export const AA_TEXT = 4.5;
