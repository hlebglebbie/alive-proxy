#!/usr/bin/env python3
"""
ProxyPilot — GUI-тестер бесплатных прокси (tkinter, тёмная тема в духе Karing).

Источники списков: free-proxy (sslproxies.org, free-proxy-list.net) +
агрегаторы GitHub (TheSpeedX, monosans, clarketm, mmpx12, roosterkid),
ProxyScrape API, proxy-list.download, geonode API.

Возможности:
  * сбор прокси со всех источников сразу (параллельно), дедупликация;
  * массовая параллельная проверка: быстрая проба google.com/generate_204
    ЧЕРЕЗ прокси (строго 204, пустое тело, замер задержки) + полная проверка
    (реальная страница через HTTPS-туннель) по клику;
  * двойной клик / кнопка — поставить прокси системным (GNOME + env-файл);
  * большая кнопка подключения: автоподбор лучшего (минимальная задержка,
    прошедший полную проверку), сторож автозамены умершего;
  * запуск: ./gui.sh  (или .venv/bin/python proxy_gui.py)

Ядро логики (проверки, GNOME, env) — в alive_proxy.py, CLI остаётся как есть.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from urllib.parse import urlsplit

import requests

import alive_proxy as core  # переиспользуем проверки, GNOME, env, состояние

log = logging.getLogger("proxypilot")

# ------------------------------------------------------------------ тема

BG      = "#0e1116"   # фон окна
CARD    = "#161a23"   # карточки
CARD2   = "#1d2230"   # приподнятые элементы
LINE    = "#262c3a"   # разделители
ACCENT  = "#3b82f6"   # акцент (синий)
ACCENT2 = "#60a5fa"
GREEN   = "#22c55e"   # подключено / живой
RED     = "#ef4444"   # мёртвый / ошибка
AMBER   = "#f59e0b"   # в процессе
TEXT    = "#e5e7eb"
MUT     = "#8b93a7"

APP_TITLE = "ProxyPilot — живой бесплатный прокси"

# ------------------------------------------------------------- источники

IP_PORT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ProxyPilot/1.0"}


def _get(url: str, timeout: float = 15.0) -> str:
    s = core._session()
    try:
        r = s.get(url, timeout=timeout, headers=HTTP_HEADERS)
        r.raise_for_status()
        return r.text
    finally:
        s.close()


def _from_text(text: str) -> list[str]:
    return [f"http://{h}:{p}" for h, p in IP_PORT_RE.findall(text)]


def _geonode() -> list[str]:
    url = ("https://proxylist.geonode.com/api/proxy-list?limit=500&page=1"
           "&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps")
    out = []
    for item in json.loads(_get(url)).get("data", []):
        ip, port = item.get("ip"), str(item.get("port") or "")
        if ip and port:
            out.append(f"http://{ip}:{port}")
    return out


def _freeproxy(repeat: int) -> list[str]:
    from fp.fp import FreeProxy
    return [str(x) for x in FreeProxy().get_proxy_list(repeat)]


SOURCES: list[tuple[str, object]] = [
    ("free-proxy / sslproxies.org",      lambda: _freeproxy(0)),
    ("free-proxy / free-proxy-list.net", lambda: _freeproxy(1)),
    ("TheSpeedX (github)", lambda: _from_text(_get(
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"))),
    ("monosans (github)", lambda: _from_text(_get(
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"))),
    ("clarketm (github)", lambda: _from_text(_get(
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"))),
    ("mmpx12 (github)", lambda: _from_text(_get(
        "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"))),
    ("roosterkid (github)", lambda: _from_text(_get(
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"))),
    ("ProxyScrape API", lambda: _from_text(_get(
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
        "&timeout=10000&country=all&ssl=all&anonymity=all"))),
    ("proxy-list.download", lambda: _from_text(_get(
        "https://www.proxy-list.download/api/v1/get?type=http"))),
    ("geonode API", _geonode),
]


def normalize(url: str) -> str | None:
    """Любую строку (ip:port, http://ip:port:HTTP...) приводим к http://ip:port."""
    m = IP_PORT_RE.search(url)
    return f"http://{m.group(1)}:{m.group(2)}" if m else None


def fetch_sources(enabled: list[str], per_source_limit: int = 1500) -> dict[str, str]:
    """Параллельно опросить выбранные источники. Возвращает {прокси: источник}."""
    chosen = [(name, fn) for name, fn in SOURCES if name in enabled]

    def grab(item):
        name, fn = item
        try:
            got = []
            for u in fn():
                n = normalize(str(u))
                if n:
                    got.append(n)
            return name, got[:per_source_limit]
        except Exception as e:
            log.warning("источник %s не сработал: %s", name, e)
            return name, []

    merged: dict[str, str] = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(2, len(chosen))) as ex:
        for name, proxies in ex.map(grab, chosen):
            for p in proxies:
                merged.setdefault(p, name)   # дедуп: первый источник выигрывает
    return merged


