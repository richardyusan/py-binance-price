"""
Binance Price Viewer - a fast, dependency-free Tkinter app for watching
Binance spot prices.

Features
- Live prices for a list of favorite tickers (one batched HTTPS request per
  refresh over a persistent keep-alive connection, done in a background thread
  so the UI never blocks).
- Add / remove / reorder favorites, symbol autocomplete from Binance's
  exchange info, sortable columns, price-change flashing.
- Click a ticker to see its candlestick / line chart (1m ... 1w).
- Hostnames are resolved through a public DNS provider (Cloudflare 1.1.1.1 by
  default) over DNS-over-HTTPS / TLS, bypassing ISP DNS hijacking.
- Save / load the configuration as JSON inside the app's own folder
  (config.json is loaded automatically at startup).

Files: binance_viewer.py (UI), net.py (DNS + HTTP), chart.py (chart widget)
Run:  py binance_viewer.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from chart import DEFAULT_INDICATORS, PriceChart
import tray
from net import (DNS_MODES, DNS_PROVIDERS, MIN_REFRESH_MS, PROXY_TYPES, TRAFFIC, BinanceClient,
                 ChartWorker, PriceWorker, Resolver, make_proxy, system_proxy)

APP_NAME = "Binance Price Viewer"
APP_VERSION = "1.2.0"
APP_AUTHOR = "richardyusan"
APP_URL = "https://github.com/richardyusan/py-binance-price"

# Folder the app lives in (works for both `py binance_viewer.py` and a
# PyInstaller-frozen exe).
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

# Bundled read-only resources (icons): inside the exe when frozen with
# PyInstaller --onefile, otherwise the app folder. config.json always lives
# next to the script / exe so it can be edited and saved.
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG: dict = {
    "favorites": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    "refresh_ms": 1000,
    # api.binance.com is the main endpoint; data-api.binance.vision serves the
    # same public market data and works in some regions where the main one
    # is blocked. Use api.binance.us for Binance.US.
    "api_host": "data-api.binance.vision",
    # DNS: cloudflare | google | quad9 | adguard | opendns | system | custom
    "dns_provider": "cloudflare",
    "dns_mode": "doh",            # doh | dot | udp
    "dns_custom": "",             # e.g. "1.1.1.1, 8.8.8.8" when provider == custom
    # If the chosen DNS is blocked (e.g. office firewall drops 1.1.1.1), try
    # the other encrypted public resolvers and keep the first that answers.
    "dns_fallback": True,
    # Proxy: none | system (Windows settings) | http (CONNECT) | socks5
    "proxy_type": "none",
    "proxy_host": "",
    "proxy_port": 8080,
    "proxy_user": "",
    "proxy_password": "",
    "proxy_remote_dns": True,     # let the proxy resolve hostnames
    # System tray / window behaviour
    "close_to_tray": True,        # X hides to the tray; exit via tray menu or File > Exit
    "minimize_to_tray": False,
    "start_in_tray": False,
    "pause_when_hidden": True,    # no network traffic while hidden or minimized
    "always_on_top": False,
    "flash": True,
    "sort_column": None,
    "sort_reverse": False,
    "chart_interval": "15m",
    "chart_style": "candles",
    "show_chart": False,
    "chart_indicators": dict(DEFAULT_INDICATORS),
    "geometry": "920x460",
}

API_HOSTS = ("data-api.binance.vision", "api.binance.com", "api1.binance.com",
             "api2.binance.com", "api3.binance.com", "api4.binance.com", "api.binance.us")

COLUMNS = (
    ("symbol", "Symbol", 110, "w"),
    ("price", "Last Price", 130, "e"),
    ("change", "24h Change", 110, "e"),
    ("pct", "24h %", 80, "e"),
    ("high", "24h High", 120, "e"),
    ("low", "24h Low", 120, "e"),
    ("volume", "24h Vol (quote)", 140, "e"),
)

# Colors (dark theme)
BG = "#14161a"
BG_ALT = "#1b1e23"
FG = "#e6e8eb"
FG_DIM = "#8a9099"
ACCENT = "#f0b90b"
UP = "#0ecb81"
DOWN = "#f6465d"
FLASH_UP = "#0f3d2c"
FLASH_DOWN = "#4a1c24"


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def fmt_price(value: str, tick: Decimal | None) -> str:
    d = Decimal(value)
    if tick is not None and tick > 0:
        places = max(0, -tick.as_tuple().exponent)
        return f"{d:,.{places}f}"
    s = f"{d:,.8f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def fmt_volume(value: str) -> str:
    v = float(value)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:,.2f}{unit}"
    return f"{v:,.2f}"


def make_resolver(cfg: dict) -> Resolver:
    custom = [s for s in str(cfg.get("dns_custom") or "").replace(";", ",").split(",") if s.strip()]
    return Resolver(cfg.get("dns_provider", "cloudflare"), cfg.get("dns_mode", "doh"), custom,
                    fallback=bool(cfg.get("dns_fallback", True)))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(700, 300)
        self.configure(bg=BG)
        self._set_icon()

        self.config_data: dict = dict(DEFAULT_CONFIG)
        self.config_path: Path = DEFAULT_CONFIG_PATH
        self.dirty = False

        self.exchange: dict[str, dict] = {}
        self.all_symbols: list[str] = []
        self.last_prices: dict[str, Decimal] = {}
        self.raw_rows: dict[str, dict] = {}
        self.flash_jobs: dict[str, str] = {}
        self.conn_info: dict | None = None
        self.last_error: str | None = None
        self.resolver = make_resolver(DEFAULT_CONFIG)
        self.proxy = None
        self.tray: tray.TrayIcon | None = None
        self._eff_paused = False
        self._tray_hint_shown = False
        self._tip_lines: list[str] = []

        self.q: queue.Queue = queue.Queue()
        self.worker = PriceWorker(self.q, DEFAULT_CONFIG["api_host"], self.resolver)
        self.chart_worker = ChartWorker(self.q, DEFAULT_CONFIG["api_host"], self.resolver)

        self._build_style()
        self._init_tray()
        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()   # packed at the bottom before the expanding panes
        self._build_panes()

        self._load_initial_config()
        self.worker.start()
        self.chart_worker.start()
        self._drain_job = self.after(30, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", lambda e: e.widget is self and self._update_pause())
        if self.config_data.get("start_in_tray") and self.tray:
            self.after(10, self.hide_window)

    # ---------------- Tray / window / pause ----------------
    def _init_tray(self):
        icon = RES_DIR / "icon.ico"
        t = tray.TrayIcon(str(icon), APP_NAME, lambda action: self.q.put(("tray", action)))
        if t.start():
            self.tray = t

    def _hidden(self) -> bool:
        return self.state() in ("withdrawn", "iconic")

    def on_window_close(self):
        """Window X button: hide to the tray (default) or exit."""
        if self.config_data.get("close_to_tray", True):
            self.hide_window()
        else:
            self.exit_app()

    def hide_window(self):
        if self.tray:
            self.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.notify(APP_NAME, "Still running in the system tray. Click the icon to open "
                                           "it, right-click for Exit.")
        else:
            self.iconify()
        self._update_pause()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        if not self.var_top.get():  # bring to front even if another app has focus
            self.attributes("-topmost", True)
            self.after(200, lambda: self.attributes("-topmost", self.var_top.get()))
        self._update_pause()

    def _on_unmap(self, event):
        if event.widget is not self:
            return
        if self.tray and self.config_data.get("minimize_to_tray") and self.state() == "iconic":
            self.after(0, self.withdraw)
        self.after(50, self._update_pause)

    def _on_tray(self, action: str):
        if action == "toggle":
            self.show_window() if self._hidden() else self.hide_window()
        elif action == "pause":
            self.var_paused.set(not self.var_paused.get())
            self._update_pause()
        elif action == "exit":
            self.after_idle(self.exit_app)  # not from inside _drain_queue

    def toggle_pause(self):
        self.var_paused.set(not self.var_paused.get())
        self._update_pause()

    def _update_pause(self):
        """Pause the workers when paused manually, or when hidden (if enabled)."""
        if not self.winfo_exists():
            return
        manual = self.var_paused.get()
        auto = bool(self.config_data.get("pause_when_hidden", True)) and self._hidden()
        paused = manual or auto
        self.var_pause_text.set("▶ Resume" if manual else "⏸ Pause")
        if paused != self._eff_paused:
            self._eff_paused = paused
            self.worker.set_paused(paused)
            self.chart_worker.set_paused(paused)
            if paused:
                self.conn_info = None
                self.var_status.set("Updates paused: no network requests"
                                    + ("" if manual else " while the window is hidden"))
            else:
                self.var_status.set("Resuming...")
        if self.tray:
            self.tray.paused = manual
            self.tray.window_visible = not self._hidden()
        self._update_conn_bar()
        self._update_tray_tip()

    def _update_tray_tip(self):
        if not self.tray:
            return
        lines = [APP_NAME]
        if self._eff_paused:
            lines.append("Updates paused")
        lines += self._tip_lines
        text = ""
        for line in lines:  # tooltip limit is 127 characters
            if len(text) + len(line) + 1 > 127:
                break
            text += ("\n" if text else "") + line
        self.tray.set_tooltip(text)

    def exit_app(self):
        if self.dirty and self._hidden():
            self.show_window()
        if not self._confirm_discard():
            return
        self.worker.stop()
        self.chart_worker.stop()
        if self.tray:
            self.tray.stop()
        if self._drain_job:
            self.after_cancel(self._drain_job)
        self.destroy()

    # ---------------- UI construction ----------------
    def _set_icon(self):
        """Window + taskbar icon (generated by make_icon.ps1)."""
        ico, png = RES_DIR / "icon.ico", RES_DIR / "icon.png"
        try:
            if ico.exists() and sys.platform == "win32":
                self.iconbitmap(default=str(ico))  # default= also applies to dialogs
            elif png.exists():
                self._icon_img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_ALT,
                        bordercolor=BG_ALT, lightcolor=BG_ALT, darkcolor=BG_ALT,
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", foreground=FG_DIM)
        style.configure("TButton", background=BG_ALT, foreground=FG, padding=(10, 4))
        style.map("TButton", background=[("active", "#2a2e35")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#111")
        style.map("Accent.TButton", background=[("active", "#ffd23f")])
        style.configure("TCombobox", fieldbackground=BG_ALT, background=BG_ALT,
                        foreground=FG, arrowcolor=FG, insertcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", BG_ALT)],
                  foreground=[("readonly", FG)])
        style.configure("TSpinbox", fieldbackground=BG_ALT, foreground=FG,
                        arrowcolor=FG, insertcolor=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Treeview", background=BG, fieldbackground=BG, foreground=FG,
                        rowheight=28, borderwidth=0, font=("Consolas", 11))
        style.configure("Treeview.Heading", background=BG_ALT, foreground=FG_DIM,
                        relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", "#2a2e35")])
        style.map("Treeview", background=[("selected", "#2b3139")],
                  foreground=[("selected", FG)])
        style.configure("Vertical.TScrollbar", background=BG_ALT, troughcolor=BG,
                        arrowcolor=FG_DIM)
        style.configure("Toggle.TRadiobutton", background=BG_ALT, foreground=FG_DIM,
                        padding=(8, 2), indicatorsize=0, indicatormargin=0, borderwidth=0)
        style.layout("Toggle.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})])
        style.map("Toggle.TRadiobutton",
                  background=[("selected", "#2b3139"), ("active", "#22262c")],
                  foreground=[("selected", ACCENT), ("active", FG)])
        # Same look for the "Chart" on/off button
        style.configure("Toggle.TCheckbutton", background=BG_ALT, foreground=FG_DIM,
                        padding=(10, 4), borderwidth=0)
        style.layout("Toggle.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})])
        style.map("Toggle.TCheckbutton",
                  background=[("selected", "#2b3139"), ("active", "#22262c")],
                  foreground=[("selected", ACCENT), ("active", FG)])
        style.configure("Toggle.TMenubutton", background=BG_ALT, foreground=FG, padding=(8, 2),
                        arrowsize=0, borderwidth=0)
        style.map("Toggle.TMenubutton", background=[("active", "#22262c")])
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", sashthickness=6, gripcount=0, background=BG_ALT)
        style.configure("TLabelframe", background=BG, bordercolor="#2b3139")
        style.configure("TLabelframe.Label", background=BG, foreground=FG_DIM)
        style.configure("TEntry", fieldbackground=BG_ALT, foreground=FG, insertcolor=FG)
        style.map("TEntry", fieldbackground=[("disabled", BG)], foreground=[("disabled", FG_DIM)])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=BG_ALT, foreground=FG_DIM, padding=(10, 5),
                        borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#2b3139"), ("active", "#22262c")],
                  foreground=[("selected", ACCENT), ("active", FG)])
        # Dropdown list of the combobox
        self.option_add("*TCombobox*Listbox.background", BG_ALT)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", "#2b3139")
        self.option_add("*TCombobox*Listbox.font", ("Consolas", 10))

    def _build_menu(self):
        menubar = tk.Menu(self)
        filem = tk.Menu(menubar, tearoff=False)
        filem.add_command(label="Load config...", accelerator="Ctrl+O", command=self.load_config_dialog)
        filem.add_command(label="Save config", accelerator="Ctrl+S", command=self.save_config)
        filem.add_command(label="Save config as...", accelerator="Ctrl+Shift+S", command=self.save_config_as)
        filem.add_separator()
        filem.add_command(label="Reset to defaults", command=self.reset_defaults)
        filem.add_command(label="Open app folder", command=lambda: webbrowser.open(APP_DIR.as_uri()))
        filem.add_separator()
        filem.add_command(label="Hide to tray" if self.tray else "Minimize", command=self.hide_window)
        filem.add_command(label="Exit", accelerator="Ctrl+Q", command=self.exit_app)
        menubar.add_cascade(label="File", menu=filem)

        viewm = tk.Menu(menubar, tearoff=False)
        self.var_top = tk.BooleanVar(value=False)
        self.var_flash = tk.BooleanVar(value=True)
        viewm.add_checkbutton(label="Always on top", variable=self.var_top, command=self._on_top_toggle)
        viewm.add_checkbutton(label="Flash on price change", variable=self.var_flash,
                              command=lambda: self._set_cfg("flash", self.var_flash.get()))
        viewm.add_separator()
        self.var_paused = tk.BooleanVar(value=False)
        self.var_pause_text = tk.StringVar(value="⏸ Pause")
        viewm.add_checkbutton(label="Pause updates", accelerator="Ctrl+P", variable=self.var_paused,
                              command=self._update_pause)
        self.var_show_chart = tk.BooleanVar(value=False)
        viewm.add_checkbutton(label="Show chart", accelerator="Ctrl+G", variable=self.var_show_chart,
                              command=lambda: self.set_chart_visible(self.var_show_chart.get()))
        menubar.add_cascade(label="View", menu=viewm)

        setm = tk.Menu(menubar, tearoff=False)
        setm.add_command(label="Connection & proxy...", accelerator="Ctrl+,",
                         command=lambda: self.open_connection_settings(0))
        setm.add_command(label="DNS...", command=lambda: self.open_connection_settings(1))
        setm.add_command(label="Window & tray...", command=lambda: self.open_connection_settings(2))
        menubar.add_cascade(label="Settings", menu=setm)

        helpm = tk.Menu(menubar, tearoff=False)
        helpm.add_command(label="GitHub project page", command=lambda: webbrowser.open(APP_URL))
        helpm.add_separator()
        helpm.add_command(label=f"About {APP_NAME}", accelerator="F1", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpm)
        self.config(menu=menubar)
        self.bind_all("<F1>", lambda e: self.show_about())

        self.bind_all("<Control-o>", lambda e: self.load_config_dialog())
        self.bind_all("<Control-s>", lambda e: self.save_config())
        self.bind_all("<Control-S>", lambda e: self.save_config_as())
        self.bind_all("<Control-comma>", lambda e: self.open_connection_settings())
        self.bind_all("<Control-p>", lambda e: self.toggle_pause())
        self.bind_all("<Control-q>", lambda e: self.exit_app())
        self.bind_all("<Control-g>", lambda e: self.set_chart_visible(not self.var_show_chart.get()))
        self.bind_all("<F5>", lambda e: (self.worker.refresh_now(), self.chart_worker.refresh_now()))

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(10, 10, 10, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Symbol").pack(side="left")
        self.var_symbol = tk.StringVar()
        self.symbol_box = ttk.Combobox(bar, textvariable=self.var_symbol, width=16,
                                       font=("Consolas", 11))
        self.symbol_box.pack(side="left", padx=(6, 6))
        self.symbol_box.bind("<Return>", lambda e: self.add_favorite())
        self.symbol_box.bind("<KeyRelease>", self._on_symbol_typed)
        self.symbol_box.bind("<<ComboboxSelected>>", lambda e: self.add_favorite())

        ttk.Button(bar, text="Add", style="Accent.TButton", command=self.add_favorite).pack(side="left")
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="▲", width=3, command=lambda: self.move_selected(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="▼", width=3, command=lambda: self.move_selected(1)).pack(side="left", padx=(2, 0))

        ttk.Checkbutton(bar, text="\U0001F4C8 Chart", style="Toggle.TCheckbutton", variable=self.var_show_chart,
                        command=lambda: self.set_chart_visible(self.var_show_chart.get())).pack(side="right", padx=(6, 0))
        ttk.Checkbutton(bar, textvariable=self.var_pause_text, style="Toggle.TCheckbutton", width=10,
                        variable=self.var_paused, command=self._update_pause).pack(side="right", padx=(14, 0))
        ttk.Label(bar, text="ms").pack(side="right")
        self.var_refresh = tk.StringVar(value="1000")
        spin = ttk.Spinbox(bar, from_=MIN_REFRESH_MS, to=60000, increment=250, width=7,
                           textvariable=self.var_refresh, command=self._on_refresh_changed)
        spin.pack(side="right", padx=(6, 4))
        spin.bind("<Return>", lambda e: self._on_refresh_changed())
        spin.bind("<FocusOut>", lambda e: self._on_refresh_changed())
        ttk.Label(bar, text="Refresh").pack(side="right")

    def _build_panes(self):
        self.panes = ttk.Panedwindow(self, orient="vertical")
        self.panes.pack(fill="both", expand=True, padx=10)
        table = ttk.Frame(self.panes)
        self._build_table(table)
        self.chart = PriceChart(self.panes, on_change=self._on_chart_options, price_fmt=self._chart_price_fmt)
        self.panes.add(table, weight=2)
        self.chart_visible = False
        self.chart_height = 420  # height the window grows by when the chart is shown

    def _place_sash(self):
        if not self.chart_visible:
            return
        self.update_idletasks()
        total = self.panes.winfo_height()
        table_h = max(200, total - self.chart_height)
        if total - table_h < 250:  # small window: split roughly 40/60
            table_h = max(180, int(total * 0.4))
        try:
            self.panes.sashpos(0, table_h)
        except tk.TclError:
            pass

    def set_chart_visible(self, show: bool, resize: bool = True):
        """Show/hide the chart pane. Hidden = no chart requests at all."""
        self.var_show_chart.set(show)
        if show == self.chart_visible:
            return
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if show:
            self.panes.add(self.chart, weight=3)
            self.chart_visible = True
            if resize:
                self.geometry(f"{w}x{min(h + self.chart_height, self.winfo_screenheight() - 80)}")
            # A newly added pane starts with zero height: place the divider so
            # the chart gets its space (table keeps at least ~5 rows).
            self.after(60, self._place_sash)
            sel = self.tree.selection()
            if sel:
                self.chart.set_symbol(sel[0])
                self.chart_worker.set_target(sel[0], self.chart.interval)
        else:
            ch = self.chart.winfo_height()
            if ch > 100:
                self.chart_height = ch
            self.panes.forget(self.chart)
            self.chart_visible = False
            self.chart.set_symbol(None)
            self.chart_worker.set_target(None, self.chart.interval)
            if resize:
                self.geometry(f"{w}x{max(h - self.chart_height - 6, 300)}")
        self._set_cfg("show_chart", show)

    def _build_table(self, frame):
        cols = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended", height=8)
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor,
                              command=lambda k=key: self.sort_by(k, toggle=True))
            self.tree.column(key, width=width, anchor=anchor, stretch=True)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("up", foreground=UP)
        self.tree.tag_configure("down", foreground=DOWN)
        self.tree.tag_configure("flat", foreground=FG)
        self.tree.tag_configure("flash_up", background=FLASH_UP)
        self.tree.tag_configure("flash_down", background=FLASH_DOWN)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", self._open_on_binance)

        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Open on Binance", command=lambda: self._open_on_binance(None))
        menu.add_command(label="Copy price", command=self._copy_price)
        menu.add_separator()
        menu.add_command(label="Move up", command=lambda: self.move_selected(-1))
        menu.add_command(label="Move down", command=lambda: self.move_selected(1))
        menu.add_command(label="Remove", command=self.remove_selected)
        self.row_menu = menu
        self.tree.bind("<Button-3>", self._show_row_menu)

    def _build_statusbar(self):
        outer = tk.Frame(self, bg=BG_ALT)
        outer.pack(side="bottom", fill="x")
        font = ("Segoe UI", 9)

        # Line 1: connection info
        line1 = tk.Frame(outer, bg=BG_ALT)
        line1.pack(fill="x", padx=10, pady=(5, 0))
        self.conn_dot = tk.Label(line1, text="●", bg=BG_ALT, fg=FG_DIM, font=("Segoe UI", 10))
        self.conn_dot.pack(side="left")
        self.var_conn_state = tk.StringVar(value="Connecting...")
        tk.Label(line1, textvariable=self.var_conn_state, bg=BG_ALT, fg=FG,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 12))
        tk.Label(line1, text="API", bg=BG_ALT, fg=FG_DIM, font=font).pack(side="left")
        self.var_conn_api = tk.StringVar(value="-")
        tk.Label(line1, textvariable=self.var_conn_api, bg=BG_ALT, fg=FG, font=font).pack(side="left", padx=(4, 12))
        tk.Label(line1, text="DNS", bg=BG_ALT, fg=FG_DIM, font=font).pack(side="left")
        self.var_conn_dns = tk.StringVar(value="-")
        tk.Label(line1, textvariable=self.var_conn_dns, bg=BG_ALT, fg=FG, font=font).pack(side="left", padx=(4, 0))

        # Line 2: refresh stats + config file
        line2 = tk.Frame(outer, bg=BG_ALT)
        line2.pack(fill="x", padx=10, pady=(2, 5))
        self.var_status = tk.StringVar(value="")
        self.var_config = tk.StringVar()
        tk.Label(line2, textvariable=self.var_status, bg=BG_ALT, fg=FG_DIM, font=font).pack(side="left")
        tk.Label(line2, textvariable=self.var_config, bg=BG_ALT, fg=FG_DIM, font=font).pack(side="right")

    def _update_conn_bar(self):
        host = self.config_data.get("api_host")
        if self._eff_paused:
            self.conn_dot.config(fg=FG_DIM)
            self.var_conn_state.set("Paused" if self.var_paused.get() else "Paused (hidden)")
        elif self.last_error:
            self.conn_dot.config(fg=DOWN)
            self.var_conn_state.set("Disconnected")
        elif self.conn_info:
            self.conn_dot.config(fg=UP)
            self.var_conn_state.set("Connected")
        else:
            self.conn_dot.config(fg=ACCENT)
            self.var_conn_state.set("Connecting...")
        info = self.conn_info
        if info:
            route = [info["ip"]] if info["ip"] else []
            if info.get("proxy"):
                route.append(f"via {info['proxy']}")
            self.var_conn_api.set(f"https://{info['host']}  ({', '.join(route)})")
            # The server that actually answered (may be a fallback, e.g. DoT).
            self.var_conn_dns.set(info["dns"])
        else:
            proxy = self.proxy
            self.var_conn_api.set(f"https://{host}" + (f"  (via {proxy.label})" if proxy else ""))
            self.var_conn_dns.set("resolved by proxy" if proxy and proxy.remote_dns else self.resolver.label)

    # ---------------- Config ----------------
    def _load_initial_config(self):
        if DEFAULT_CONFIG_PATH.exists():
            try:
                self._apply_config(self._read_config(DEFAULT_CONFIG_PATH), DEFAULT_CONFIG_PATH)
                return
            except Exception as e:
                messagebox.showwarning(APP_NAME, f"Could not read {DEFAULT_CONFIG_PATH.name}:\n{e}\n\nUsing defaults.")
        self._apply_config(dict(DEFAULT_CONFIG), DEFAULT_CONFIG_PATH)
        self.dirty = not DEFAULT_CONFIG_PATH.exists()
        self._update_title()

    @staticmethod
    def _read_config(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config root must be a JSON object")
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(data)
        favs = []
        for s in cfg.get("favorites") or []:
            s = str(s).strip().upper()
            if s and s not in favs:
                favs.append(s)
        cfg["favorites"] = favs
        try:
            cfg["refresh_ms"] = max(MIN_REFRESH_MS, int(cfg["refresh_ms"]))
        except (TypeError, ValueError):
            cfg["refresh_ms"] = DEFAULT_CONFIG["refresh_ms"]
        return cfg

    def _apply_config(self, cfg: dict, path: Path):
        self.config_data = cfg
        self.config_path = path
        self.var_refresh.set(str(cfg["refresh_ms"]))
        self.var_top.set(bool(cfg.get("always_on_top")))
        self.var_flash.set(bool(cfg.get("flash", True)))
        self.attributes("-topmost", self.var_top.get())
        if cfg.get("geometry"):
            try:
                self.geometry(cfg["geometry"])
            except tk.TclError:
                pass
        self._rebuild_rows()
        self.worker.configure(symbols=cfg["favorites"], interval_ms=cfg["refresh_ms"])
        self._apply_network()
        self.chart.set_options(cfg.get("chart_interval", "15m"), cfg.get("chart_style", "candles"),
                               cfg.get("chart_indicators"))
        self.chart.set_symbol(None)
        self.chart_worker.set_target(None, self.chart.interval)
        if cfg["favorites"]:
            self.tree.selection_set(cfg["favorites"][0])
        # The saved geometry already includes the chart's space, so don't resize.
        self.set_chart_visible(bool(cfg.get("show_chart")), resize=False)
        self.dirty = False
        self._update_title()

    def _collect_config(self) -> dict:
        cfg = dict(self.config_data)
        cfg["favorites"] = list(self.tree.get_children())
        cfg["geometry"] = self.geometry()
        return cfg

    def _write_config(self, path: Path) -> bool:
        cfg = self._collect_config()
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            tmp.replace(path)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not save config:\n{e}")
            return False
        self.config_data = cfg
        self.config_path = path
        self.dirty = False
        self._update_title()
        self.var_status.set(f"Saved {path.name}")
        return True

    def save_config(self):
        return self._write_config(self.config_path)

    def save_config_as(self):
        p = filedialog.asksaveasfilename(
            title="Save config as", initialdir=str(APP_DIR), initialfile=self.config_path.name,
            defaultextension=".json", filetypes=[("JSON config", "*.json"), ("All files", "*.*")])
        if p:
            return self._write_config(Path(p))
        return False

    def load_config_dialog(self):
        if not self._confirm_discard():
            return
        p = filedialog.askopenfilename(
            title="Load config", initialdir=str(APP_DIR),
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")])
        if not p:
            return
        try:
            cfg = self._read_config(Path(p))
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not load config:\n{e}")
            return
        self._apply_config(cfg, Path(p))
        self.var_status.set(f"Loaded {Path(p).name}")

    def reset_defaults(self):
        if not self._confirm_discard():
            return
        self._apply_config(dict(DEFAULT_CONFIG), self.config_path)
        self.dirty = True
        self._update_title()

    def _set_cfg(self, key, value):
        if self.config_data.get(key) != value:
            self.config_data[key] = value
            self._mark_dirty()

    def _mark_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        star = " *" if self.dirty else ""
        self.title(f"{APP_NAME} - {self.config_path.name}{star}")
        path = str(self.config_path)
        if len(path) > 48:  # keep long paths from overlapping the stats text
            path = "…" + path[-47:]
        self.var_config.set(f"Config: {path}{' (unsaved changes)' if self.dirty else ''}")

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_NAME, f"Save changes to {self.config_path.name}?")
        if ans is None:
            return False
        if ans:
            return self.save_config()
        return True

    # ---------------- Favorites ----------------
    def _rebuild_rows(self):
        self.tree.delete(*self.tree.get_children())
        self.last_prices.clear()
        self.raw_rows.clear()
        for s in self.config_data["favorites"]:
            self.tree.insert("", "end", iid=s, values=(s, "...", "", "", "", "", ""), tags=("flat",))

    def _favorites_changed(self):
        favs = list(self.tree.get_children())
        self.config_data["favorites"] = favs
        self.worker.configure(symbols=favs)
        if self.chart.symbol and not self.tree.exists(self.chart.symbol):
            if favs:
                self.tree.selection_set(favs[0])
            else:
                self.chart.set_symbol(None)
                self.chart_worker.set_target(None, self.chart.interval)
        self._mark_dirty()

    def add_favorite(self):
        s = self.var_symbol.get().strip().upper().replace("/", "").replace("-", "")
        if not s:
            return
        if self.exchange and s not in self.exchange:
            # Allow shorthand like "BTC" -> BTCUSDT
            if s + "USDT" in self.exchange:
                s += "USDT"
            else:
                self.var_status.set(f"Unknown symbol: {s}")
                self.bell()
                return
        if self.tree.exists(s):
            self.tree.selection_set(s)
            self.tree.see(s)
            self.var_symbol.set("")
            return
        self.tree.insert("", "end", iid=s, values=(s, "...", "", "", "", "", ""), tags=("flat",))
        self.tree.selection_set(s)
        self.tree.see(s)
        self.var_symbol.set("")
        self._favorites_changed()
        self.worker.refresh_now()

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        for s in sel:
            self.tree.delete(s)
            self.last_prices.pop(s, None)
            self.raw_rows.pop(s, None)
        self._favorites_changed()

    def move_selected(self, delta: int):
        sel = list(self.tree.selection())
        if not sel:
            return
        # Manual ordering overrides column sorting.
        self.config_data["sort_column"] = None
        self._update_heading_arrows()
        items = list(self.tree.get_children())
        order = sorted(sel, key=items.index, reverse=delta > 0)
        for s in order:
            idx = self.tree.index(s)
            new = idx + delta
            if 0 <= new < len(items):
                self.tree.move(s, "", new)
        self.tree.see(sel[0])
        self._favorites_changed()

    def _on_symbol_typed(self, event):
        if event.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
            return
        text = self.var_symbol.get().strip().upper()
        if not text or not self.all_symbols:
            self.symbol_box["values"] = ()
            return
        starts = [s for s in self.all_symbols if s.startswith(text)]
        contains = [s for s in self.all_symbols if text in s and not s.startswith(text)] if len(starts) < 40 else []
        self.symbol_box["values"] = (starts + contains)[:60]

    # ---------------- Settings ----------------
    def _on_refresh_changed(self):
        try:
            ms = max(MIN_REFRESH_MS, int(float(self.var_refresh.get())))
        except ValueError:
            ms = self.config_data["refresh_ms"]
        self.var_refresh.set(str(ms))
        self.worker.configure(interval_ms=ms)
        self._set_cfg("refresh_ms", ms)

    def _on_top_toggle(self):
        self.attributes("-topmost", self.var_top.get())
        self._set_cfg("always_on_top", self.var_top.get())

    # ---------------- About ----------------
    def show_about(self):
        if getattr(self, "_about_win", None) and self._about_win.winfo_exists():
            self._about_win.lift()
            return
        win = tk.Toplevel(self)
        self._about_win = win
        win.title(f"About {APP_NAME}")
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(False, False)
        body = tk.Frame(win, bg=BG, padx=28, pady=22)
        body.pack()

        png = RES_DIR / "icon.png"
        if png.exists():
            try:
                img = tk.PhotoImage(file=str(png)).subsample(3)  # 256 -> ~85 px
                win._logo = img
                tk.Label(body, image=img, bg=BG).pack(pady=(0, 10))
            except tk.TclError:
                pass
        tk.Label(body, text=APP_NAME, bg=BG, fg=FG, font=("Segoe UI", 15, "bold")).pack()
        tk.Label(body, text=f"Version {APP_VERSION}", bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack()
        tk.Label(body, text="Fast desktop viewer for Binance spot prices with favorites,\n"
                            "live charts & indicators, and encrypted DNS (DoH / DoT).",
                 bg=BG, fg=FG, font=("Segoe UI", 9), justify="center").pack(pady=(12, 12))
        link = tk.Label(body, text=APP_URL, bg=BG, fg="#3d9df3", cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        link.pack()
        link.bind("<Button-1>", lambda e: webbrowser.open(APP_URL))
        tk.Label(body, text=f"© {time.strftime('%Y')} {APP_AUTHOR}. All rights reserved.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(pady=(12, 0))
        tk.Label(body, text=f"Python {sys.version.split()[0]}  ·  Tk {tk.TkVersion}  ·  "
                            "market data from Binance public API",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8)).pack(pady=(4, 14))
        ttk.Button(body, text="OK", style="Accent.TButton", command=win.destroy).pack()
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + max(0, (self.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()

    # ---------------- Network / DNS ----------------
    def _apply_network(self):
        """(Re)create the resolver from config and hand it to both workers."""
        host = self.config_data.get("api_host") or DEFAULT_CONFIG["api_host"]
        self.resolver = make_resolver(self.config_data)
        self.proxy = make_proxy(self.config_data, host)[0]
        self.conn_info = None
        self.last_error = None
        self.exchange.clear()
        self.worker.set_network(host, self.resolver, self.proxy)
        self.chart_worker.set_network(host, self.resolver, self.proxy)
        self._update_conn_bar()

    def open_connection_settings(self, tab: int = 0):
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_nb.select(tab)
            self._settings_win.deiconify()
            self._settings_win.lift()
            return
        if self.state() != "normal":
            self.show_window()
        win = tk.Toplevel(self)
        self._settings_win = win
        win.title("Settings")
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both")
        nb = ttk.Notebook(body)
        nb.pack(fill="both")
        self._settings_nb = nb
        small = ("Segoe UI", 8)

        cfg = self.config_data
        prov_keys = list(DNS_PROVIDERS)
        prov_labels = [DNS_PROVIDERS[k][0] + (f"  ({', '.join(DNS_PROVIDERS[k][1])})" if DNS_PROVIDERS[k][1] else "")
                       for k in prov_keys]
        mode_keys = list(DNS_MODES)
        mode_labels = [DNS_MODES[k] for k in mode_keys]
        ptype_keys = list(PROXY_TYPES)
        ptype_labels = [PROXY_TYPES[k] for k in ptype_keys]

        # ---- Tab 1: Connection (API host + proxy) ----
        tab_conn = ttk.Frame(nb, padding=10)
        nb.add(tab_conn, text="  Connection  ")
        api = ttk.Labelframe(tab_conn, text=" Binance API ", padding=10)
        api.pack(fill="x")
        ttk.Label(api, text="REST host").grid(row=0, column=0, sticky="w")
        var_host = tk.StringVar(value=cfg.get("api_host", DEFAULT_CONFIG["api_host"]))
        ttk.Combobox(api, textvariable=var_host, values=API_HOSTS, width=44).grid(
            row=0, column=1, padx=(10, 0), sticky="we")

        prx = ttk.Labelframe(tab_conn, text=" Proxy ", padding=10)
        prx.pack(fill="x", pady=(10, 0))
        ttk.Label(prx, text="Type").grid(row=0, column=0, sticky="w")
        cur_pt = cfg.get("proxy_type", "none")
        var_ptype = tk.StringVar(value=PROXY_TYPES.get(cur_pt, PROXY_TYPES["none"]))
        cb_ptype = ttk.Combobox(prx, textvariable=var_ptype, values=ptype_labels, state="readonly", width=44)
        cb_ptype.grid(row=0, column=1, columnspan=3, padx=(10, 0), sticky="we")

        ttk.Label(prx, text="Host").grid(row=1, column=0, sticky="w", pady=(8, 0))
        var_phost = tk.StringVar(value=cfg.get("proxy_host", ""))
        ent_phost = ttk.Entry(prx, textvariable=var_phost, width=30)
        ent_phost.grid(row=1, column=1, padx=(10, 0), pady=(8, 0), sticky="we")
        ttk.Label(prx, text="Port").grid(row=1, column=2, sticky="e", padx=(10, 0), pady=(8, 0))
        var_pport = tk.StringVar(value=str(cfg.get("proxy_port", 8080)))
        ent_pport = ttk.Entry(prx, textvariable=var_pport, width=7)
        ent_pport.grid(row=1, column=3, padx=(6, 0), pady=(8, 0), sticky="e")

        ttk.Label(prx, text="Username").grid(row=2, column=0, sticky="w", pady=(8, 0))
        var_puser = tk.StringVar(value=cfg.get("proxy_user", ""))
        ent_puser = ttk.Entry(prx, textvariable=var_puser, width=30)
        ent_puser.grid(row=2, column=1, padx=(10, 0), pady=(8, 0), sticky="we")
        ttk.Label(prx, text="Password").grid(row=3, column=0, sticky="w", pady=(8, 0))
        var_ppass = tk.StringVar(value=cfg.get("proxy_password", ""))
        ent_ppass = ttk.Entry(prx, textvariable=var_ppass, width=30, show="•")
        ent_ppass.grid(row=3, column=1, padx=(10, 0), pady=(8, 0), sticky="we")
        ttk.Label(prx, text="optional", style="Dim.TLabel", font=small).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))

        var_premote = tk.BooleanVar(value=bool(cfg.get("proxy_remote_dns", True)))
        chk_premote = ttk.Checkbutton(prx, variable=var_premote,
                                      text="Let the proxy look up hostnames (skips local DNS; recommended)")
        chk_premote.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        var_pinfo = tk.StringVar()
        ttk.Label(prx, textvariable=var_pinfo, style="Dim.TLabel", font=small, justify="left",
                  wraplength=460).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- Tab 2: DNS ----
        tab_dns = ttk.Frame(nb, padding=10)
        nb.add(tab_dns, text="  DNS  ")
        dns = ttk.Labelframe(tab_dns, text=" DNS resolver ", padding=10)
        dns.pack(fill="x")
        ttk.Label(dns, text="Provider").grid(row=0, column=0, sticky="w")
        cur_prov = cfg.get("dns_provider", "cloudflare")
        var_prov = tk.StringVar(value=prov_labels[prov_keys.index(cur_prov)] if cur_prov in prov_keys
                                else prov_labels[0])
        cb_prov = ttk.Combobox(dns, textvariable=var_prov, values=prov_labels, state="readonly", width=44)
        cb_prov.grid(row=0, column=1, padx=(10, 0), sticky="we")

        ttk.Label(dns, text="Protocol").grid(row=1, column=0, sticky="w", pady=(8, 0))
        var_mode = tk.StringVar(value=DNS_MODES.get(cfg.get("dns_mode", "doh"), DNS_MODES["doh"]))
        cb_mode = ttk.Combobox(dns, textvariable=var_mode, values=mode_labels, state="readonly", width=44)
        cb_mode.grid(row=1, column=1, padx=(10, 0), pady=(8, 0), sticky="we")

        ttk.Label(dns, text="Custom servers").grid(row=2, column=0, sticky="w", pady=(8, 0))
        var_custom = tk.StringVar(value=cfg.get("dns_custom", ""))
        ent_custom = ttk.Entry(dns, textvariable=var_custom, width=46)
        ent_custom.grid(row=2, column=1, padx=(10, 0), pady=(8, 0), sticky="we")
        ttk.Label(dns, text="IP addresses, comma separated (used when Provider = Custom)",
                  style="Dim.TLabel", font=small).grid(row=3, column=1, sticky="w", padx=(10, 0))
        var_fallback = tk.BooleanVar(value=bool(cfg.get("dns_fallback", True)))
        chk_fallback = ttk.Checkbutton(
            dns, variable=var_fallback,
            text="If blocked, automatically try other encrypted DNS (Quad9, AdGuard, OpenDNS...)")
        chk_fallback.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(dns, text="Tip: many ISPs hijack plain DNS (UDP 53). DNS over HTTPS / TLS is encrypted\n"
                            "so Binance hostnames resolve to their real servers. Office networks often\n"
                            "block Cloudflare and Google DNS; the fallback finds one that is allowed.\n"
                            "Not used when a proxy looks up hostnames.",
                  style="Dim.TLabel", font=small, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ---- Tab 3: Window & tray ----
        tab_tray = ttk.Frame(nb, padding=10)
        nb.add(tab_tray, text="  Window & tray  ")
        trayf = ttk.Labelframe(tab_tray, text=" System tray ", padding=10)
        trayf.pack(fill="x")
        tray_opts = [
            ("close_to_tray", "Close button (X) hides the window to the tray instead of exiting"),
            ("minimize_to_tray", "Minimize button hides the window to the tray"),
            ("start_in_tray", "Start hidden in the tray"),
        ]
        tray_vars = {}
        for i, (key, text) in enumerate(tray_opts):
            tray_vars[key] = tk.BooleanVar(value=bool(cfg.get(key, DEFAULT_CONFIG[key])))
            cb = ttk.Checkbutton(trayf, text=text, variable=tray_vars[key])
            cb.grid(row=i, column=0, sticky="w", pady=(0 if i == 0 else 6, 0))
            if not self.tray:
                cb.state(["disabled"])
        ttk.Label(trayf, style="Dim.TLabel", font=small, justify="left", text=(
            "Left-click the tray icon to show or hide the window. Right-click it for\n"
            "Pause updates and Exit. File → Exit (Ctrl+Q) also quits."
            if self.tray else "The system tray isn't available here; the window minimizes instead.")).grid(
            row=len(tray_opts), column=0, sticky="w", pady=(10, 0))

        powf = ttk.Labelframe(tab_tray, text=" Data and battery ", padding=10)
        powf.pack(fill="x", pady=(10, 0))
        tray_vars["pause_when_hidden"] = tk.BooleanVar(value=bool(cfg.get("pause_when_hidden", True)))
        ttk.Checkbutton(powf, variable=tray_vars["pause_when_hidden"],
                        text="Pause updates while the window is hidden or minimized").grid(
            row=0, column=0, sticky="w")
        ttk.Label(powf, style="Dim.TLabel", font=small, justify="left", text=(
            "Paused means no network requests at all: the connection is closed and\n"
            "prices refresh as soon as the window is shown again. Use the ⏸ Pause button\n"
            "(Ctrl+P) to pause manually at any time.")).grid(row=1, column=0, sticky="w", pady=(8, 0))

        def sync_state(*_):
            key = prov_keys[prov_labels.index(var_prov.get())]
            ent_custom.state(["!disabled"] if key == "custom" else ["disabled"])
            cb_mode.state(["disabled"] if key == "system" else ["!disabled", "readonly"])
            chk_fallback.state(["disabled"] if key == "system" else ["!disabled"])
            pt = ptype_keys[ptype_labels.index(var_ptype.get())]
            manual = ["!disabled"] if pt in ("http", "socks5") else ["disabled"]
            for w in (ent_phost, ent_pport, ent_puser, ent_ppass):
                w.state(manual)
            chk_premote.state(["disabled"] if pt == "none" else ["!disabled"])
            if pt == "system":
                var_pinfo.set(system_proxy(var_host.get().strip())[1])
            elif pt in ("http", "socks5"):
                var_pinfo.set("Ask your IT department for the proxy address. The username and password "
                              "are saved in plain text in config.json.")
            else:
                var_pinfo.set("Connects directly to Binance.")

        def on_ptype(_e=None):
            pt = ptype_keys[ptype_labels.index(var_ptype.get())]
            if pt in ("http", "socks5") and var_pport.get().strip() in ("", "8080", "1080"):
                var_pport.set("1080" if pt == "socks5" else "8080")
            sync_state()
        cb_prov.bind("<<ComboboxSelected>>", sync_state)
        cb_ptype.bind("<<ComboboxSelected>>", on_ptype)
        sync_state()

        def collect() -> dict | None:
            host = var_host.get().strip().replace("https://", "").replace("http://", "").strip("/")
            pt = ptype_keys[ptype_labels.index(var_ptype.get())]
            try:
                port = int(var_pport.get().strip() or 0)
                if pt in ("http", "socks5") and not 0 < port < 65536:
                    raise ValueError
            except ValueError:
                nb.select(0)
                messagebox.showerror(APP_NAME, "Proxy port must be a number from 1 to 65535.", parent=win)
                return None
            if pt in ("http", "socks5") and not var_phost.get().strip():
                nb.select(0)
                messagebox.showerror(APP_NAME, "Enter the proxy host.", parent=win)
                return None
            new = {
                "api_host": host or DEFAULT_CONFIG["api_host"],
                "proxy_type": pt,
                "proxy_host": var_phost.get().strip(),
                "proxy_port": port or DEFAULT_CONFIG["proxy_port"],
                "proxy_user": var_puser.get().strip(),
                "proxy_password": var_ppass.get(),
                "proxy_remote_dns": bool(var_premote.get()),
                "dns_provider": prov_keys[prov_labels.index(var_prov.get())],
                "dns_mode": mode_keys[mode_labels.index(var_mode.get())],
                "dns_custom": var_custom.get().strip(),
                "dns_fallback": bool(var_fallback.get()),
            }
            new.update({k: bool(v.get()) for k, v in tray_vars.items()})
            return new

        var_result = tk.StringVar(value="")
        lbl_result = tk.Label(body, textvariable=var_result, bg=BG, fg=FG_DIM, justify="left",
                              anchor="w", font=("Consolas", 9), wraplength=520)
        lbl_result.pack(fill="x", pady=(10, 0))

        def show_result(text, color):
            if win.winfo_exists():
                var_result.set(text)
                lbl_result.config(fg=color)

        def test():
            new = collect()
            if new is None:
                return
            show_result("Testing...", FG_DIM)

            def run():
                t0 = time.perf_counter()
                proxy = make_proxy(new, new["api_host"])[0]
                client = BinanceClient(new["api_host"], make_resolver(new), timeout=8, proxy=proxy)
                try:
                    client.tickers(["BTCUSDT"])
                    t_all = (time.perf_counter() - t0) * 1000
                    info = client.conn_info
                    text = (f"OK    {new['api_host']} -> {info['ip'] or 'via proxy'}\n"
                            f"Route {info.get('proxy') or 'direct'}\n"
                            f"DNS   {info['dns']}\n"
                            f"Total {t_all:.0f} ms (DNS + proxy + TLS + first request)")
                    color = UP
                except Exception as e:
                    text, color = f"FAILED: {e}", DOWN
                finally:
                    client.close()
                self.after(0, show_result, text, color)
            threading.Thread(target=run, daemon=True).start()

        net_keys = ("api_host", "proxy_type", "proxy_host", "proxy_port", "proxy_user", "proxy_password",
                    "proxy_remote_dns", "dns_provider", "dns_mode", "dns_custom", "dns_fallback")

        def apply(close: bool):
            new = collect()
            if new is None:
                return
            if new["dns_provider"] == "custom" and not new["dns_custom"]:
                nb.select(1)
                messagebox.showerror(APP_NAME, "Enter at least one custom DNS server IP.", parent=win)
                return
            changed = {k for k, v in new.items() if self.config_data.get(k) != v}
            if changed:
                self.config_data.update(new)
                self._mark_dirty()
                if changed & set(net_keys):
                    self._apply_network()
                self._update_pause()
            if close:
                win.destroy()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Test connection", command=test).pack(side="left")
        ttk.Button(btns, text="OK", style="Accent.TButton", command=lambda: apply(True)).pack(side="right")
        ttk.Button(btns, text="Apply", command=lambda: apply(False)).pack(side="right", padx=6)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())
        nb.select(tab)
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, self.winfo_rooty() + 40)}")
        win.grab_set()

    # ---------------- Chart ----------------
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if self.chart_visible and sel and sel[0] != self.chart.symbol:
            self.chart.set_symbol(sel[0])
            self.chart_worker.set_target(sel[0], self.chart.interval)

    def _on_chart_options(self, interval: str, style: str, indicators: dict):
        self._set_cfg("chart_interval", interval)
        self._set_cfg("chart_style", style)
        self._set_cfg("chart_indicators", indicators)
        if self.chart_visible:
            self.chart_worker.set_target(self.chart.symbol, interval)

    def _chart_price_fmt(self, symbol, value: float) -> str:
        tick = self.exchange.get(symbol, {}).get("tick") if symbol else None
        if tick is not None and tick > 0:
            return f"{value:,.{max(0, -tick.as_tuple().exponent)}f}"
        if value >= 100:
            return f"{value:,.2f}"
        return f"{value:,.6g}"

    # ---------------- Sorting ----------------
    def _sort_key(self, sym: str, col: str):
        r = self.raw_rows.get(sym)
        if col == "symbol":
            return sym
        if r is None:
            return float("-inf")
        field = {"price": "lastPrice", "change": "priceChange", "pct": "priceChangePercent",
                 "high": "highPrice", "low": "lowPrice", "volume": "quoteVolume"}[col]
        try:
            return float(r[field])
        except (KeyError, ValueError):
            return float("-inf")

    def sort_by(self, col: str, toggle=False):
        if toggle:
            if self.config_data.get("sort_column") == col:
                self.config_data["sort_reverse"] = not self.config_data.get("sort_reverse")
            else:
                self.config_data["sort_column"] = col
                self.config_data["sort_reverse"] = col != "symbol"
            self._mark_dirty()
        items = list(self.tree.get_children())
        ordered = sorted(items, key=lambda s: self._sort_key(s, col),
                         reverse=bool(self.config_data.get("sort_reverse")))
        if ordered != items:
            for i, s in enumerate(ordered):
                self.tree.move(s, "", i)
            if toggle:
                self.config_data["favorites"] = ordered
                self.worker.configure(symbols=ordered)
        self._update_heading_arrows()

    def _update_heading_arrows(self):
        col = self.config_data.get("sort_column")
        rev = self.config_data.get("sort_reverse")
        for key, title, _, _ in COLUMNS:
            arrow = (" ▼" if rev else " ▲") if key == col else ""
            self.tree.heading(key, text=title + arrow)

    # ---------------- Data updates ----------------
    def _drain_queue(self):
        latest_tickers = None
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "tray":
                    self._on_tray(msg[1])
                    continue
                if self._eff_paused and kind in ("tickers", "conn", "error"):
                    continue  # a request that was already in flight when pausing
                if kind == "tickers":
                    latest_tickers = msg  # only render the newest batch
                    if self.last_error:
                        self.last_error = None
                        self._update_conn_bar()
                elif kind == "exchange_info":
                    self.exchange = msg[1]
                    self.all_symbols = sorted(self.exchange)
                    self._drop_unknown_favorites()
                    self.chart.schedule_redraw()  # axis labels can now use tick sizes
                elif kind == "conn":
                    self.conn_info = msg[1]
                    self._update_conn_bar()
                elif kind == "klines":
                    self.chart.set_data(msg[1], msg[2], msg[3])
                elif kind == "chart_error":
                    self.chart.set_error(msg[1], msg[2], msg[3])
                elif kind == "error":
                    self.last_error = msg[1]
                    self.var_status.set(f"Error: {msg[1]}")
                    self._update_conn_bar()
        except queue.Empty:
            pass
        if latest_tickers is not None:
            self._render_tickers(latest_tickers[1], latest_tickers[2])
        self._drain_job = self.after(30, self._drain_queue)

    def _drop_unknown_favorites(self):
        bad = [s for s in self.tree.get_children() if s not in self.exchange]
        if bad:
            for s in bad:
                self.tree.delete(s)
            self._favorites_changed()
            messagebox.showwarning(APP_NAME, "Removed unknown / non-trading symbols:\n" + ", ".join(bad))

    def _render_tickers(self, data: list[dict], latency: float):
        flash = self.var_flash.get()
        for t in data:
            s = t.get("symbol")
            if not s or not self.tree.exists(s):
                continue
            self.raw_rows[s] = t
            tick = self.exchange.get(s, {}).get("tick")
            try:
                price = Decimal(t["lastPrice"])
                change = Decimal(t["priceChange"])
                pct = float(t["priceChangePercent"])
            except (KeyError, ArithmeticError, ValueError):
                continue
            trend = "up" if change > 0 else "down" if change < 0 else "flat"
            values = (
                s,
                fmt_price(t["lastPrice"], tick),
                ("+" if change > 0 else "") + fmt_price(t["priceChange"], tick),
                f"{pct:+.2f}%",
                fmt_price(t["highPrice"], tick),
                fmt_price(t["lowPrice"], tick),
                fmt_volume(t.get("quoteVolume", "0")),
            )
            prev = self.last_prices.get(s)
            self.last_prices[s] = price
            tags = [trend]
            if flash and prev is not None and price != prev:
                tags.append("flash_up" if price > prev else "flash_down")
                job = self.flash_jobs.pop(s, None)
                if job:
                    self.after_cancel(job)
                self.flash_jobs[s] = self.after(450, self._unflash, s, trend)
            elif s in self.flash_jobs:
                # keep current flash running; just update the trend color
                tags.append(next((tg for tg in self.tree.item(s, "tags") if tg.startswith("flash")), ""))
            self.tree.item(s, values=values, tags=[tg for tg in tags if tg])
            if s == self.chart.symbol:
                self.chart.update_last(s, float(price))

        col = self.config_data.get("sort_column")
        if col and col != "symbol":
            self.sort_by(col)
        n = len(self.tree.get_children())
        tips = []
        for s in self.tree.get_children()[:4]:
            v = self.tree.item(s, "values")
            if v and v[1] not in ("", "..."):
                tips.append(f"{s} {v[1]} {v[3]}")
        self._tip_lines = tips
        self._update_tray_tip()
        self.var_status.set(f"{n} symbols   ·   latency {latency:.0f} ms   ·   "
                            f"updated {time.strftime('%H:%M:%S')}   ·   every {self.config_data['refresh_ms']} ms"
                            f"   ·   data {fmt_bytes(TRAFFIC['down'] + TRAFFIC['up'])}")

    def _unflash(self, s: str, _trend: str):
        self.flash_jobs.pop(s, None)
        if self.tree.exists(s):
            tags = [tg for tg in self.tree.item(s, "tags") if not tg.startswith("flash")]
            self.tree.item(s, tags=tags)

    # ---------------- Misc actions ----------------
    def _show_row_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.row_menu.tk_popup(event.x_root, event.y_root)

    def _open_on_binance(self, event):
        if event is not None and self.tree.identify_region(event.x, event.y) != "cell":
            return
        sel = self.tree.selection()
        if not sel:
            return
        s = sel[0]
        info = self.exchange.get(s)
        pair = f"{info['base']}_{info['quote']}" if info else s
        webbrowser.open(f"https://www.binance.com/en/trade/{pair}?type=spot")

    def _copy_price(self):
        sel = self.tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(self.tree.set(sel[0], "price").replace(",", ""))

    def on_close(self):  # kept for compatibility
        self.exit_app()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI Windows
        # Own taskbar identity, so Windows shows our icon instead of Python's.
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("BinancePriceViewer.App")
    except Exception:
        pass
    app = App()
    app._update_heading_arrows()
    app.mainloop()


if __name__ == "__main__":
    main()
