#!/usr/bin/env python3
"""
alive_proxy.py — держит переменные окружения прокси (http_proxy/https_proxy/...)
всегда указывающими на ЖИВОЙ прокси из списка pip-модуля free-proxy.

Как это устроено:
  * кандидаты берутся из FreeProxy().get_proxy_list() (обе страницы параллельно),
  * кандидаты проверяются ПАРАЛЛЕЛЬНО (пул потоков, default 32) в два этапа:
    быстрая проба — 204 от google.com/generate_204 ЧЕРЕЗ прокси (строго 204 и
    пустое тело), точная проба — загрузка реальной страницы через HTTPS-туннель;
    победитель — первый прошедший оба этапа, остальное отменяется;
  * живой прокси записывается в ~/.config/alive-proxy/proxy.env (export-строки)
    и в state.json (текущий прокси + «кладбище» умерших с кулдауном),
    и одновременно прописывается как системный прокси текущего DE
    (GNOME/Cinnamon/MATE/... через gsettings, KDE Plasma через kwriteconfig),
  * демон `daemon` периодически проверяет текущий прокси и, как только тот
    умер, подбирает замену и перезаписывает env-файл и GNOME.

Зависимости:
    pip install --user free-proxy requests

Быстрый старт:
    python3 alive_proxy.py daemon             # сторож в фоне (или systemd: install-systemd)
    python3 alive_proxy.py refresh            # проверить/сменить прокси, напечатать export-строки
    eval "$(python3 alive_proxy.py refresh)"  # применить прокси в текущем шелле

Подключить в шелл (~/.bashrc) — каждое новое окно + обновление перед каждым промптом:
    source ~/.config/alive-proxy/proxy.env
    PROMPT_COMMAND='[ -f ~/.config/alive-proxy/proxy.env ] && . ~/.config/alive-proxy/proxy.env; '"${PROMPT_COMMAND:+$'\n'$PROMPT_COMMAND}"

Запустить команду с прокси в окружении:
    python3 alive_proxy.py run -- curl -s ifconfig.me
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fp.fp import FreeProxy

# ------------------------------------------------------------------ настройка

STATE_DIR = Path(os.environ.get("ALIVE_PROXY_HOME", str(Path.home() / ".config" / "alive-proxy")))
ENV_FILE = STATE_DIR / "proxy.env"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "daemon.log"

PROBE_204_URLS = (                 # быстрая проба: строго 204 и пустое тело
    "https://www.google.com/generate_204",
    "https://www.gstatic.com/generate_204",  # запасная, если google недоступен через прокси
)
PROBE_CONTENT_URL = "https://example.com/"   # точная проба: реальная страница с телом
PROBE_CONTENT_MARKER = "example domain"      # маркер в теле (нижний регистр)
DEAD_COOLDOWN = 600          # сек: мёртвый прокси не пробуем снова раньше этого срока
LOG_ROTATE_BYTES = 256 * 1024

log = logging.getLogger("alive_proxy")

# ------------------------------------------------------------------ проверки


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # игнорируем чужие прокси из окружения — проверяем именно целевой
    return s


def proxy_alive(proxy_url: str, timeout: float = 8.0) -> bool:
    """Быстрая проба: 204 от Google ЧЕРЕЗ прокси, строго без тела.
    Отсекает мёртвых и перехватчиков (перехватчики обычно отвечают 200 с телом)."""
    s = _session()
    try:
        for url in PROBE_204_URLS:
            try:
                r = s.get(url, proxies={"http": proxy_url, "https": proxy_url},
                          timeout=timeout, allow_redirects=False)
                if r.status_code == 204 and not r.content:
                    return True
            except requests.RequestException:
                continue
        return False
    finally:
        s.close()


def proxy_confirm(proxy_url: str, timeout: float = 8.0) -> bool:
    """Точная проба: реальная страница с телом через HTTPS-туннель прокси.
    Прошедшие её гарантированно годятся для обычного трафика (CONNECT, TLS, тело)."""
    s = _session()
    try:
        try:
            r = s.get(PROBE_CONTENT_URL, proxies={"http": proxy_url, "https": proxy_url},
                      timeout=timeout)
            return r.status_code == 200 and PROBE_CONTENT_MARKER in r.text.lower()
        except requests.RequestException:
            return False
    finally:
        s.close()

# ------------------------------------------------------- список из free-proxy


def fetch_candidates() -> list[str]:
    """Список прокси из free-proxy; обе страницы-источника качаются параллельно."""
    # get_proxy_list(repeat): repeat=0 -> sslproxies.org, repeat=1 -> free-proxy-list.net
    def grab(repeat: int) -> list[str]:
        try:
            return [str(x) for x in FreeProxy().get_proxy_list(repeat)]
        except Exception as e:
            log.warning("get_proxy_list(%s) не сработал: %s", repeat, e)
            return []
    with ThreadPoolExecutor(max_workers=2) as ex:
        pages = list(ex.map(grab, (0, 1)))
    raw = [u for page in pages for u in page]
    out, seen = [], set()
    for u in raw:
        u = str(u).strip()
        if not u:
            continue
        # free-proxy отдаёт 'ip:port' или 'ip:port:HTTP' — приводим к http://ip:port
        parts = u.replace("https://", "").replace("http://", "").split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        u = f"http://{parts[0]}:{parts[1]}"
        if u not in seen:
            seen.add(u)
            out.append(u)
    random.shuffle(out)
    return out


def fallback_candidates(tries: int = 2) -> list[str]:
    """Если список не спарсился — просим у free-proxy проверенные прокси (параллельно)."""
    def grab(_i: int) -> str:
        try:
            return str(FreeProxy(timeout=2, rand=True).get() or "")
        except Exception:
            return ""
    with ThreadPoolExecutor(max_workers=tries) as ex:
        return [g for g in ex.map(grab, range(tries)) if g]

# ------------------------------------------------------------------ состояние


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def export_block(proxy_url: str) -> str:
    lines = [f'export {var}="{proxy_url}"'
             for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                         "all_proxy", "ALL_PROXY")]
    lines.append('export no_proxy="localhost,127.0.0.1,::1"')
    lines.append('export NO_PROXY="localhost,127.0.0.1,::1"')
    return "\n".join(lines) + "\n"


def save_state(proxy_url: str | None, dead: dict[str, float]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    dead = {u: t for u, t in dead.items() if now - t < DEAD_COOLDOWN}  # чистим кулдаун
    STATE_FILE.write_text(json.dumps(
        {"proxy": proxy_url, "set_at": now if proxy_url else None, "dead": dead},
        indent=2, ensure_ascii=False))
    if proxy_url:
        ENV_FILE.write_text(export_block(proxy_url))
        apply_desktop_proxy(proxy_url)  # системный прокси текущего DE — тоже всегда свежий
    else:
        ENV_FILE.unlink(missing_ok=True)

# ------------------------------------------ графическое окружение (DE)

# Канонические имена DE для логов; «» (пустая строка) = определить не удалось.
DESKTOP_NAMES = {
    "kde": "KDE",
    "gnome": "GNOME",
    "cinnamon": "Cinnamon",
    "mate": "MATE",
    "xfce": "XFCE",
    "lxqt": "LXQt",
    "lxde": "LXDE",
    "unity": "Unity",
    "budgie": "Budgie",
    "pantheon": "Pantheon",
}

# DE, управляемые через схему org.gnome.system.proxy (gsettings).
GSETTINGS_DESKTOPS = {"gnome", "cinnamon", "mate", "unity", "budgie", "pantheon"}


def _kwriteconfig_path() -> str | None:
    """Путь к kwriteconfig (Plasma 6 → Plasma 5), либо None."""
    for tool in ("kwriteconfig6", "kwriteconfig5"):
        p = shutil.which(tool)
        if p:
            return p
    return None


def detect_desktop() -> str:
    """Определить текущее графическое окружение: сначала по сессионным
    переменным, затем (запасной путь) по наличию настроечных утилит.
    Пустая строка — определить не удалось."""
    raw = " ".join(os.environ.get(k, "") for k in (
        "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")).lower()
    for name, tags in (
        ("kde", ("plasma", "kde")),
        ("cinnamon", ("cinnamon",)),
        ("mate", ("mate",)),
        ("xfce", ("xfce",)),
        ("lxqt", ("lxqt",)),
        ("lxde", ("lxde",)),
        ("pantheon", ("pantheon",)),
        ("budgie", ("budgie",)),
        ("unity", ("unity",)),
        ("gnome", ("gnome",)),
    ):
        if any(tag in raw for tag in tags):
            return name
    if _kwriteconfig_path() is not None:
        return "kde"
    if shutil.which("gsettings") is not None:
        return "gnome"
    return ""


# --------------------------------------------------- прокси в GNOME (gsettings)

_gnome_applied: str | None = None  # что уже вписали в gsettings (чтобы не дёргать зря)


def _host_port(proxy_url: str) -> tuple[str, int] | None:
    u = urlsplit(proxy_url)
    if not u.hostname or u.port is None:
        return None
    return u.hostname, u.port


def gnome_apply(proxy_url: str) -> bool:
    """Прописать прокси как системный в GNOME/Cinnamon/MATE (gsettings): его видят
    Firefox/Chromium в режиме «системный прокси», приложения и т.д."""
    global _gnome_applied
    if proxy_url == _gnome_applied:
        return True
    if shutil.which("gsettings") is None:
        log.warning("gsettings не найден — системный прокси не настраиваю")
        return False
    hp = _host_port(proxy_url)
    if hp is None:
        log.warning("не разобрать адрес прокси %s — gsettings пропускаю", proxy_url)
        return False
    host, port = hp
    settings = (
        ("org.gnome.system.proxy", "mode", "manual"),
        ("org.gnome.system.proxy.http", "host", host),
        ("org.gnome.system.proxy.http", "port", str(port)),
        ("org.gnome.system.proxy.https", "host", host),
        ("org.gnome.system.proxy.https", "port", str(port)),
        ("org.gnome.system.proxy", "ignore-hosts", "['localhost', '127.0.0.1', '::1']"),
    )
    ok = True
    for schema, key, value in settings:
        r = subprocess.run(["gsettings", "set", schema, key, value],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("gsettings set %s %s: %s", schema, key,
                        (r.stderr or "").strip() or "ошибка")
            ok = False
    if ok:
        _gnome_applied = proxy_url
        log.info("gsettings: системный прокси = %s", proxy_url)
    return ok


def gnome_off() -> bool:
    """Сбросить системный прокси gsettings на значения по умолчанию (mode=none)."""
    global _gnome_applied
    if shutil.which("gsettings") is None:
        log.warning("gsettings не найден — сбрасывать нечего")
        return False
    ok = True
    for schema, key in (
        ("org.gnome.system.proxy", "mode"),
        ("org.gnome.system.proxy.http", "host"),
        ("org.gnome.system.proxy.http", "port"),
        ("org.gnome.system.proxy.https", "host"),
        ("org.gnome.system.proxy.https", "port"),
        ("org.gnome.system.proxy", "ignore-hosts"),
    ):
        r = subprocess.run(["gsettings", "reset", schema, key],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("gsettings reset %s %s: %s", schema, key,
                        (r.stderr or "").strip() or "ошибка")
            ok = False
    if ok:
        _gnome_applied = None
        log.info("gsettings: системный прокси сброшен на значения по умолчанию")
    return ok

# ---------------------------------------------------- прокси в KDE (kioslaverc)

_kde_applied: str | None = None  # что уже вписали в kioslaverc


def kde_apply(proxy_url: str) -> bool:
    """Прописать прокси как системный в KDE Plasma (kwriteconfig → kioslaverc).
    KDE хранит адрес в виде 'http://хост порт' — пробел вместо двоеточия."""
    global _kde_applied
    if proxy_url == _kde_applied:
        return True
    kw = _kwriteconfig_path()
    if kw is None:
        log.warning("kwriteconfig не найден — системный прокси KDE не настраиваю")
        return False
    hp = _host_port(proxy_url)
    if hp is None:
        log.warning("не разобрать адрес прокси %s — KDE пропускаю", proxy_url)
        return False
    host, port = hp
    addr = f"http://{host} {port}"
    ok = True
    for key, value in (
        ("ProxyType", "1"),          # 1 = настройка вручную
        ("httpProxy", addr),
        ("httpsProxy", addr),
    ):
        r = subprocess.run(
            [kw, "--file", "kioslaverc", "--group", "Proxy Settings",
             "--key", key, value],
            capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("kwriteconfig %s: %s", key, (r.stderr or "").strip() or "ошибка")
            ok = False
    if ok:
        _kde_applied = proxy_url
        log.info("KDE: системный прокси = %s", proxy_url)
    return ok


def kde_off() -> bool:
    """Отключить системный прокси KDE (ProxyType=0), не трогая сохранённые адреса."""
    global _kde_applied
    kw = _kwriteconfig_path()
    if kw is None:
        log.warning("kwriteconfig не найден — сбрасывать нечего")
        return False
    r = subprocess.run(
        [kw, "--file", "kioslaverc", "--group", "Proxy Settings",
         "--key", "ProxyType", "0"],
        capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("kwriteconfig ProxyType: %s", (r.stderr or "").strip() or "ошибка")
        return False
    _kde_applied = None
    log.info("KDE: системный прокси отключён")
    return True


# ------------------------------------- системный прокси в текущем окружении


def apply_desktop_proxy(proxy_url: str) -> bool:
    """Применить прокси к системным настройкам текущего DE."""
    de = detect_desktop()
    log.debug("графическое окружение: %s", DESKTOP_NAMES.get(de, de) or "не определено")
    if de in GSETTINGS_DESKTOPS:
        return gnome_apply(proxy_url)
    if de == "kde":
        return kde_apply(proxy_url)
    log.info("%s: системного прокси в DE нет — только переменные окружения",
             DESKTOP_NAMES.get(de, de) if de else "неизвестное окружение")
    return False


def clear_desktop_proxy() -> bool:
    """Сбросить системный прокси текущего DE."""
    de = detect_desktop()
    if de in GSETTINGS_DESKTOPS:
        return gnome_off()
    if de == "kde":
        return kde_off()
    return False

# -------------------------------------------------------------- подбор живого


def find_alive_proxy(timeout: float, dead: dict[str, float], workers: int = 32) -> str | None:
    """Параллельная проверка кандидатов демон-потоками: побеждает первый, кто
    подтвердился дважды; после победителя остальных не ждём — хвост проверок
    остаётся в фоне и не тормозит выход процесса."""
    now = time.time()
    cands = [c for c in fetch_candidates() if now - dead.get(c, 0) >= DEAD_COOLDOWN]
    if not cands:
        cands = [c for c in fallback_candidates() if now - dead.get(c, 0) >= DEAD_COOLDOWN]
    if not cands:
        return None
    workers = min(workers, len(cands))
    log.info("кандидатов: %d — проверяю параллельно (%d потоков)", len(cands), workers)

    todo: queue.Queue[str] = queue.Queue()
    for c in cands:
        todo.put(c)
    stop = threading.Event()
    lock = threading.Lock()
    winner: list[str] = []

    def worker() -> None:
        while not stop.is_set():
            try:
                c = todo.get_nowait()
            except queue.Empty:
                return
            # двухступенчатая проверка: быстрая 204-проба Google через прокси,
            # затем загрузка реальной страницы — перехватчики и «одноразовые» отпадают
            if proxy_alive(c, timeout) and not stop.is_set() and proxy_confirm(c, timeout):
                if not stop.is_set():
                    winner.append(c)
                    stop.set()
                return
            with lock:
                dead[c] = time.time()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    started = time.monotonic()
    for t in threads:
        t.start()
    # ждём победителя (или исчерпания кандидатов), но не хвоста чужих таймаутов
    while not stop.is_set() and any(t.is_alive() for t in threads):
        time.sleep(0.05)
    if winner:
        log.info("живой прокси найден и подтверждён за %.1f c: %s",
                 time.monotonic() - started, winner[0])
        return winner[0]
    return None  # кандидаты кончились, все потоки уже вышли


def ensure_alive(timeout: float, force: bool = False,
                 workers: int = 32) -> tuple[str | None, bool]:
    """Возвращает (прокси, менялся_ли). Гарантирует, что в состоянии живой прокси.
    При force=True текущий прокси исключается из кандидатов — ищем именно ДРУГОГО
    живого; если другого нет, остаётся текущий."""
    st = load_state()
    cur = st.get("proxy")
    dead = dict(st.get("dead", {}))
    if cur and not force and proxy_confirm(cur, timeout):
        return cur, False
    log.info("текущий прокси отсутствует или мёртв — ищем замену")
    if cur and force:
        dead[cur] = time.time()  # кулдаун текущему: смена должна дать ДРУГОГО прокси
    new = find_alive_proxy(timeout, dead, workers)
    if new:
        if cur:
            dead[cur] = time.time()
        save_state(new, dead)
        return new, True
    # живого не нашли: старый остаётся в силе (лучше мёртвый, чем никакого),
    # но кладбище неудач сохраняем, чтобы не долбить их каждый цикл
    if cur:
        log.warning("замены не нашли, оставляем прежний %s", cur)
        save_state(cur, dead)
    return cur, False

# ------------------------------------------------------------------- команды


def cmd_refresh(args) -> int:
    proxy, changed = ensure_alive(args.timeout, force=args.force, workers=args.workers)
    if proxy:
        if changed:
            log.info("установлен новый прокси: %s", proxy)
        print(export_block(proxy), end="")  # stdout — для eval "$( ... )"
        return 0
    print("живой прокси не найден — попробуйте позже", file=sys.stderr)
    return 2


def cmd_export(args) -> int:
    p = load_state().get("proxy")
    if not p:
        print("прокси ещё не установлен; сначала: alive_proxy.py refresh", file=sys.stderr)
        return 1
    print(export_block(p), end="")
    return 0


def cmd_status(args) -> int:
    st = load_state()
    p = st.get("proxy")
    if not p:
        print("прокси не установлен")
        return 1
    age = int((time.time() - (st.get("set_at") or 0)) // 60)
    print(f"текущий прокси : {p}")
    print(f"возраст        : {age} мин")
    if args.no_probe:
        print("живой          : не проверял (-n)")
    else:
        # строгая проверка: реальная страница через прокси
        print(f"живой          : {'да' if proxy_confirm(p, args.timeout) else 'НЕТ'}")
    return 0


def cmd_daemon(args) -> int:
    log.info("сторож запущен: интервал %s c, таймаут %s c, потоков %d",
             args.interval, args.timeout, args.workers)
    backoff = args.interval
    while True:
        try:
            proxy, changed = ensure_alive(args.timeout, workers=args.workers)
            if proxy is None:
                log.warning("живых прокси нет вообще; повтор через %s c", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, args.max_backoff)
                continue
            if changed:
                log.info("прокси заменён на %s", proxy)
            else:
                log.debug("текущий прокси жив: %s", proxy)
            backoff = args.interval
        except KeyboardInterrupt:
            log.info("остановлен пользователем")
            return 0
        except Exception:
            log.exception("неожиданная ошибка в цикле сторожа")
        time.sleep(args.interval)


def cmd_run(args) -> int:
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("укажите команду после run --", file=sys.stderr)
        return 2
    proxy, _ = ensure_alive(args.timeout, workers=args.workers)
    if not proxy:
        print("живой прокси не найден", file=sys.stderr)
        return 2
    log.info("запуск %s через %s", shlex.join(cmd), proxy)
    env = dict(os.environ)
    env.update({"http_proxy": proxy, "https_proxy": proxy,
                "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy})
    os.execvpe(cmd[0], cmd, env)  # noqa: E999 - не возвращаемся


def cmd_install_systemd(args) -> int:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "alive-proxy.service"
    unit.write_text(
        "[Unit]\n"
        "Description=Alive free-proxy keeper (держит http_proxy живым)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
        f" daemon --interval {args.interval}\n"
        "Restart=always\n"
        "RestartSec=15\n\n"
        "[Install]\n"
        "WantedBy=default.target\n")
    print(f"юнит записан: {unit}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "alive-proxy.service"], check=False)
    print("демон включён (systemctl --user status alive-proxy)")
    return 0


def cmd_gnome(args) -> int:
    p = load_state().get("proxy")
    if not p:
        print("прокси ещё не установлен; сначала: alive_proxy.py refresh", file=sys.stderr)
        return 1
    if apply_desktop_proxy(p):
        de = detect_desktop()
        print(f"{DESKTOP_NAMES.get(de, de) or 'система'}: системный прокси = {p}")
        return 0
    print("не удалось применить прокси в графическом окружении (см. лог выше)",
          file=sys.stderr)
    return 1


def cmd_gnome_off(args) -> int:
    if clear_desktop_proxy():
        print("системный прокси сброшен")
        return 0
    print("не удалось сбросить системный прокси (см. лог выше)", file=sys.stderr)
    return 1


def cmd_shell_hook(args) -> int:
    env = str(ENV_FILE)
    print("# --- добавить в ~/.bashrc (для zsh — source в ~/.zshrc) ---")
    print(f'[ -f "{env}" ] && source "{env}"')
    print("PROMPT_COMMAND='[ -f \"" + env + "\" ] && . \"" + env
          + "\"; '\"${PROMPT_COMMAND:+$'\\n'$PROMPT_COMMAND}\"")
    print("# ----------------------------------------------------------")
    return 0

# ---------------------------------------------------------------------- main


def setup_logging(verbose: bool) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_ROTATE_BYTES:
            LOG_FILE.unlink()
        handlers.append(logging.FileHandler(LOG_FILE))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="alive_proxy.py",
        description="Держит переменные окружения прокси всегда живыми (free-proxy).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='примеры:\n'
               '  alive_proxy.py refresh            # проверить/сменить и напечатать export-строки\n'
               '  eval "$(python3 alive_proxy.py refresh)"\n'
               '  alive_proxy.py daemon             # сторож в фоне\n'
               '  alive_proxy.py run -- curl -s ifconfig.me\n')
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-t", "--timeout", type=float, default=6.0,
                        help="таймаут проверки одного прокси, сек (по умолчанию 6)")
    common.add_argument("-w", "--workers", type=int, default=32,
                        help="потоков параллельной проверки прокси (по умолчанию 32)")

    p = sub.add_parser("refresh", parents=[common],
                       help="проверить и при необходимости сменить прокси; напечатать export-строки")
    p.add_argument("-f", "--force", action="store_true", help="сменить даже живой")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("export", help="напечатать export-строки текущего прокси")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", parents=[common], help="показать текущий прокси")
    p.add_argument("-n", "--no-probe", action="store_true",
                   help="не проверять живость — мгновенный вывод")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("daemon", parents=[common],
                       help="бесконечный сторож: держит прокси живым")
    p.add_argument("--interval", type=float, default=60.0,
                   help="период проверки текущего прокси, сек (по умолчанию 60)")
    p.add_argument("--max-backoff", type=float, default=600.0,
                   help="макс. пауза, если живых не нашлось (по умолчанию 600)")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("run", parents=[common],
                       help="выполнить команду с живым прокси в окружении")
    p.add_argument("command", nargs=argparse.REMAINDER, metavar="CMD")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("install-systemd",
                       help="поставить и включить user-юнит systemd для демона")
    p.add_argument("--interval", type=float, default=60.0)
    p.set_defaults(func=cmd_install_systemd)

    p = sub.add_parser("gnome", help="прописать текущий прокси как системный в текущем DE (KDE/GNOME/Cinnamon/MATE/...)")
    p.set_defaults(func=cmd_gnome)

    p = sub.add_parser("gnome-off", help="сбросить системный прокси текущего DE на умолчания")
    p.set_defaults(func=cmd_gnome_off)

    p = sub.add_parser("shell-hook", help="напечатать строки подключения для ~/.bashrc")
    p.set_defaults(func=cmd_shell_hook)

    args = ap.parse_args()
    setup_logging(args.verbose)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
