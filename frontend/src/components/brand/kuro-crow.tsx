import { cn } from "@/lib/cn";

// O mesmo corvo do `kuro dash` (src/agent_service/tui/crow.py): a grade e as cores
// precisam ficar iguais às de lá — tests/test_tui.py confere.
export const GRID = [
  "................",
  ".........KKK....",
  "........KKKKK...",
  "........KKEKKBB.",
  "........KKKKKBBB",
  ".......KKKKK....",
  "....KKKKKKKK....",
  "..KKKHHHKKKK....",
  "KKKHHHHHKKK.....",
  "KK..KKKKKK......",
  ".....F..F.......",
  ".....FF.FF......",
];

export const COLORS: Record<string, string> = {
  K: "#2a3442",
  H: "#4a5a6e",
  B: "#8b99ab",
  F: "#6b7787",
  E: "#22d3ee",
};

// Um <rect> por trecho contínuo da mesma cor numa linha, em vez de um por pixel.
const RECTS = GRID.flatMap((row, y) => {
  const runs: { x: number; y: number; width: number; fill: string }[] = [];
  for (let x = 0; x < row.length; ) {
    const cell = row[x];
    let end = x + 1;
    while (end < row.length && row[end] === cell) end++;
    if (cell in COLORS) runs.push({ x, y, width: end - x, fill: COLORS[cell] });
    x = end;
  }
  return runs;
});

/** O mascote do Kuro. `scale` é quantos pixels de tela cada pixel do sprite ocupa. */
export function KuroCrow({ scale = 2, className, title }: { scale?: number; className?: string; title?: string }) {
  return (
    <svg
      viewBox="0 0 16 12"
      width={16 * scale}
      height={12 * scale}
      shapeRendering="crispEdges"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      className={cn("shrink-0", className)}
    >
      {RECTS.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.width} height={1} fill={r.fill} />
      ))}
    </svg>
  );
}
