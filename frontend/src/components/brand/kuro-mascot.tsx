"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { Minus } from "lucide-react";
import { cn } from "@/lib/cn";
import { apiUrl, readErrorMessage } from "@/lib/http";
import {
  type KuroEvent,
  type KuroFeed,
  type KuroMood,
  balloonEvent,
  feedFailure,
  feedRuns,
  initialFeed,
  moodOf,
  nextPollDelay,
  statusLine,
} from "@/lib/kuro-mood";
import type { RunPage } from "@/lib/types";
import { useLocalStorage } from "@/lib/use-local-storage";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { COLORS, GRID } from "@/components/brand/kuro-crow";

/*
 * O corvo do `kuro dash` (src/agent_service/tui/crow.py) no canto do console: os mesmos
 * quadros e o mesmo repertório. O humor sai de `lib/kuro-mood.ts`; aqui só o desenho, o
 * relógio da animação e a consulta repetida de /observability/runs.
 */

type Grid = readonly string[];

interface Frame {
  grid: Grid;
  /** Cor do olho; `null` = fechado. */
  eye: string | null;
  dead?: boolean;
  /** Até 3 caracteres no alto, à direita da cabeça (o "zZ" do sono). */
  zz?: string;
}

const EYE_OK = COLORS.E;
const EYE_ALERT = "#ef4444";
const MOUTH = "#7f1d1d";
const GLYPH = "#94a3b8";

function withRows(grid: Grid, rows: Record<number, string>): Grid {
  return grid.map((row, i) => rows[i] ?? row);
}

const BASE: Grid = GRID;
const WINGS_UP = withRows(BASE, {
  5: "...HH..KKKKK....",
  6: "..HHHHKKKKKK....",
  7: "...KHHHHKKKK....",
  8: "KKKKKHHKKKK.....",
});
const LOOK_BACK = withRows(BASE, { 3: "......BBKEKKK...", 4: ".....BBBKKKKK..." });
const PECK: Grid = [
  "................",
  "................",
  "................",
  "................",
  ".........KKK....",
  ".......KKKKKK...",
  "....KKKKKKEKK...",
  "..KKKHHHKKKKKB..",
  "KKKHHHHHKKK..BB.",
  "KK..KKKKKK....B.",
  ".....F..F.......",
  ".....FF.FF......",
];
// No ar: tudo um pixel acima e os dedos recolhidos.
const HOP: Grid = [...BASE.slice(1, 10), ".....F..F.......", "................", "................"];
const DEAD: Grid = [...BASE].reverse();

/** Grasnando: o bico de cima segue reto, o de baixo cai e mostra a boca (R). */
function openBeak(grid: Grid): Grid {
  return withRows(grid, { 3: "........KKEKKBBB", 4: "........KKKKKR..", 5: grid[5].slice(0, 12) + "BBB." });
}

const SNORE = ["z  ", "zZ ", "zZz", " Zz", "  z"];

const STILL: Frame = { grid: BASE, eye: EYE_OK };

// Repertório do ocioso: [peso, quadros]. Cada quadro dura um tique.
const IDLE_ACTIONS: [number, Frame[]][] = [
  [4, [{ grid: BASE, eye: null }]],
  [2, Array<Frame>(5).fill({ grid: LOOK_BACK, eye: EYE_OK })],
  [2, [{ grid: PECK, eye: EYE_OK }, STILL, { grid: PECK, eye: EYE_OK }, STILL]],
  [2, [{ grid: HOP, eye: EYE_OK }, STILL, { grid: HOP, eye: EYE_OK }, STILL]],
];

const TICK_MS = 300;

function frameFor(mood: KuroMood, tick: number, animate: boolean): Frame {
  const flap = !animate || tick % 2 ? WINGS_UP : BASE;
  if (mood === "working") return { grid: flap, eye: EYE_OK };
  if (mood === "error") return { grid: openBeak(flap), eye: EYE_ALERT };
  if (mood === "paused") return { grid: BASE, eye: null, zz: animate ? SNORE[tick % SNORE.length] : "zZ " };
  if (mood === "offline") return { grid: DEAD, eye: null, dead: true };
  return STILL;
}

/** O relógio do corvo (`Crow.next_frame` da TUI): sorteia as ações do ocioso entre descansos. */
class CrowClock {
  tick = 0;
  mood: KuroMood = "idle";
  queue: Frame[] = [];
  rest = CrowClock.restTicks();

  static restTicks() {
    return 13 + Math.floor(Math.random() * 14); // 4 a 8 s parado entre uma ação e outra
  }

  next(mood: KuroMood, animate: boolean): Frame {
    if (mood !== this.mood) {
      this.mood = mood;
      this.queue = [];
      this.rest = CrowClock.restTicks();
    }
    this.tick += 1;
    if (mood !== "idle" || !animate) {
      this.queue = [];
      return frameFor(mood, this.tick, animate);
    }
    if (this.queue.length === 0) {
      this.rest -= 1;
      if (this.rest > 0) return STILL;
      const total = IDLE_ACTIONS.reduce((sum, [weight]) => sum + weight, 0);
      let pick = Math.random() * total;
      const action = IDLE_ACTIONS.find(([weight]) => (pick -= weight) < 0) ?? IDLE_ACTIONS[0];
      this.queue = [...action[1]];
      this.rest = CrowClock.restTicks();
    }
    return this.queue.shift() ?? STILL;
  }
}

