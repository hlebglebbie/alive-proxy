# alive-proxy

Keep `http_proxy` / `https_proxy` (and `all_proxy`) pointed at a **live** free proxy,
push it into your desktop environment's system-proxy settings, and run a background
daemon that swaps in a fresh proxy the moment the current one dies.

Two front-ends share one core (`alive_proxy.py`):

- **CLI + daemon** — scriptable, no UI.
- **ProxyPilot** — an optional Tkinter GUI proxy scanner/tester.

Русская версия: [README.ru.md](README.ru.md)

## Features

- Candidate proxies come from the [`free-proxy`](https://pypi.org/project/free-proxy/)
  package (sslproxies.org + free-proxy-list.net).
- Probes run **in parallel** (default 32 threads) with two strict checks:
  1. quick probe — `204` from `google.com/generate_204` through the proxy
     (exactly `204`, empty body — this also rejects interceptors);
  2. confirmation — a real HTTPS page loaded through the proxy tunnel.
- The winner is written to `~/.config/alive-proxy/proxy.env` (export lines) plus
  `state.json` (current proxy + a "graveyard" of dead ones with a cooldown).
- Sets the proxy as the **system proxy** of the current desktop environment.
- The `daemon` watches the current proxy and replaces it as soon as it dies.
- `run -- CMD` executes any command with the proxy injected into its environment.
- The GUI collects from many sources at once, mass-tests them, and offers a
  one-click best-proxy autopick.

## Supported desktop environments

The environment is detected automatically (`XDG_CURRENT_DESKTOP`,
`XDG_SESSION_DESKTOP`, `DESKTOP_SESSION`, with a fallback to available tools).

| Environment(s) | System-proxy backend |
| --- | --- |
| GNOME, Cinnamon, MATE, Unity, Budgie, Pantheon | `gsettings` → `org.gnome.system.proxy` |
| KDE Plasma 5 / 6 | `kwriteconfig` → `~/.config/kioslaverc` |
| XFCE, LXQt, LXDE | none — shell environment only |

## Requirements

- Linux; the GUI needs an X11/XWayland display.
- Python 3.10+ (developed on 3.14).

`free-proxy` and `requests` are installed into a local `.venv` automatically on
first run.

## Quick start

```bash
git clone https://github.com/<your-name>/alive-proxy.git
cd alive-proxy

./start.sh            # interactive menu
./start.sh start      # rotate proxy, install bash hook + system proxy + daemon
./start.sh status     # current proxy and whether it is alive
./start.sh uninstall  # remove hook, system proxy and state
```

Apply the proxy to the current shell without the daemon:

```bash
source ./start.sh     # runs it and sources proxy.env into the current shell
```

## CLI (`alive_proxy.py`)

```bash
.venv/bin/python alive_proxy.py refresh [-f] [-t SEC] [-w N]    # find/verify a live proxy, print export lines
eval "$(.venv/bin/python alive_proxy.py refresh)"               # apply in the current shell
.venv/bin/python alive_proxy.py export                          # print current export lines
.venv/bin/python alive_proxy.py status [-n]                     # current proxy + alive?
.venv/bin/python alive_proxy.py daemon [--interval SEC] [--max-backoff SEC]
.venv/bin/python alive_proxy.py run -- curl -s ifconfig.me      # run CMD through the proxy
.venv/bin/python alive_proxy.py install-systemd                 # systemd --user unit for the daemon
.venv/bin/python alive_proxy.py gnome                           # apply proxy to the current DE's system proxy
.venv/bin/python alive_proxy.py gnome-off                       # reset the DE system proxy
.venv/bin/python alive_proxy.py shell-hook                      # print the ~/.bashrc hook lines
```

Override the state directory with `ALIVE_PROXY_HOME=/some/path`.

## GUI (ProxyPilot)

```bash
./gui.sh        # or: .venv/bin/python proxy_gui.py
```

Sources: free-proxy (sslproxies.org, free-proxy-list.net), GitHub aggregators
(TheSpeedX, monosans, clarketm, mmpx12, roosterkid), ProxyScrape,
proxy-list.download and Geonode. Double-click a row (or press the connect button)
to set it as the system proxy.

## What it changes on your system

`alive_proxy.py refresh` and `./start.sh start` modify the host, not just the repo:

- `~/.config/alive-proxy/` — `proxy.env`, `state.json`, daemon pid/log;
- a hook appended to `~/.bashrc`;
- the system proxy via `gsettings` (GNOME family) or `kwriteconfig` (KDE);
- optionally a systemd **user** unit (`install-systemd`).

`./start.sh uninstall` and `alive_proxy.py gnome-off` revert these changes.

## License

[MIT](LICENSE)