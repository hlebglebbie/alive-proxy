# alive-proxy

Держит `http_proxy` / `https_proxy` (и `all_proxy`) всегда указывающими на **живой**
бесплатный прокси, прописывает его как системный прокси текущего графического
окружения и запускает фоновый демон-сторож, который подменяет прокси, как только
текущий умирает.

Два интерфейса на одном ядре (`alive_proxy.py`):

- **CLI + демон** — скриптуемо, без GUI.
- **ProxyPilot** — опциональный GUI-тестер прокси (tkinter).

English version: [README.md](README.md)

## Возможности

- Кандидаты берутся из pip-пакета [`free-proxy`](https://pypi.org/project/free-proxy/)
  (sslproxies.org + free-proxy-list.net).
- Проверка идёт **параллельно** (по умолчанию 32 потока) в два строгих этапа:
  1. быстрая проба — `204` от `google.com/generate_204` через прокси
     (строго `204` и пустое тело — отсекает перехватчиков);
  2. точная проба — реальная HTTPS-страница через туннель прокси.
- Победитель пишется в `~/.config/alive-proxy/proxy.env` (export-строки) и в
  `state.json` (текущий прокси + «кладбище» умерших с кулдауном).
- Прокси ставится как **системный** для текущего графического окружения.
- Демон `daemon` следит за текущим прокси и меняет его, как только он умер.
- `run -- CMD` выполняет любую команду с прокси в окружении.
- GUI собирает списки сразу из многих источников, массово проверяет их и умеет
  в один клик подобрать лучший живой прокси.

## Поддерживаемые графические окружения

Окружение определяется автоматически (`XDG_CURRENT_DESKTOP`,
`XDG_SESSION_DESKTOP`, `DESKTOP_SESSION`, запасной вариант — по наличию утилит).

| Окружение | Настройка системного прокси |
| --- | --- |
| GNOME, Cinnamon, MATE, Unity, Budgie, Pantheon | `gsettings` → `org.gnome.system.proxy` |
| KDE Plasma 5 / 6 | `kwriteconfig` → `~/.config/kioslaverc` |
| XFCE, LXQt, LXDE | нет — только переменные окружения |

## Требования

- Linux; для GUI нужен дисплей X11/XWayland.
- Python 3.10+ (разрабатывалось на 3.14).

`free-proxy` и `requests` ставятся в локальный `.venv` автоматически при первом
запуске.

## Быстрый старт

```bash
git clone https://github.com/<your-name>/alive-proxy.git
cd alive-proxy

./start.sh            # интерактивное меню
./start.sh start      # ротация прокси + хук bashrc + системный прокси + демон
./start.sh status     # текущий прокси и жив ли он
./start.sh uninstall  # убрать хук, системный прокси и состояние
```

Применить прокси к текущему шеллу без демона:

```bash
source ./start.sh     # запускает и подключает proxy.env в текущий шелл
```

## CLI (`alive_proxy.py`)

```bash
.venv/bin/python alive_proxy.py refresh [-f] [-t SEC] [-w N]    # найти/проверить живой прокси, напечатать export-строки
eval "$(.venv/bin/python alive_proxy.py refresh)"               # применить в текущем шелле
.venv/bin/python alive_proxy.py export                          # напечатать текущие export-строки
.venv/bin/python alive_proxy.py status [-n]                     # текущий прокси + жив ли он
.venv/bin/python alive_proxy.py daemon [--interval SEC] [--max-backoff SEC]
.venv/bin/python alive_proxy.py run -- curl -s ifconfig.me      # выполнить CMD через прокси
.venv/bin/python alive_proxy.py install-systemd                 # user-юнит systemd для демона
.venv/bin/python alive_proxy.py gnome                           # применить прокси как системный в текущем DE
.venv/bin/python alive_proxy.py gnome-off                       # сбросить системный прокси текущего DE
.venv/bin/python alive_proxy.py shell-hook                      # напечатать строки хука для ~/.bashrc
```

Каталог состояния можно переопределить через `ALIVE_PROXY_HOME=/some/path`.

## GUI (ProxyPilot)

```bash
./gui.sh        # или: .venv/bin/python proxy_gui.py
```

Источники: free-proxy (sslproxies.org, free-proxy-list.net), GitHub-агрегаторы
(TheSpeedX, monosans, clarketm, mmpx12, roosterkid), ProxyScrape,
proxy-list.download и Geonode. Двойной клик по строке (или большая кнопка)
задают прокси как системный.

## Что изменяется на системе

`alive_proxy.py refresh` и `./start.sh start` меняют систему, а не только репозиторий:

- `~/.config/alive-proxy/` — `proxy.env`, `state.json`, pid/лог демона;
- хук, дописанный в `~/.bashrc`;
- системный прокси через `gsettings` (GNOME-семейство) или `kwriteconfig` (KDE);
- опционально systemd **user**-юнит (`install-systemd`).

`./start.sh uninstall` и `alive_proxy.py gnome-off` откатывают эти изменения.

## Лицензия

[MIT](LICENSE)