function frameKey(frame: Frame) {
  return `${frame.grid.join("")}|${frame.eye}|${frame.dead ? 1 : 0}|${frame.zz ?? ""}`;
}

function CrowSprite({ frame, scale }: { frame: Frame; scale: number }) {
  const color = (pixel: string) => {
    if (pixel === "E") return frame.dead ? COLORS.K : (frame.eye ?? COLORS.H); // fechado: a cor do brilho
    if (pixel === "R") return MOUTH;
    return COLORS[pixel];
  };
  const rects = frame.grid.flatMap((row, y) => {
    const runs: { x: number; y: number; width: number; fill: string }[] = [];
    for (let x = 0; x < row.length; ) {
      const fill = color(row[x]);
      let end = x + 1;
      while (end < row.length && color(row[end]) === fill) end++;
      if (fill) runs.push({ x, y, width: end - x, fill });
      x = end;
    }
    return runs;
  });
  const eyeRow = frame.grid.findIndex((row) => row.includes("E"));
  const eyeCol = eyeRow >= 0 ? frame.grid[eyeRow].indexOf("E") : -1;
  return (
    <svg
      viewBox="0 0 16 12"
      width={16 * scale}
      height={12 * scale}
      shapeRendering="crispEdges"
      aria-hidden
      className="block shrink-0 overflow-visible"
    >
      {rects.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.width} height={1} fill={r.fill} />
      ))}
      {frame.dead && eyeRow >= 0 && (
        <text x={eyeCol + 0.5} y={eyeRow + 0.85} fontSize={1.6} textAnchor="middle" fill={GLYPH} fontWeight={700}>
          ×
        </text>
      )}
      {frame.zz &&
        [...frame.zz].map((char, i) =>
          char.trim() ? (
            <text key={i} x={13.5 + i} y={1.9} fontSize={2.2} textAnchor="middle" fill={GLYPH} fontWeight={700}>
              {char}
            </text>
          ) : null
        )}
    </svg>
  );
}

// -- preferências do navegador ------------------------------------------------------

function subscribeReducedMotion(callback: () => void) {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function usePrefersReducedMotion() {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    () => true // no servidor e na hidratação: parado
  );
}

function subscribeVisibility(callback: () => void) {
  document.addEventListener("visibilitychange", callback);
  return () => document.removeEventListener("visibilitychange", callback);
}

function useDocumentHidden() {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState === "hidden",
    () => false
  );
}

// O corvo só aparece a partir de `xl` (1280 px): abaixo disso não consulta nem anima.
const WIDE = "(min-width: 80rem)";