# ================================================================== GUI

class ProxyPilot(tk.Tk):
    STATE_OFF, STATE_SEARCH, STATE_ON = "off", "search", "on"

    def __init__(self):
        super().__init__(className="ProxyPilot")   # WM_CLASS для ярлыка/дока
        self.title(APP_TITLE)
        self.geometry("1240x780")
        self.minsize(1040, 640)
        self.configure(bg=BG)
        self._set_window_icon()

        # ---- состояние приложения
        self.app_state = self.STATE_OFF
        self.connected_proxy: str | None = None
        self.connected_ms: int | None = None
        self.connected_since: float | None = None
        self.source_of: dict[str, str] = {}      # прокси -> источник
        self.testing = False
        self.test_total = 0
        self.test_done = 0
        self.alive_cnt = 0
        self.results_q: queue.Queue = queue.Queue()
        self.test_then: callable | None = None   # колбэк по завершении проверки
        self._watchdog_at = 0.0
        self._lock = threading.Lock()

        self._setup_fonts()
        self._setup_style()
        self._build_layout()
        self._loop_drain()
        self._loop_watchdog()
        self._loop_uptime()

        self.after(200, self.load_sources)       # сразу подтягиваем списки

    def _set_window_icon(self):
        icon_path = os.path.expanduser("~/.local/share/icons/proxypilot.png")
        try:
            img = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, img)
            self._icon_img = img   # держим ссылку, иначе tk её соберёт
        except Exception:
            pass                   # без иконки всё равно работает

    # ------------------------------------------------------------- стили

    def _setup_fonts(self):
        self.f_root  = tkfont.Font(family="DejaVu Sans", size=10)
        self.f_big   = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")
        self.f_h1    = tkfont.Font(family="DejaVu Sans", size=17, weight="bold")
        self.f_mono  = tkfont.Font(family="DejaVu Sans Mono", size=10)
        self.f_btn   = tkfont.Font(family="DejaVu Sans", size=10, weight="bold")
        self.f_small = tkfont.Font(family="DejaVu Sans", size=9)
        self.option_add("*Font", self.f_root)

    def _setup_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD,
                     bordercolor=LINE, lightcolor=CARD, darkcolor=CARD)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Mut.TLabel", background=CARD, foreground=MUT)
        st.configure("Title.TLabel", background=CARD, foreground=TEXT, font=self.f_h1)
        st.configure("TButton", background=CARD2, foreground=TEXT, borderwidth=0,
                     focuscolor=CARD2, font=self.f_btn, padding=(10, 6))
        st.map("TButton",
               background=[("active", ACCENT), ("disabled", CARD)],
               foreground=[("disabled", MUT)])
        st.configure("Acc.TButton", background=ACCENT, foreground="#ffffff")
        st.map("Acc.TButton", background=[("active", ACCENT2)])
        st.configure("Green.TButton", background=GREEN, foreground="#08130c")
        st.map("Green.TButton", background=[("active", "#34d377")])
        st.configure("Red.TButton", background="#3a1d22", foreground=RED)
        st.map("Red.TButton", background=[("active", "#55262f")])
        st.configure("Treeview", background=CARD, foreground=TEXT, fieldbackground=CARD,
                     rowheight=26, borderwidth=0, font=self.f_root)
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=CARD2, foreground=MUT,
                     borderwidth=0, font=self.f_btn, padding=(6, 6))
        st.map("Treeview.Heading", background=[("active", LINE)])
        st.configure("Vertical.TScrollbar", background=CARD2, troughcolor=CARD,
                     borderwidth=0, arrowsize=12)
        st.configure("TCheckbutton", background=CARD, foreground=TEXT, focuscolor=CARD)
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure("TProgressbar", background=ACCENT, troughcolor=CARD2,
                     borderwidth=0, thickness=4)

    # ------------------------------------------------------------- каркас

    def _build_layout(self):
        self.columnconfigure(0, weight=0, minsize=312)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_left()
        self._build_right()

    # --------------------------------------------------- левая панель

    def _build_left(self):
        left = ttk.Frame(self, style="Card.TFrame", padding=(16, 16))
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)

        ttk.Label(left, text="ProxyPilot", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="бесплатные прокси · всегда живой", style="Mut.TLabel",
                  font=self.f_small).pack(anchor="w", pady=(0, 10))

        # --- большая кнопка-круг (как в VPN-клиентах)
        self.circle = tk.Canvas(left, width=190, height=190, bg=CARD,
                                highlightthickness=0)
        self.circle.pack(pady=(2, 6))
        self.circle.tag_bind("btn", "<Button-1>", lambda e: self.on_connect_clicked())
        self.circle.tag_bind("btn", "<Enter>",
                             lambda e: self.circle.config(cursor="hand2"))
        self.circle.tag_bind("btn", "<Leave>",
                             lambda e: self.circle.config(cursor=""))

        # --- статус подключения
        self.lbl_state = ttk.Label(left, text="ОТКЛЮЧЕНО", style="Card.TLabel",
                                   font=self.f_h1)
        self.lbl_state.pack()
        self.lbl_cur = ttk.Label(left, text="прокси не выбран", style="Mut.TLabel",
                                 font=self.f_mono)
        self.lbl_cur.pack(pady=(2, 0))
        self.lbl_meta = ttk.Label(left, text="", style="Mut.TLabel", font=self.f_small)
        self.lbl_meta.pack(pady=(0, 10))

        # --- кнопки под кругом
        row = ttk.Frame(left, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 10))
        row.columnconfigure((0, 1), weight=1)
        self.btn_pick = ttk.Button(row, text="Автоподбор лучшего", style="Acc.TButton",
                                   command=self.auto_pick)
        self.btn_pick.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_off = ttk.Button(row, text="Отключить", style="Red.TButton",
                                  command=self.disconnect, state="disabled")
        self.btn_off.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # --- сторож
        watchdog = ttk.Frame(left, style="Card.TFrame")
        ttk.Label(watchdog, text="СТОРОЖ", style="Mut.TLabel",
                  font=self.f_small).pack(anchor="w")
        self.var_watchdog = tk.BooleanVar(value=True)
        ttk.Checkbutton(watchdog, text="автоматически менять прокси, если текущий умер",
                        variable=self.var_watchdog).pack(anchor="w")
        self.var_watch_log = tk.StringVar(value="")
        ttk.Label(watchdog, textvariable=self.var_watch_log, style="Mut.TLabel",
                  font=self.f_small, wraplength=270, justify="left").pack(anchor="w")
        watchdog.pack(fill="x", pady=(0, 10))

        # --- источники
        ttk.Label(left, text="ИСТОЧНИКИ СПИСКОВ", style="Mut.TLabel",
                  font=self.f_small).pack(anchor="w")
        src_box = ttk.Frame(left, style="Card.TFrame")
        src_box.pack(fill="both", expand=True)
        self.var_sources: dict[str, tk.BooleanVar] = {}
        for name, _fn in SOURCES:
            v = tk.BooleanVar(value=True)
            self.var_sources[name] = v
            ttk.Checkbutton(src_box, text=name, variable=v).pack(anchor="w")
        self.btn_load = ttk.Button(left, text="Обновить списки", style="TButton",
                                   command=self.load_sources)
        self.btn_load.pack(fill="x", pady=(8, 0))

        self.draw_circle()

    # --------------------------------------------------- правая панель

    def _build_right(self):
        right = ttk.Frame(self, style="Card.TFrame", padding=(12, 16))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        # --- тулбар
        bar = ttk.Frame(right, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.btn_test_all = ttk.Button(bar, text="Проверить все", style="Acc.TButton",
                                       command=self.test_all)
        self.btn_test_all.pack(side="left")
        ttk.Button(bar, text="Проверить выбранные", style="TButton",
                   command=self.test_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Полная проверка выбранных", style="TButton",
                   command=self.full_check_selected).pack(side="left", padx=(6, 0))
        self.var_alive_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="только живые", variable=self.var_alive_only,
                        command=self.apply_filter).pack(side="left", padx=(12, 0))
        self.spin_to = tk.StringVar(value="4")
        ttk.Label(bar, text="таймаут, с:", style="Card.TLabel").pack(
            side="left", padx=(12, 0))
        ttk.Entry(bar, textvariable=self.spin_to, width=3).pack(side="left")

        # --- таблица
        cols = ("proxy", "ms", "status", "source")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("proxy", text="Прокси",
                          command=lambda: self.sort_by("proxy", False))
        self.tree.heading("ms", text="Задержка, мс",
                          command=lambda: self.sort_by("ms", True))
        self.tree.heading("status", text="Статус",
                          command=lambda: self.sort_by("status", False))
        self.tree.heading("source", text="Источник",
                          command=lambda: self.sort_by("source", False))
        self.tree.column("proxy", width=230, anchor="w")
        self.tree.column("ms", width=110, anchor="center", stretch=False)
        self.tree.column("status", width=140, anchor="center", stretch=False)
        self.tree.column("source", width=260, anchor="w")
        self.tree.grid(row=1, column=0, sticky="nsew")

        sb = ttk.Scrollbar(right, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=1, sticky="ns")

        self.tree.tag_configure("alive", foreground=GREEN)
        self.tree.tag_configure("dead", foreground="#5b6272")
        self.tree.tag_configure("current", background="#12233a")

        self.tree.bind("<Double-1>", lambda e: self.apply_selected())
        self.tree.bind("<Button-3>", self._popup_menu)

        # --- статус-бар
        self.var_status = tk.StringVar(value="готово")
        self.var_counts = tk.StringVar(value="прокси: 0 · живых: 0")
        sbar = ttk.Frame(right, style="Card.TFrame")
        sbar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(sbar, textvariable=self.var_counts, style="Mut.TLabel",
                  font=self.f_small).pack(side="left")
        ttk.Label(sbar, textvariable=self.var_status, style="Mut.TLabel",
                  font=self.f_small).pack(side="right")

        self.progress = ttk.Progressbar(right, mode="determinate")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            m = tk.Menu(self, tearoff=0, bg=CARD2, fg=TEXT,
                        activebackground=ACCENT, activeforeground="#fff")
            m.add_command(label="Сделать системным (GNOME)", command=self.apply_selected)
            m.add_command(label="Полная проверка", command=self.full_check_selected)
            m.add_command(label="Копировать адрес", command=self.copy_selected)
            m.add_separator()
            m.add_command(label="Убрать из списка", command=self.remove_selected)
            m.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------ круг-кнопка

    def draw_circle(self, pulse: bool = False):
        c = self.circle
        c.delete("all")
        cx = cy = 95
        r_in, r_out = 66, 78
        color, label = {
            self.STATE_OFF:    ("#1a2333", "ПОДКЛЮЧИТЬ"),
            self.STATE_SEARCH: ("#2a2410" if pulse else "#31301a", "ПОИСК…"),
            self.STATE_ON:     (GREEN,    "ОТКЛЮЧИТЬ"),
        }[self.app_state]
        text = TEXT if self.app_state != self.STATE_OFF else MUT
        c.create_oval(cx - r_out, cy - r_out, cx + r_out, cy + r_out,
                      outline=color if self.app_state != self.STATE_ON else GREEN,
                      width=10, tags="btn")
        c.create_oval(cx - r_in, cy - r_in, cx + r_in, cy + r_in, fill=color,
                      outline="", tags="btn")
        c.create_text(cx, cy - 8, text=label, fill="#ffffff", font=self.f_btn,
                      tags="btn")
        if self.app_state == self.STATE_ON:
            c.create_text(cx, cy + 14, text="✔ подключено", fill="#d7ffe8",
                          font=self.f_small, tags="btn")

    # --------------------------------------------------------- статусы

    def set_state(self, state: str, proxy: str | None = None, ms: int | None = None):
        self.app_state = state
        if state == self.STATE_ON:
            self.connected_proxy, self.connected_ms = proxy, ms
            if self.connected_since is None:
                self.connected_since = time.monotonic()
            self.lbl_state.config(text="ПОДКЛЮЧЕНО", foreground=GREEN)
            self.btn_off.config(state="normal")
        elif state == self.STATE_OFF:
            self.connected_proxy = self.connected_ms = None
            self.connected_since = None
            self.lbl_state.config(text="ОТКЛЮЧЕНО", foreground=TEXT)
            self.btn_off.config(state="disabled")
        elif state == self.STATE_SEARCH:
            self.lbl_state.config(text="ПОДБОР…", foreground=AMBER)
        if proxy:
            self.lbl_cur.config(text=proxy)
            meta = []
            if ms is not None:
                meta.append(f"задержка ~{ms} мс")
            self.lbl_meta.config(text=" · ".join(meta))
        else:
            self.lbl_cur.config(text="прокси не выбран")
            self.lbl_meta.config(text="")
        self._mark_current_row()
        self.draw_circle()

    def _mark_current_row(self):
        for iid in self.tree.get_children(""):
            tags = list(self.tree.item(iid, "tags"))
            tags = [t for t in tags if t != "current"]
            if iid == self.connected_proxy:
                tags.append("current")
            self.tree.item(iid, tags=tags)

    def set_status(self, text: str):
        self.var_status.set(text)

    def update_counts(self):
        # total берём из source_of: при фильтре «только живые» detached-строки
        # не видны в get_children("")
        self.var_counts.set(
            f"прокси: {len(self.source_of)} · живых: {self.alive_cnt}"
            + (f" · проверка: {self.test_done}/{self.test_total}"
               if self.testing else ""))

    # ------------------------------------------------------ загрузка

    def load_sources(self):
        enabled = [name for name, v in self.var_sources.items() if v.get()]
        if not enabled:
            self.set_status("не выбран ни один источник")
            return
        self.btn_load.config(state="disabled")
        self.set_status("загружаю списки прокси…")

        def work():
            merged = fetch_sources(enabled)
            self.after(0, lambda: self._sources_done(merged))

        threading.Thread(target=work, daemon=True).start()

    def _sources_done(self, merged: dict[str, str]):
        self.btn_load.config(state="normal")
        self.source_of = merged
        self.tree.delete(*self.tree.get_children(""))
        self.alive_cnt = 0
        for p, src in merged.items():
            self.tree.insert("", "end", iid=p, values=(p, "", "—", src))
        self.update_counts()
        self.set_status(f"загружено {len(merged)} прокси из "
                        f"{sum(1 for v in self.var_sources.values() if v.get())} источников")
        # восстановить текущий из состояния CLI-скрипта
        st = core.load_state()
        if st.get("proxy") and self.app_state != self.STATE_ON:
            self.set_state(self.STATE_ON, st["proxy"])

    # ------------------------------------------------------ тест-движок

    def _timeout(self) -> float:
        try:
            return max(1.0, float(self.spin_to.get()))
        except ValueError:
            return 4.0

    def _full_timeout(self) -> float:
        """Полная проверка (реальная страница) через бесплатные прокси идёт 3–10 с —
        таймаут для неё заметно больше, чем для быстрой 204-пробы."""
        return max(8.0, self._timeout() * 2)

    def start_test(self, proxies: list[str], full: bool = False, then=None):
        if self.testing or not proxies:
            return
        self.testing = True
        self.test_total = len(proxies)
        self.test_done = 0
        self.test_then = then
        self.progress.config(maximum=len(proxies), value=0)
        for p in proxies:
            self.tree.set(p, "status", "…" if not full else "…")
        self.set_status(("полная" if full else "быстрая") +
                        f" проверка {len(proxies)} прокси…")
        self.update_counts()

        todo: queue.Queue[str] = queue.Queue()
        for p in proxies:
            todo.put(p)
        timeout = self._timeout()

        def worker():
            while True:
                try:
                    p = todo.get_nowait()
                except queue.Empty:
                    return
                t0 = time.monotonic()
                ok = core.proxy_alive(p, timeout)          # 204 от Google через прокси
                ms = int((time.monotonic() - t0) * 1000)
                if ok and full:
                    ok = core.proxy_confirm(p, self._full_timeout())  # реальная страница
                self.results_q.put((p, ms if ok else None, full))

        for _ in range(min(64, max(4, len(proxies)))):
            threading.Thread(target=worker, daemon=True).start()

    def test_all(self):
        # source_of — все когда-либо загруженные (включая скрытые фильтром)
        proxies = [p for p in self.source_of
                   if self.tree.set(p, "status") not in ("✔ жив", "✔ полн.")]
        if not proxies:
            proxies = list(self.source_of)
        self.start_test(list(proxies))

    def test_selected(self):
        sel = list(self.tree.selection())
        self.start_test(sel)

    def full_check_selected(self):
        self.start_test(list(self.tree.selection()), full=True)

    def _loop_drain(self):
        """Перенос результатов проверок в таблицу (в главном потоке)."""
        drained = 0
        try:
            while drained < 300:
                p, ms, full = self.results_q.get_nowait()
                drained += 1
                self.test_done += 1
                old = self.tree.set(p, "status")
                if ms is None:                       # умер (или умер при перепроверке)
                    if old in ("✔ жив", "✔ полн."):
                        self.alive_cnt = max(0, self.alive_cnt - 1)
                    self.tree.set(p, "ms", "")
                    self.tree.set(p, "status", "✘ мёртв")
                    self.tree.item(p, tags=["dead"])
                else:
                    if old not in ("✔ жив", "✔ полн."):
                        self.alive_cnt += 1
                    self.tree.set(p, "ms", str(ms))
                    self.tree.set(p, "status", "✔ полн." if full else "✔ жив")
                    self.tree.item(p, tags=["alive"])
        except queue.Empty:
            pass
        if drained:
            self.progress.config(value=self.test_done)
            self.update_counts()
            if self.var_alive_only.get():
                self.apply_filter()
        if self.testing and self.test_done >= self.test_total:
            self.testing = False
            self.set_status(f"проверка завершена: живых {self.alive_cnt}")
            cb = self.test_then
            self.test_then = None
            if cb:
                self.after(50, cb)
        self.after(120, self._loop_drain)

    # --------------------------------------------------- сортировка/фильтр

    def sort_by(self, col: str, numeric: bool):
        data = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]
        if numeric:
            data.sort(key=lambda t: int(t[0]) if t[0].isdigit() else 10 ** 9)
        else:
            data.sort(key=lambda t: t[0].lower())
        for i, (_v, iid) in enumerate(data):
            self.tree.move(iid, "", i)

    def apply_filter(self):
        only = self.var_alive_only.get()
        for iid in self.tree.get_children(""):
            alive = self.tree.set(iid, "status").startswith("✔")
            if only and not alive:
                self.tree.detach(iid)
            else:
                self.tree.reattach(iid, "", "end")

    # ------------------------------------------------ применение прокси

    def apply_proxy(self, proxy: str, ms: int | None = None, then=None):
        de = core.DESKTOP_NAMES.get(core.detect_desktop(), "окружение")
        self.set_status(f"применяю {proxy} ({de} + окружение)…")

        def work():
            ok_de = core.apply_desktop_proxy(proxy)
            core.save_state(proxy, {})      # env-файл + состояние (+ системный прокси уже стоит)
            self.after(0, lambda: self._apply_done(proxy, ms, ok_de, then))

        threading.Thread(target=work, daemon=True).start()

    def _apply_done(self, proxy: str, ms, ok_de, then):
        de = core.DESKTOP_NAMES.get(core.detect_desktop(), "окружение")
        self.set_state(self.STATE_ON, proxy, ms)
        self.set_status(f"системный прокси: {proxy}"
                        + ("" if ok_de else f" ({de} не настроен — только окружение)"))
        if then:
            then()

    def apply_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            self.set_status("ничего не выбрано")
            return
        p = sel[0]
        ms = self.tree.set(p, "ms")
        self.apply_proxy(p, int(ms) if ms.isdigit() else None)

    def copy_selected(self):
        sel = list(self.tree.selection())
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(sel))
            self.set_status("адреса скопированы")

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
        self.update_counts()

    def disconnect(self):
        self.set_status("отключаю…")

        def work():
            core.clear_desktop_proxy()
            core.save_state(None, {})
            self.after(0, lambda: self.set_state(self.STATE_OFF))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------- автоподбор

    def on_connect_clicked(self):
        if self.app_state == self.STATE_OFF:
            self.auto_pick()
        elif self.app_state == self.STATE_ON:
            self.disconnect()

    def auto_pick(self):
        """Лучший = минимальная задержка среди живых, подтверждённый полной проверкой."""
        if self.app_state == self.STATE_SEARCH:
            return
        self.set_state(self.STATE_SEARCH)
        self.set_status("подбираю лучший прокси…")
        alive = [(int(self.tree.set(p, "ms")), p)
                 for p in self.tree.get_children("")
                 if self.tree.set(p, "ms").isdigit()]
        if alive:
            self._auto_pick_among(alive)
        else:
            # ещё ничего не проверено — проверяем первый батч и подбираем после
            proxies = list(self.tree.get_children(""))[:400]
            if not proxies:
                self.set_state(self.STATE_OFF)
                self.set_status("список пуст — обновите источники")
                return
            self.start_test(proxies, then=lambda: self.auto_pick_after_test())

    def auto_pick_after_test(self):
        alive = [(int(self.tree.set(p, "ms")), p)
                 for p in self.source_of
                 if self.tree.set(p, "ms").isdigit()]
        if not alive:
            self.set_state(self.STATE_OFF)
            self.set_status("живых не нашлось — попробуйте ещё раз или смените источники")
            return
        self._auto_pick_among(alive)

    def _auto_pick_among(self, alive: list[tuple[int, str]]):
        """Перебираем живых батчами по 5 (лучшие по задержке первыми), полную
        проверку проходит первый попавшийся — его и ставим системным."""
        alive.sort()
        full_to = self._full_timeout()

        def try_batch(idx: int):
            batch = [p for _ms, p in alive[idx:idx + 5]]
            if not batch:
                self.set_state(self.STATE_OFF)
                self.set_status("ни один живой прокси не прошёл полную проверку — "
                                "обновите списки и проверьте ещё раз")
                return
            results: dict[str, bool] = {}
            threads = []
            for p in batch:
                def check(p=p):
                    results[p] = core.proxy_confirm(p, full_to)
                t = threading.Thread(target=check, daemon=True)
                threads.append(t)
                t.start()

            def poll():
                if any(t.is_alive() for t in threads):
                    self.after(150, poll)
                    return
                for p in batch:                 # порядок = по росту задержки
                    if results.get(p):
                        ms = int(self.tree.set(p, "ms") or 0) or None
                        self.tree.set(p, "status", "✔ полн.")
                        self.apply_proxy(p, ms)
                        return
                self.set_status(f"батч {idx // 5 + 1} не прошёл полную проверку — "
                                "пробую следующих…")
                self.after(100, lambda: try_batch(idx + 5))

            poll()

        try_batch(0)

    # --------------------------------------------------------- сторож

    def _loop_watchdog(self):
        interval = 30
        if (self.var_watchdog.get() and self.app_state == self.STATE_ON
                and not self.testing
                and time.monotonic() - self._watchdog_at > interval):
            self._watchdog_at = time.monotonic()
            p = self.connected_proxy

            def work():
                ok = core.proxy_confirm(p, self._full_timeout()) if p else False
                self.after(0, lambda: self._watchdog_done(p, ok))

            threading.Thread(target=work, daemon=True).start()
        self.after(2000, self._loop_watchdog)

    def _watchdog_done(self, p: str, ok: bool):
        if not self.var_watchdog.get() or self.connected_proxy != p:
            return
        if ok:
            self.var_watch_log.set(f"✓ {p} жив ({time.strftime('%H:%M:%S')})")
        else:
            self.var_watch_log.set(f"✗ {p} умер — меняю…")
            self.set_state(self.STATE_OFF)   # чтобы авто-подбор не упёрся в state
            self.after(100, self.auto_pick)

    # -------------------------------------------------------- uptime

    def _loop_uptime(self):
        if self.app_state == self.STATE_ON and self.connected_since is not None:
            up = int(time.monotonic() - self.connected_since)
            h, m, s = up // 3600, (up % 3600) // 60, up % 60
            meta = self.lbl_meta.cget("text")
            base = meta.split(" · ")[0]
            self.lbl_meta.config(text=f"{base} · uptime {h:02d}:{m:02d}:{s:02d}"
                                if base else f"uptime {h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._loop_uptime)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = ProxyPilot()
    app.mainloop()


if __name__ == "__main__":
    main()
