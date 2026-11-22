"""
Lightweight candlestick / line chart drawn on a Tkinter Canvas (no matplotlib).

- Candles or line, last-price marker, price + time axes.
- Indicators (toggle from the "Indicators" menu):
    overlays : Bollinger Bands (20, 2), MA (7/25/99), EMA (12/26)
    panes    : Volume, RSI (14), MACD (12, 26, 9)
- Mouse wheel zooms, crosshair shows OHLC + indicator values.
- update_last(price) patches the live candle between full refreshes.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")

# key -> menu label; order = menu order
INDICATORS = {
    "bb": "Bollinger Bands (20, 2)",
    "ma": "MA (7, 25, 99)",
    "ema": "EMA (12, 26)",
    "vol": "Volume",
    "rsi": "RSI (14)",
    "macd": "MACD (12, 26, 9)",
}
DEFAULT_INDICATORS = {"bb": True, "ma": False, "ema": False, "vol": True, "rsi": False, "macd": False}

BG = "#14161a"
GRID = "#22262c"
SEP = "#2b3139"
AXIS_FG = "#8a9099"
FG = "#e6e8eb"
UP = "#0ecb81"
DOWN = "#f6465d"
LINE = "#f0b90b"
CROSS = "#5e6673"
VOL_UP = "#0d4f37"
VOL_DOWN = "#5c2029"
BB_LINE = "#9c7cf4"
BB_FILL = "#1b1a28"
MA_COLORS = {7: "#f0b90b", 25: "#e056fd", 99: "#3d9df3"}
EMA_COLORS = {12: "#00bcd4", 26: "#ff7043"}
RSI_COLOR = "#b388ff"
MACD_COLOR = "#3d9df3"
SIGNAL_COLOR = "#ff9800"

AXIS_W = 86      # right price axis
TIME_H = 22      # bottom time axis
VOL_FRAC = 0.18  # share of the price pane used by volume bars
FONT = ("Consolas", 9)


# --------------------------------------------------------------------------
# Indicator math (lists aligned with candles; None during warm-up)
# --------------------------------------------------------------------------
def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    k = 2 / (n + 1)
    prev = None
    seed: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def bollinger(closes: list[float], n: int = 20, mult: float = 2.0):
    mid = sma(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        var = sum((c - m) ** 2 for c in closes[i - n + 1:i + 1]) / n
        sd = math.sqrt(var)
        upper[i], lower[i] = m + mult * sd, m - mult * sd
    return upper, mid, lower


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes: list[float], fast=12, slow=26, sig=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    signal = ema(line, sig)
    hist = [m - s if m is not None and s is not None else None for m, s in zip(line, signal)]
    return line, signal, hist


def _nice_step(span: float, target: int) -> float:
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


# --------------------------------------------------------------------------
# Widget
# --------------------------------------------------------------------------
class PriceChart(ttk.Frame):
    def __init__(self, master, on_change=None, price_fmt=None):
        super().__init__(master)
        self.on_change = on_change              # callback(interval, style, indicators)
        self.price_fmt = price_fmt or (lambda sym, v: f"{v:,.8g}")
        self.symbol: str | None = None
        self.interval = "15m"
        self.style = "candles"
        self.indicators = dict(DEFAULT_INDICATORS)
        self.candles: list[list] = []           # [t, o, h, l, c, v, close_t]
        self.visible = 120
        self._cross: tuple[int, int] | None = None
        self._redraw_job = None
        self._geom = None
        self._ind: dict = {}

        bar = ttk.Frame(self, padding=(0, 4, 0, 4))
        bar.pack(fill="x")
        self.var_title = tk.StringVar(value="Click a ticker to show its chart")
        ttk.Label(bar, textvariable=self.var_title, font=("Segoe UI", 11, "bold")).pack(side="left")

        # Indicators dropdown
        self.ind_vars = {k: tk.BooleanVar(value=v) for k, v in self.indicators.items()}
        mb = ttk.Menubutton(bar, text="Indicators ▾", style="Toggle.TMenubutton")
        menu = tk.Menu(mb, tearoff=False)
        for i, (key, label) in enumerate(INDICATORS.items()):
            if i == 3:
                menu.add_separator()
            menu.add_checkbutton(label=label, variable=self.ind_vars[key], command=self._indicators_changed)
        mb["menu"] = menu
        mb.pack(side="right", padx=(8, 0))

        self.var_style = tk.StringVar(value=self.style)
        for val, txt in (("line", "Line"), ("candles", "Candles")):
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.var_style, style="Toggle.TRadiobutton",
                            command=self._style_changed).pack(side="right", padx=(2, 0))
        ttk.Label(bar, text="  ").pack(side="right")
        self.var_interval = tk.StringVar(value=self.interval)
        for iv in reversed(INTERVALS):
            ttk.Radiobutton(bar, text=iv, value=iv, variable=self.var_interval, style="Toggle.TRadiobutton",
                            command=self._interval_changed).pack(side="right", padx=(2, 0))

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, height=300, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    # ---------------- public API ----------------
    def set_options(self, interval: str, style: str, indicators: dict | None = None):
        self.interval = interval if interval in INTERVALS else "15m"
        self.style = style if style in ("candles", "line") else "candles"
        self.var_interval.set(self.interval)
        self.var_style.set(self.style)
        if isinstance(indicators, dict):
            for k in INDICATORS:
                self.indicators[k] = bool(indicators.get(k, DEFAULT_INDICATORS[k]))
                self.ind_vars[k].set(self.indicators[k])
        self._compute()
        self.schedule_redraw()

    def set_symbol(self, symbol: str | None):
        if symbol != self.symbol:
            self.symbol = symbol
            self.candles = []
            self._ind = {}
            self.var_title.set(f"{symbol}  ·  {self.interval}  ·  loading..." if symbol
                               else "Click a ticker to show its chart")
            self.schedule_redraw()

    def set_data(self, symbol: str, interval: str, candles: list[tuple]):
        if symbol != self.symbol or interval != self.interval:
            return  # stale response
        self.candles = [list(c) for c in candles]
        self.var_title.set(f"{symbol}  ·  {interval}")
        self._compute()
        self.schedule_redraw()

    def set_error(self, symbol: str, interval: str, msg: str):
        if symbol == self.symbol and interval == self.interval and not self.candles:
            self.var_title.set(f"{symbol}  ·  {interval}  ·  error: {msg[:80]}")

    def update_last(self, symbol: str, price: float):
        """Patch the live candle with the latest ticker price."""
        if symbol != self.symbol or not self.candles:
            return
        c = self.candles[-1]
        if time.time() * 1000 > c[6] or price == c[4]:
            return
        c[4] = price
        c[2] = max(c[2], price)
        c[3] = min(c[3], price)
        self._compute()
        self.schedule_redraw()

    # ---------------- indicators ----------------
    def _compute(self):
        ind = {}
        if self.candles:
            closes = [c[4] for c in self.candles]
            on = self.indicators
            if on["bb"]:
                ind["bb"] = bollinger(closes, 20, 2.0)
            if on["ma"]:
                ind["ma"] = {n: sma(closes, n) for n in MA_COLORS}
            if on["ema"]:
                ind["ema"] = {n: ema(closes, n) for n in EMA_COLORS}
            if on["rsi"]:
                ind["rsi"] = rsi(closes, 14)
            if on["macd"]:
                ind["macd"] = macd(closes)
        self._ind = ind

    # ---------------- events ----------------
    def _notify(self):
        if self.on_change:
            self.on_change(self.interval, self.style, dict(self.indicators))

    def _interval_changed(self):
        self.interval = self.var_interval.get()
        if self.symbol:
            self.candles = []
            self._ind = {}
            self.var_title.set(f"{self.symbol}  ·  {self.interval}  ·  loading...")
        self.schedule_redraw()
        self._notify()

    def _style_changed(self):
        self.style = self.var_style.get()
        self.schedule_redraw()
        self._notify()

    def _indicators_changed(self):
        self.indicators = {k: v.get() for k, v in self.ind_vars.items()}
        self._compute()
        self.schedule_redraw()
        self._notify()

    def _on_wheel(self, e):
        factor = 0.85 if e.delta > 0 else 1.18
        self.visible = int(min(max(15, self.visible * factor), max(15, len(self.candles) or 500)))
        self.schedule_redraw()

    def _on_motion(self, e):
        self._cross = (e.x, e.y)
        self._draw_cross()

    def _on_leave(self, _e):
        self._cross = None
        self.canvas.delete("cross")
        self._draw_info(None)

    # ---------------- drawing ----------------
    def schedule_redraw(self):
        if self._redraw_job is None:
            self._redraw_job = self.after_idle(self._redraw)

    def _legend_rows(self) -> int:
        return 1 if any(self.indicators[k] for k in ("bb", "ma", "ema")) else 0

    def _redraw(self):
        self._redraw_job = None
        cv = self.canvas
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        self._geom = None
        if w < 80 or h < 80:
            return
        if not self.candles:
            msg = "Click a ticker to show its chart" if not self.symbol else "Loading..."
            cv.create_text(w / 2, h / 2, text=msg, fill=AXIS_FG, font=("Segoe UI", 11))
            return

        total = len(self.candles)
        start = max(0, total - self.visible)
        data = self.candles[start:]
        n = len(data)
        left, right = 6, w - AXIS_W
        top = 22 + 16 * self._legend_rows() + 4
        step = (right - left) / n
        body_w = max(1.0, step * 0.7)

        # Vertical layout: price pane, then optional RSI / MACD panes.
        subs = [k for k in ("rsi", "macd") if self.indicators[k]]
        avail = h - TIME_H - top
        sub_h = max(48, avail * 0.2) if subs else 0
        price_bot = h - TIME_H - sub_h * len(subs) - (6 if subs else 0)
        vol_on = self.indicators["vol"]
        vol_top = price_bot - (price_bot - top) * VOL_FRAC if vol_on else price_bot
        price_area_bot = vol_top - 4 if vol_on else price_bot

        def x_of(i):
            return left + step * (i + 0.5)

        # price range includes visible overlays
        lo = min(c[3] for c in data)
        hi = max(c[2] for c in data)
        for series in self._overlay_series():
            vals = [v for v in series[start:] if v is not None]
            if vals:
                lo, hi = min(lo, min(vals)), max(hi, max(vals))
        if hi == lo:
            hi, lo = hi * 1.001 + 1e-12, lo * 0.999
        pad = (hi - lo) * 0.05
        lo, hi = lo - pad, hi + pad

        def y_of(p):
            return top + (hi - p) / (hi - lo) * (price_area_bot - top)

        self._geom = dict(left=left, right=right, step=step, n=n, start=start, data=data, lo=lo, hi=hi,
                          top=top, price_bot=price_area_bot, h=h, w=w)

        # Bollinger band fill goes under the grid
        if "bb" in self._ind:
            up, _, dn = self._ind["bb"]
            pts_u = [(x_of(i), y_of(up[start + i])) for i in range(n) if up[start + i] is not None]
            pts_d = [(x_of(i), y_of(dn[start + i])) for i in range(n) if dn[start + i] is not None]
            if len(pts_u) >= 2:
                poly = [c for p in pts_u for c in p] + [c for p in reversed(pts_d) for c in p]
                cv.create_polygon(*poly, fill=BB_FILL, outline="")

        # price grid + axis labels
        pstep = _nice_step(hi - lo, max(3, int((price_area_bot - top) / 30)))
        p = math.ceil(lo / pstep) * pstep
        while p <= hi:
            y = y_of(p)
            cv.create_line(left, y, right, y, fill=GRID)
            cv.create_text(right + 6, y, text=self.price_fmt(self.symbol, p), anchor="w", fill=AXIS_FG, font=FONT)
            p += pstep

        # vertical grid + time labels (through every pane)
        every = max(1, int(110 / step))
        for i in range(n - 1, -1, -every):
            x = x_of(i)
            cv.create_line(x, top, x, h - TIME_H, fill=GRID)
            cv.create_text(x, h - TIME_H / 2, text=self._fmt_time(data[i][0]), fill=AXIS_FG, font=FONT)
        cv.create_line(right, top - 6, right, h - TIME_H, fill=GRID)

        # volume
        if vol_on:
            vmax = max(c[5] for c in data) or 1.0
            for i, c in enumerate(data):
                x = x_of(i)
                vh = (c[5] / vmax) * (price_bot - vol_top)
                cv.create_rectangle(x - body_w / 2, price_bot - vh, x + body_w / 2, price_bot, width=0,
                                    fill=VOL_UP if c[4] >= c[1] else VOL_DOWN)

        # price
        if self.style == "line":
            pts = [v for i, c in enumerate(data) for v in (x_of(i), y_of(c[4]))]
            if len(pts) >= 4:
                cv.create_line(*pts, fill=LINE, width=2)
        else:
            for i, c in enumerate(data):
                x = x_of(i)
                col = UP if c[4] >= c[1] else DOWN
                cv.create_line(x, y_of(c[2]), x, y_of(c[3]), fill=col)
                y1, y2 = y_of(c[1]), y_of(c[4])
                if abs(y2 - y1) < 1:
                    y2 = y1 + 1
                cv.create_rectangle(x - body_w / 2, min(y1, y2), x + body_w / 2, max(y1, y2), fill=col, outline=col)

        # overlay lines
        def poly_line(series, color, width=1, dash=None):
            seg: list[float] = []
            for i in range(n):
                v = series[start + i]
                if v is None:
                    if len(seg) >= 4:
                        cv.create_line(*seg, fill=color, width=width, dash=dash)
                    seg = []
                else:
                    seg += [x_of(i), y_of(v)]
            if len(seg) >= 4:
                cv.create_line(*seg, fill=color, width=width, dash=dash)

        if "bb" in self._ind:
            up, mid, dn = self._ind["bb"]
            poly_line(up, BB_LINE)
            poly_line(dn, BB_LINE)
            poly_line(mid, BB_LINE, dash=(4, 3))
        for n_, s in self._ind.get("ma", {}).items():
            poly_line(s, MA_COLORS[n_])
        for n_, s in self._ind.get("ema", {}).items():
            poly_line(s, EMA_COLORS[n_])

        # last price marker
        last = data[-1]
        col = UP if last[4] >= last[1] else DOWN
        y = y_of(last[4])
        cv.create_line(left, y, right, y, fill=col, dash=(2, 3))
        cv.create_rectangle(right, y - 9, w, y + 9, fill=col, width=0)
        cv.create_text(right + 6, y, text=self.price_fmt(self.symbol, last[4]), anchor="w",
                       fill="#111", font=("Consolas", 9, "bold"))

        # sub panes
        self._geom["subs"] = []
        y0 = price_bot + 6
        for key in subs:
            y1 = y0 + sub_h
            cv.create_line(left, y0 - 3, w, y0 - 3, fill=SEP)
            self._geom["subs"].append((key, y0, y1))
            if key == "rsi":
                self._draw_rsi(x_of, start, n, y0, y1, left, right)
            else:
                self._draw_macd(x_of, start, n, y0, y1, left, right, body_w)
            y0 = y1

        self._draw_info(None)
        if self._cross:
            self._draw_cross()

    def _overlay_series(self):
        if "bb" in self._ind:
            yield self._ind["bb"][0]
            yield self._ind["bb"][2]
        yield from self._ind.get("ma", {}).values()
        yield from self._ind.get("ema", {}).values()

    def _draw_rsi(self, x_of, start, n, y0, y1, left, right):
        cv = self.canvas
        pad = 6

        def ry(v):
            return y0 + pad + (100 - v) / 100 * (y1 - y0 - 2 * pad)
        cv.create_rectangle(left, ry(70), right, ry(30), fill="#191b24", width=0)
        for lvl in (70, 30):
            cv.create_line(left, ry(lvl), right, ry(lvl), fill=CROSS, dash=(2, 3))
            cv.create_text(right + 6, ry(lvl), text=str(lvl), anchor="w", fill=AXIS_FG, font=FONT)
        series = self._ind.get("rsi", [])
        pts = [v for i in range(n) if series[start + i] is not None for v in (x_of(i), ry(series[start + i]))]
        if len(pts) >= 4:
            cv.create_line(*pts, fill=RSI_COLOR, width=1)
        cv.create_text(left + 2, y0 + 2, text="RSI 14", anchor="nw", fill=AXIS_FG, font=FONT, tags="subinfo")

    def _draw_macd(self, x_of, start, n, y0, y1, left, right, body_w):
        cv = self.canvas
        line, signal, hist = self._ind.get("macd", ([], [], []))
        vals = [abs(v) for s in (line, signal, hist) for v in s[start:start + n] if v is not None]
        m = max(vals) if vals else 1.0
        m = m or 1.0
        pad = 6
        mid = (y0 + y1) / 2

        def my(v):
            return mid - v / m * ((y1 - y0) / 2 - pad)
        cv.create_line(left, mid, right, mid, fill=GRID)
        for i in range(n):
            v = hist[start + i]
            if v is not None:
                x = x_of(i)
                cv.create_rectangle(x - body_w / 2, min(mid, my(v)), x + body_w / 2, max(mid, my(v)),
                                    width=0, fill=UP if v >= 0 else DOWN)
        for series, color in ((line, MACD_COLOR), (signal, SIGNAL_COLOR)):
            pts = [c for i in range(n) if series[start + i] is not None for c in (x_of(i), my(series[start + i]))]
            if len(pts) >= 4:
                cv.create_line(*pts, fill=color)
        cv.create_text(right + 6, mid, text="0", anchor="w", fill=AXIS_FG, font=FONT)

    def _fmt_time(self, ms: int) -> str:
        t = time.localtime(ms / 1000)
        if self.interval in ("1d", "1w"):
            return time.strftime("%d %b", t)
        if self.interval in ("1h", "4h"):
            return time.strftime("%d %H:%M", t)
        return time.strftime("%H:%M", t)

    def _texts(self, y, parts, tag):
        """Draw colored text segments left-to-right on one line."""
        x = 8
        for text, color in parts:
            item = self.canvas.create_text(x, y, text=text, anchor="w", fill=color, font=FONT, tags=tag)
            x = self.canvas.bbox(item)[2] + 10

    def _draw_info(self, idx: int | None):
        cv = self.canvas
        cv.delete("info")
        if not self.candles:
            return
        if idx is None:
            idx = len(self.candles) - 1
        c = self.candles[idx]
        f = lambda v: self.price_fmt(self.symbol, v) if v is not None else "-"  # noqa: E731
        chg = (c[4] - c[1]) / c[1] * 100 if c[1] else 0.0
        col = UP if c[4] >= c[1] else DOWN
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(c[0] / 1000))
        self._texts(11, [(t, AXIS_FG), (f"O {f(c[1])}", col), (f"H {f(c[2])}", col), (f"L {f(c[3])}", col),
                         (f"C {f(c[4])}", col), (f"{chg:+.2f}%", col), (f"Vol {c[5]:,.2f}", AXIS_FG)], "info")

        legend = []
        if "bb" in self._ind:
            up, mid, dn = (s[idx] for s in self._ind["bb"])
            legend.append((f"BB(20,2)  U {f(up)}  M {f(mid)}  L {f(dn)}", BB_LINE))
        for n_, s in self._ind.get("ma", {}).items():
            legend.append((f"MA{n_} {f(s[idx])}", MA_COLORS[n_]))
        for n_, s in self._ind.get("ema", {}).items():
            legend.append((f"EMA{n_} {f(s[idx])}", EMA_COLORS[n_]))
        if legend:
            self._texts(27, legend, "info")

        # sub-pane values
        g = self._geom
        if g:
            for key, y0, _ in g.get("subs", []):
                if key == "rsi" and "rsi" in self._ind:
                    v = self._ind["rsi"][idx]
                    txt = [(f"RSI(14) {v:.2f}" if v is not None else "RSI(14) -", RSI_COLOR)]
                elif key == "macd" and "macd" in self._ind:
                    m, s, hv = (series[idx] for series in self._ind["macd"])
                    fm = lambda v: f"{v:.6g}" if v is not None else "-"  # noqa: E731
                    txt = [("MACD(12,26,9)", AXIS_FG), (fm(m), MACD_COLOR), (fm(s), SIGNAL_COLOR),
                           (fm(hv), UP if (hv or 0) >= 0 else DOWN)]
                else:
                    continue
                cv.delete("subinfo")
                x = 8
                for text, color in txt:
                    item = cv.create_text(x, y0 + 8, text=text, anchor="w", fill=color, font=FONT, tags="info")
                    x = cv.bbox(item)[2] + 10

    def _draw_cross(self):
        cv = self.canvas
        cv.delete("cross")
        g = self._geom
        if not g or not self._cross:
            return
        x, y = self._cross
        if not (g["left"] <= x <= g["right"]) or y < g["top"] or y > g["h"] - TIME_H:
            self._draw_info(None)
            return
        i = min(g["n"] - 1, max(0, int((x - g["left"]) / g["step"])))
        cx = g["left"] + g["step"] * (i + 0.5)
        cv.create_line(cx, g["top"], cx, g["h"] - TIME_H, fill=CROSS, dash=(3, 3), tags="cross")
        cv.create_line(g["left"], y, g["right"], y, fill=CROSS, dash=(3, 3), tags="cross")
        if g["top"] <= y <= g["price_bot"]:
            p = g["hi"] - (y - g["top"]) / (g["price_bot"] - g["top"]) * (g["hi"] - g["lo"])
            cv.create_rectangle(g["right"], y - 9, g["w"], y + 9, fill=SEP, width=0, tags="cross")
            cv.create_text(g["right"] + 6, y, text=self.price_fmt(self.symbol, p), anchor="w",
                           fill=FG, font=FONT, tags="cross")
        self._draw_info(g["start"] + i)
