"""O corvo do `kuro dash`: o mascote que mostra, de relance, como o serviço está.

O sprite tem 16×12 pixels e sai em 16×6 células: cada célula desenha dois pixels
empilhados com `▀`/`▄` (cor da frente em cima, do fundo embaixo). Cada humor é um
estado real do painel, não enfeite:

- `idle`: tudo normal. Parado quase sempre; de tempos em tempos pisca, olha para
  trás, bica o chão ou dá um pulinho.
- `working`: chegaram execuções novas — bate as asas.
- `error`: chegou execução com erro (ou tool falhando), ou a API respondeu erro —
  bico aberto, asas batendo e olho vermelho.
- `paused`: atualização pausada — olhos fechados e "zZ" na cabeça.
- `offline`: o serviço não responde — de cabeça para baixo, olho ×.

Sem animação, cada humor fica num quadro fixo que ainda se reconhece.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

MOODS = ("idle", "working", "error", "paused", "offline")

COLORS = {"K": "#2a3442", "H": "#4a5a6e", "B": "#8b99ab", "F": "#6b7787", "R": "#7f1d1d"}
EYE_OK = "#22d3ee"
EYE_ALERT = "#ef4444"
GLYPH = "#94a3b8"  # o "zZ" do sono e o × do olho

Grid = tuple[str, ...]

BASE: Grid = (
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
)


def _with(grid: Grid, rows: dict[int, str]) -> Grid:
    return tuple(rows.get(i, row) for i, row in enumerate(grid))


WINGS_UP = _with(
    BASE,
    {5: "...HH..KKKKK....", 6: "..HHHHKKKKKK....", 7: "...KHHHHKKKK....", 8: "KKKKKHHKKKK....."},
)
LOOK_BACK = _with(BASE, {3: "......BBKEKKK...", 4: ".....BBBKKKKK..."})
PECK: Grid = (
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
)
# No ar: tudo um pixel acima e os dedos recolhidos.
HOP: Grid = BASE[1:10] + (".....F..F.......", "................", "................")
DEAD: Grid = tuple(reversed(BASE))


def open_beak(grid: Grid) -> Grid:
    """Grasnando: o bico de cima segue reto, o de baixo cai e mostra a boca."""
    return _with(grid, {3: "........KKEKKBBB", 4: "........KKKKKR..", 5: grid[5][:12] + "BBB."})


@dataclass(frozen=True)
class Frame:
    grid: Grid
    eye: str | None = EYE_OK
    """Cor do olho; `None` = fechado."""
    dead: bool = False
    zz: str = ""
    """Até 3 caracteres no alto, à direita da cabeça."""


SNORE = ("z  ", "zZ ", "zZz", " Zz", "  z")

# Repertório do ocioso: (peso, quadros). Cada quadro dura um tique.
IDLE_ACTIONS: tuple[tuple[int, tuple[Frame, ...]], ...] = (
    (4, (Frame(BASE, eye=None),)),
    (2, (Frame(LOOK_BACK),) * 5),
    (2, (Frame(PECK), Frame(BASE), Frame(PECK), Frame(BASE))),
    (2, (Frame(HOP), Frame(BASE), Frame(HOP), Frame(BASE))),
)


def frame_for(mood: str, tick: int, animate: bool) -> Frame:
    """O quadro de um humor fora das ações do ocioso (que o `Crow` sorteia)."""
    flap = WINGS_UP if (not animate or tick % 2) else BASE
    if mood == "working":
        return Frame(flap)
    if mood == "error":
        return Frame(open_beak(flap), eye=EYE_ALERT)
    if mood == "paused":
        return Frame(BASE, eye=None, zz=SNORE[tick % len(SNORE)] if animate else "zZ ")
    if mood == "offline":
        return Frame(DEAD, dead=True)
    return Frame(BASE)


def render(frame: Frame) -> Text:
    def color(pixel: str) -> str | None:
        if pixel != "E":
            return COLORS.get(pixel)
        if frame.dead:
            return COLORS["K"]
        return frame.eye or COLORS["H"]  # olho fechado: a pálpebra tem a cor do brilho das penas

    text = Text(no_wrap=True, overflow="crop")
    grid = frame.grid
    for row in range(0, len(grid), 2):
        for col in range(len(grid[row])):
            top, bottom = grid[row][col], grid[row + 1][col]
            glyph = ""
            if frame.zz and row == 0 and col >= 13:
                glyph = frame.zz[col - 13].strip()
            if frame.dead and "E" in (top, bottom):
                glyph = "×"
            if glyph:
                text.append(glyph, Style(color=GLYPH, bold=True, bgcolor=COLORS["K"] if "E" in (top, bottom) else None))
                continue
            up, down = color(top), color(bottom)
            if up and down:
                text.append("▀", Style(color=up, bgcolor=down))
            elif up:
                text.append("▀", Style(color=up))
            elif down:
                text.append("▄", Style(color=down))
            else:
                text.append(" ")
        if row < len(grid) - 2:
            text.append("\n")
    return text


class Crow(Widget):
    """O corvo animado. Pergunta o humor a `mood_source` a cada tique.

    Só redesenha quando o quadro muda, e sem refazer o layout: parado (o ocioso quase
    sempre, ou com a animação desligada) ele não custa nada à tela.
    """

    DEFAULT_CSS = "Crow { width: 16; height: 6; }"
    TICK = 0.3

    def __init__(
        self, mood_source: Callable[[], str], animate: Callable[[], bool], *, rng: random.Random | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.mood_source = mood_source
        self.animate = animate
        self.rng = rng or random.Random()
        self.tick_count = 0
        self.mood = "idle"
        self.queue: list[Frame] = []
        self.rest = self._rest()
        self.frame = Frame(BASE)

    def on_mount(self) -> None:
        self.step()
        self.set_interval(self.TICK, self.step)

    def render(self) -> Text:
        return render(self.frame)

    def _rest(self) -> int:
        return self.rng.randint(13, 26)  # 4 a 8 s parado entre uma ação e outra

    def next_frame(self) -> Frame:
        mood, animate = self.mood_source(), self.animate()
        if mood != self.mood:
            self.mood, self.queue, self.rest = mood, [], self._rest()
        self.tick_count += 1
        if mood != "idle" or not animate:
            self.queue = []
            return frame_for(mood, self.tick_count, animate)
        if not self.queue:
            self.rest -= 1
            if self.rest > 0:
                return Frame(BASE)
            weights, actions = zip(*IDLE_ACTIONS, strict=True)
            self.queue = list(self.rng.choices(actions, weights=weights)[0])
            self.rest = self._rest()
        return self.queue.pop(0)

    def step(self) -> None:
        frame = self.next_frame()
        if frame != self.frame:
            self.frame = frame
            self.refresh()
