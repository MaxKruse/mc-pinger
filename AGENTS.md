# mc-ping-logger - agent notes

## Purpose

Diagnose time-of-day unreachability of a Minecraft server
(default `169.155.120.31:9080`). The app runs a fixed test battery every
30 minutes and records results for offline analysis. The whole point is
attribution: target-only failures => server-side; target+control failures
=> user's network/ISP.

## Files

| File | Role |
| --- | --- |
| `config.py` | All env-var configuration (`TARGET_HOST`, `INTERVAL_MINUTES`, ...). |
| `net_tools.py` | TCP connect, ICMP ping, traceroute via system tools + locale-tolerant output parsing (German Windows included). |
| `mc_protocol.py` | Minecraft status ping: varints, modern 1.13+ handshake, legacy 0xFE fallback. |
| `ping_logger.py` | Orchestrator: scheduler, JSONL/log writing + rotation, verdict logic, `main`. |
| `summarize.py` | Offline analyzer for `results*.jsonl`: hour-of-day table, per-day table, failure windows, optional CSV. |
| `Dockerfile` | `python:3.12-alpine` + `iproute2` (ip) + `iputils` (tracepath/TCP traceroute, GNU ping). Modern alpine moved tracepath OUT of iproute2 into iputils - if tracepath is "missing" in the image, iputils is the fix. |
| `docker-compose.yml` | No published ports. `cap_add: NET_RAW` (required for ping + tracepath). `./data:/data` volume. |
| `README.md` | User-facing: what it does, how to run, how to evaluate. |

## Data model

`data/results.jsonl` (current day) + `data/results-YYYY-MM-DD.jsonl`:
**one JSON object per line, per test, per cycle**. Common fields:
`ts` (ISO UTC) - the stable per-cycle identity: every record of a cycle
(including the `summary`, whose `epoch` is later) shares the cycle's base
`ts`; `epoch` (float seconds), `cycle` (int, resets to 1 on container
restart - never a global sequence), `host`, `port`, `test`.

`test` values and their key fields:

- `mc_ping`: `ok`, `connect_ms`, `total_ms`, `protocol` ("modern"|"legacy"),
  `server_version`, `players_online`, `players_max`, `motd`
- `tcp_connect`: `ok`, `ms`
- `tcp_trace`: `ok` (reached target), `tool`, `hops` (list of
  `{n, addr, ms, reached}`), `hops_count`, `raw` (truncated to 4000 chars)
- `control_icmp` / `control_tcp`: same shapes as `icmp_ping` / `tcp_connect`,
  plus `host`, `control` (index into CONTROL_HOSTS)
- `summary`: `target_ok` (= mc_ping.ok AND tcp_connect.ok), `control_ok`,
  `verdict` (human string)

`data/app.log` + `data/app-YYYY-MM-DD.log`: human-readable, one section per
cycle, includes full trace hop list.

**Invariants:**
- `summarize.py` and `ping_logger.py` must stay in sync on `test` names and
  field names (LATENCY_KEY in summarize.py).
- Rotation is by UTC day: on first cycle after midnight the current
  `results.jsonl`/`app.log` are renamed to the dated names and pruning of
  files older than `KEEP_DAYS` happens. Never rename the *new* day's files.
- A cycle must never crash the daemon: `run_cycle` is wrapped in
  try/except in `main`.
- **Ping output parsing** (`icmp_ping_test`) handles busybox (alpine), GNU,
  English Windows, and German Windows (`Gesendet/Empfangen/Verloren`,
  `Durchschnitt`) via `_PING_LOSS_RES` / `_PING_RTT_RES`. Keep all branches
  working if you touch them. `run_cmd` decodes bytes with
  `errors="replace"` - never switch to `text=True` (crashes on cp1252 output).

## Gotchas

- **Protocol:** `mc_modern_ping` speaks 1.13+ (handshake id 0x00, next
  state 2, then status request). `MC_PROTOCOL_VERSION` default 769
  (1.21.x); servers generally accept any version for status. If the target
  is a pre-1.13 server, `mc_legacy_ping` (0xFE) is the fallback - it
  returns `protocol: "legacy"`.
- **Tools differ by platform:** `tcp_trace` picks `tracepath` (TCP mode)
  when present, else `traceroute -n -w 2 host`, else `tracert -d host`
  (Windows, ICMP-only - no TCP mode). There are TWO incompatible
  `tracepath` implementations: iproute2 (`-T host:port`) and iputils
  (`-p port host`); modern alpine ships iputils. `_detect_tracepath_style()`
  in `net_tools.py` probes `tracepath --help` once and caches the flavor.
- **Ping output parsing** (`icmp_ping_test`) handles busybox (alpine), GNU,
  English Windows, and German Windows via `_PING_LOSS_RES` / `_PING_RTT_RES`.
  German specifics (verified live): header `Ping-Statistik`, the `(N%
  Verlust)` pct is on the line AFTER `Verloren = N` (regex uses `\s*`), and
  the average keyword is `Mittelwert` (older builds `Durchschnitt`). Keep
  all branches working if you touch them. `run_cmd` decodes bytes with
  `errors="replace"` - never switch to `text=True` (crashes on cp1252 output).
- **ICMP to game servers is often blocked** - `icmp_ping` 100% loss with
  a working `mc_ping` is a normal state, not a bug. Windows also counts
  ICMP error replies ("Zielnetz nicht erreichbar") as *received*, so 0%
  loss does not always mean the target answered.
- **Container traceroute only shows the Docker gateway.** The Docker
  Desktop VM forwards traffic but never answers TTL-expired probes, so
  inside the container `tcp_trace` resolves hop 1 (172.17.0.1) then
  "no reply" - a tooling limit, not network loss. The *local* one-shot
  (`uv run ping_logger.py` on Windows, `tracert`) shows the real path.
- `cycle` numbers restart after container restarts; `summarize.failure_windows`
  groups by epoch gap (<2h) for exactly this reason.
- Windows `nul` redirections and `taskkill /IM` are forbidden in this
  repo's agent rules (see `~/.omp/agent/AGENTS.md`); test the daemon in
  Docker, not as a local process.
- Port scheme: this service publishes **no** ports. Never add published
  ports; the daemon is outbound-only.

## Testing

- One-shot local cycle (Windows, uses system tools): `uv run ping_logger.py`
  with `MAX_CYCLES=1 LOG_DIR=data` set.
- One-shot container cycle: `docker compose run --rm --entrypoint
  "python -u /app/ping_logger.py" mc-ping-logger` with `MAX_CYCLES=1` in
  the environment.
- Summarizer check: `uv run summarize.py --dir data` after any cycle.
