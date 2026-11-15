#!/usr/bin/env bash
# start.sh — управление живым бесплатным прокси (bash-env + GNOME + демон-сторож).
#
#   ./start.sh              интерактивное меню (без терминала — просто запуск)
#   ./start.sh start        настроить всё и запустить демона; КАЖДЫЙ запуск
#                           меняет прокси на другого живого (если такой есть)
#   ./start.sh rotate       только сменить прокси на другого живого
#   ./start.sh status       текущий прокси и жив ли он
#   ./start.sh stop         остановить демона
#   ./start.sh uninstall    убрать ВСЁ: демон, хук ~/.bashrc, GNOME-прокси,
#                           состояние в ~/.config/alive-proxy
#   ./start.sh menu         принудительно показать меню
#   source ./start.sh       запустить + сразу применить прокси в текущем шелле

STATE_DIR="$HOME/.config/alive-proxy"
ENV_FILE="$STATE_DIR/proxy.env"
PID_FILE="$STATE_DIR/daemon.pid"
BASHRC="$HOME/.bashrc"
HOOK_MARK=">>> alive-proxy >>>"

cd "$(dirname "$0")"
PY=".venv/bin/python"
DID_UNINSTALL=0

msg()  { printf '%s\n' "$*"; }
die()  { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- стройблоки

ensure_venv() {
    if [ ! -x "$PY" ]; then
        msg "== создаю venv и ставлю зависимости (free-proxy, requests)"
        python3 -m venv .venv || die "не удалось создать .venv"
        "$PY" -m pip install -q free-proxy requests || die "не удалось поставить зависимости"
    fi
    "$PY" -c "import requests, fp" 2>/dev/null \
        || { msg "== зависимости битые, переустанавливаю"; "$PY" -m pip install -q --force-reinstall free-proxy requests; }
}

daemon_running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

daemon_stop() {
    if daemon_running; then
        kill "$(cat "$PID_FILE")" && msg "демон (pid $(cat "$PID_FILE")) остановлен"
    else
        msg "демон не запущен"
    fi
    rm -f "$PID_FILE"
}

hook_install() {
    if [ -f "$BASHRC" ] && grep -q "$HOOK_MARK" "$BASHRC"; then
        msg "== хук в ~/.bashrc уже стоит"
    else
        msg "== добавляю хук в ~/.bashrc (source + обновление перед каждым промптом)"
        {
            printf '\n# %s\n' "$HOOK_MARK"
            cat <<'EOF'
[ -f "$HOME/.config/alive-proxy/proxy.env" ] && source "$HOME/.config/alive-proxy/proxy.env"
PROMPT_COMMAND='[ -f "$HOME/.config/alive-proxy/proxy.env" ] && . "$HOME/.config/alive-proxy/proxy.env"; '"${PROMPT_COMMAND:+$'\n'$PROMPT_COMMAND}"
EOF
            printf '# <<< alive-proxy <<<\n'
        } >> "$BASHRC" || die "не получилось дописать в $BASHRC"
    fi
}

hook_remove() {
    if [ -f "$BASHRC" ] && grep -q "$HOOK_MARK" "$BASHRC"; then
        sed -i '/>>> alive-proxy >>>/,/<<< alive-proxy <<</d' "$BASHRC"
        msg "хук удалён из ~/.bashrc"
    else
        msg "хука в ~/.bashrc нет"
    fi
}

# ---------------------------------------------------------------- действия

do_start() {
    ensure_venv
    msg "== ротация: ищу ДРУГОГО живого прокси"
    # GNOME обновляется автоматически вместе со сменой прокси (внутри alive_proxy)
    "$PY" alive_proxy.py refresh -f -t 4 \
        || msg "   живых вообще не нашли — демон продолжит попытки сам"
    hook_install
    mkdir -p "$STATE_DIR"
    if daemon_running; then
        msg "== демон уже работает (pid $(cat "$PID_FILE"))"
    else
        msg "== запускаю демона (nohup)"
        nohup "$PY" alive_proxy.py daemon >> "$STATE_DIR/daemon.out" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 1
        daemon_running || die "демон умер сразу после старта — смотри $STATE_DIR/daemon.out"
        msg "   демон запущен, pid $(cat "$PID_FILE")"
    fi
    do_status_quiet
}

do_rotate() {
    ensure_venv
    msg "== ротация: ищу ДРУГОГО живого прокси"
    # GNOME обновляется автоматически вместе со сменой прокси (внутри alive_proxy)
    if "$PY" alive_proxy.py refresh -f -t 4 >/dev/null; then
        do_status_quiet
    else
        msg "живых прокси вообще не нашли — текущий не тронут"
    fi
}

do_status()       { ensure_venv; "$PY" alive_proxy.py status -t 4 || true; }
do_status_quiet() { "$PY" alive_proxy.py status -n || true; }  # без проб-запроса: только что проверяли

do_stop() { daemon_stop; }

do_uninstall() {
    ensure_venv
    msg "== откат всех настроек прокси"
    daemon_stop
    hook_remove
    "$PY" alive_proxy.py gnome-off 2>/dev/null \
        && msg "системный прокси сброшен на умолчания" \
        || msg "графическое окружение не тронуто (нет утилит настройки/сессии)"
    rm -rf "$STATE_DIR" && msg "удалено состояние: $STATE_DIR"
    msg "остались нетронутыми: сам скрипт и .venv в $(pwd)"
    DID_UNINSTALL=1
}

# ---------------------------------------------------------------- меню

pause() { printf '\n'; read -r -p "Enter — в меню (Ctrl-C — выход)... " _ 2>/dev/null </dev/tty || true; }

menu() {
    trap 'echo; msg "выход"; exit 0' INT
    while true; do
        echo
        msg "===== alive-proxy: живой бесплатный прокси ====="
        cur="$("$PY" alive_proxy.py status 2>/dev/null | head -1 | cut -d: -f2- | xargs)"
        daemon_running && msg "демон: работает (pid $(cat "$PID_FILE"))" || msg "демон: не запущен"
        [ -n "$cur" ] && msg "текущий прокси: $cur"
        cat <<'MENU'
  1) запустить всё (ротация прокси + хук bashrc + GNOME + демон)
  2) статус прокси
  3) сменить прокси на другого живого
  4) остановить демона
  5) удалить ВСЕ настройки прокси (uninstall)
  0) выход