function subscribeWide(callback: () => void) {
  const query = window.matchMedia(WIDE);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function useWideScreen() {
  return useSyncExternalStore(
    subscribeWide,
    () => window.matchMedia(WIDE).matches,
    () => false
  );
}

function clock(at: number) {
  return new Date(at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

interface View {
  mood: KuroMood;
  frame: Frame;
  balloon: KuroEvent | null;
  status: string;
  last: KuroEvent | null;
}

/**
 * O Kuro no canto inferior esquerdo da área de conteúdo, ao lado da sidebar: longe dos toasts
 * (canto inferior direito) e, a partir de `xl`, na margem livre ao lado do composer do chat.
 * Reage às execuções que chegam (inclusive os testes do Playground: quem está no console é quem
 * os dispara), a erros e à API fora do ar; minimizado, vira um corvinho e para de consultar.
 */
export function KuroMascot({ sidebarCollapsed }: { sidebarCollapsed: boolean }) {
  const { backendReachable, observability } = useWorkspace();
  const [pref, setPref] = useLocalStorage<"shown" | "minimized">("agent-service:kuro-mascot", "shown");
  const reducedMotion = usePrefersReducedMotion();
  const hidden = useDocumentHidden();
  const wide = useWideScreen();
  const minimized = pref === "minimized";
  const asleep = minimized || hidden || !wide;
  // Com a API fora do ar na carga, o layout não leu a config: consulta assim mesmo, para notar a volta.
  const observing = observability.enabled || !backendReachable;

  const feedRef = useRef<KuroFeed | null>(null);
  const clockRef = useRef<CrowClock | null>(null);
  const ctxRef = useRef({ observability: observing, hidden, animate: !reducedMotion });
  const [view, setView] = useState<View>(() => {
    const feed = initialFeed(backendReachable);
    const ctx = { observability: observing, hidden: false };
    const mood = moodOf(feed, 0, ctx);
    return { mood, frame: frameFor(mood, 0, false), balloon: null, status: statusLine(feed, mood, ctx), last: null };
  });

  // Recalcula o que aparece (só chamado de timers e da consulta, nunca durante o render).
  const refreshRef = useRef<() => void>(() => {});
  useEffect(() => {
    ctxRef.current = { observability: observing, hidden, animate: !reducedMotion };
    refreshRef.current = () => {
      const feed = (feedRef.current ??= initialFeed(backendReachable));
      const crow = (clockRef.current ??= new CrowClock());
      const now = Date.now();
      const ctx = ctxRef.current;
      const mood = moodOf(feed, now, ctx);
      const frame = crow.next(mood, ctx.animate);
      const balloon = balloonEvent(feed, now);
      const status = statusLine(feed, mood, ctx);
      setView((prev) =>
        prev.mood === mood &&
        frameKey(prev.frame) === frameKey(frame) &&
        prev.balloon === balloon &&
        prev.status === status &&
        prev.last === feed.last
          ? prev
          : { mood, frame, balloon, status, last: feed.last }
      );
    };
  }, [observing, hidden, reducedMotion, backendReachable]);

  // O relógio da animação: um tique a cada 300 ms enquanto a aba está visível e o corvo aberto.
  useEffect(() => {
    if (asleep) return;
    const id = window.setInterval(() => refreshRef.current(), TICK_MS);
    const first = window.setTimeout(() => refreshRef.current(), 0);
    return () => {
      window.clearInterval(id);
      window.clearTimeout(first);
    };
  }, [asleep]);

  // A consulta: a cada POLL_MS, com backoff em erro; para com a aba oculta ou o corvo minimizado.
  useEffect(() => {
    if (asleep || !observing) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      const feed = feedRef.current ?? initialFeed(backendReachable);
      let next: KuroFeed;
      try {
        const res = await fetch(apiUrl("/api/observability/runs?limit=20"), {
          cache: "no-store",
          signal: controller.signal,
        });
        if (res.ok) {
          const page = (await res.json()) as RunPage;
          next = feedRuns(feed, page.items ?? [], Date.now());
        } else {
          next = feedFailure(feed, res.status, await readErrorMessage(res, "Falha ao ler as execuções"), Date.now());
        }
      } catch {
        if (controller.signal.aborted) return;
        next = feedFailure(feed, 0, "sem resposta do console", Date.now());
      }
      if (controller.signal.aborted) return;
      feedRef.current = next;
      refreshRef.current();
      timer = window.setTimeout(poll, nextPollDelay(next));
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [asleep, observing, backendReachable]);

  const position = cn(
    "fixed bottom-3 z-30 hidden transition-[left] duration-200 ease-out xl:block",
    sidebarCollapsed ? "left-[72px]" : "left-[284px]"
  );

  if (minimized) {
    return (
      <button
        type="button"
        onClick={() => setPref("shown")}
        aria-label="Mostrar o Kuro"
        title="Mostrar o Kuro"
        className={cn(
          position,
          "rounded-md p-1 opacity-40 transition-opacity hover:opacity-100 focus-visible:opacity-100"
        )}
      >
        <CrowSprite frame={STILL} scale={1} />
      </button>
    );
  }

  const alert = view.balloon?.kind === "alert" || view.mood === "error";
  const href = view.last?.href ?? "/logs/runs";
  const opensRun = href.startsWith("/runs/");

  return (
    <div className={cn(position, "group")}>
      {/* O balão: abre sozinho numa reação; no hover/foco mostra o estado e o último evento. */}
      <div
        className={cn(
          "pointer-events-none absolute bottom-full left-0 mb-1.5 w-max max-w-56 rounded-lg border bg-popover",
          "px-2.5 py-1.5 text-xs text-popover-foreground shadow-md",
          alert ? "border-destructive/40" : "border-border",
          view.balloon ? "block animate-fade-in" : "hidden group-focus-within:block group-hover:block"
        )}
      >
        {view.balloon ? (
          <p className={cn("font-medium", alert && "text-destructive")}>{view.balloon.text}</p>
        ) : (
          <>
            <p className="font-medium">{view.status}</p>
            {view.last && (
              <p className="mt-0.5 text-muted-foreground">
                última: {view.last.text} · {clock(view.last.at)}
              </p>
            )}
          </>
        )}
      </div>
      <span role="status" aria-live="polite" className="sr-only">
        {view.balloon?.text ?? ""}
      </span>

      <Link
        href={href}
        aria-label={`Kuro: ${view.last?.text ?? view.status}. Abrir ${opensRun ? "a execução" : "os Logs"}`}
        className="block rounded-md p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CrowSprite frame={view.frame} scale={3} />
      </Link>
      <button
        type="button"
        onClick={() => setPref("minimized")}
        aria-label="Minimizar o Kuro"
        title="Minimizar o Kuro"
        className={cn(
          "absolute -right-1.5 -top-1.5 hidden size-4 items-center justify-center rounded-full border border-border",
          "bg-popover text-muted-foreground hover:text-foreground group-focus-within:flex group-hover:flex"
        )}
      >
        <Minus className="size-3" />
      </button>
    </div>
  );
}
