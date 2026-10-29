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
| `mc_protocol.py` | Minecraft status ping: varints, modern 1.13+ handshake, pre-1.13 field-order variant, legacy 0xFE fallback. See the Protocol gotcha. |
| `ping_logger.py` | Orchestrator: scheduler, JSONL/log writing + rotation, verdict logic, `main`. |
| `summarize.py` | Offline analyzer for `results*.jsonl`: hour-of-day table, per-day table, failure windows, optional CSV. |
| `dist/mc-ping-logger/` | PyInstaller build output (git-ignored): native Windows exe. Build: `uv run --with pyinstaller python -m PyInstaller --name mc-ping-logger --noconfirm ping_logger.py`. The frozen exe defaults `LOG_DIR` to a `data/` dir NEXT TO THE EXE (frozen branch in `config.py`), not the CWD. |
| `README.md` | User-facing: what it does, how to run, how to evaluate. |

## Data model

`data/results.jsonl` (current day) + `data/results-YYYY-MM-DD.jsonl`:
**one JSON object per line, per test, per cycle**. Common fields:
`ts` (ISO UTC) - the stable per-cycle identity: every record of a cycle
(including the `summary`, whose `epoch` is later) shares the cycle's base
`ts`; `epoch` (float seconds), `cycle` (int, resets to 1 on process
restart - never a global sequence), `host`, `port`, `test`.

`test` values and their key fields:

- `mc_ping`: `ok`, `connect_ms`, `total_ms`, `protocol`
  ("modern"|"pre113_order"|"legacy" - which handshake got the reply),
  `server_version`, `players_online`, `players_max`, `motd`; on total
  failure plus `fallback_errors` (map of the fallback attempts' errors)
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

- **Protocol (verified live 2026-10-29):** the front-end in front of the
  default target (a 1.20.1 Fabric server, MOTD "Homestead Server") does
  NOT answer the standard 1.13+ status handshake (state 2, modern field
  order): it accepts the TCP connection, reads the handshake, then closes
  cleanly with zero bytes (idle connections are kept open, so it is a
  payload reaction, not a firewall). It DOES answer the pre-1.13 field
  order (protocol version BEFORE next state, next state 1) followed by a
  single empty packet 0x00 - the exact byte sequence Prism Launcher's
  server list sends (`McClient::sendRequest`). Legacy 0xFE also gets
  closed. Therefore `mc_ping_test` tries in order: `mc_modern_ping` ->
  `mc_pre113_order_ping` -> `mc_legacy_ping`; the `protocol` field of the
  record shows which one succeeded (currently "pre113_order"). The
  pre113 reply sometimes takes ~5s (front-end read timeout) - covered by
  `TEST_TIMEOUT`. `_read_status_response` rejects non-status JSON
  (e.g. disconnect packets) so a fallback can't false-positive. If the
  target ever changes, re-verify all three orderings with a raw socket
  probe before trusting the data.
- **Tools differ by platform:** `tcp_trace` picks `tracepath` (TCP mode)
  when present, else `traceroute -n -w 2 host`, else `tracert -d host`
  (Windows, ICMP-only - no TCP mode). There are TWO incompatible
  `tracepath` implementations: iproute2 (`-T host:port`) and iputils
  (`-p port host`); `_detect_tracepath_style()` in `net_tools.py` probes
  `tracepath --help` once and caches the flavor.
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
- `cycle` numbers restart after process restarts; `summarize.failure_windows`
  groups by epoch gap (<2h) for exactly this reason.
- Windows `nul` redirections and `taskkill /IM` are forbidden in this
  repo's agent rules (see `~/.omp/agent/AGENTS.md`).
- This app is outbound-only: it never listens on a port and holds no
  state outside its log dir. Keep it that way.

## Testing

- One-shot local cycle (Windows, uses system tools): `uv run ping_logger.py`
  with `MAX_CYCLES=1 LOG_DIR=data` set.
- One-shot exe (after a build): `MAX_CYCLES=1 LOG_DIR=data
  dist/mc-ping-logger/mc-ping-logger.exe` (without `LOG_DIR` it writes
  next to the exe). Rebuild the exe after ANY code change before testing
  it.
- Summarizer check: `uv run summarize.py --dir data` after any cycle.