MENU
        read -r -p "выбор> " choice || break
        case "$choice" in
            1) do_start; pause ;;
            2) do_status; pause ;;
            3) do_rotate; pause ;;
            4) do_stop; pause ;;
            5) read -r -p "точно удалить все настройки прокси? [y/N] " yn 2>/dev/null </dev/tty || yn=n
               case "$yn" in [Yy]*) do_uninstall; pause ;; *) msg "отменено" ;; esac ;;
            0) break ;;
            *) msg "нет такого пункта" ;;
        esac
    done
    msg "выход"
}

# ---------------------------------------------------------------- разбор аргументов

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    start)      do_start ;;
    rotate)     do_rotate ;;
    status)     do_status ;;
    stop)       do_stop ;;
    uninstall)  do_uninstall ;;
    menu)       ensure_venv; menu ;;
    "")         if [ -t 0 ] && [ -t 1 ]; then ensure_venv; menu; else do_start; fi ;;
    -h|--help)  usage ;;
    *)          usage; exit 1 ;;
esac

# ---------------------------------------------------------------- если запустили через source

if [ "${BASH_SOURCE[0]:-}" != "$0" ]; then
    if [ "$DID_UNINSTALL" = 1 ]; then
        unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY 2>/dev/null
        msg "прокси-переменные убраны из текущего шелла"
    elif [ -f "$ENV_FILE" ]; then
        # shellcheck disable=SC1090
        . "$ENV_FILE" && msg "прокси применён в текущем шелле: $http_proxy"
    fi
fi
