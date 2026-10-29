# mc-ping-logger

A small native Windows app (single exe, no dependencies) that tests a
Minecraft server every 30 minutes and logs everything, so you can figure
out why it's unreachable in the evening but fine at night and early morning.

## What it does

Every 30 minutes (aligned to :00 and :30), it runs this battery against
`169.155.120.31:9080` by default:

| Test | What it measures |
| --- | --- |
| `mc_ping` | A real Minecraft server-list ping: TCP connect, handshake, status request, JSON status response (server version, player count, MOTD). This is the "fake connect" your game client does when browsing servers. Tries the standard modern (1.13+) handshake first, then a pre-1.13 handshake field-order variant (the default target's front-end only answers that one - that is what Prism Launcher sends), then the legacy 0xFE protocol for very old servers. |
| `tcp_connect` | Bare TCP connect to the port, no payload. Distinguishes "port closed / filtered" from "handshake failed". |
| `icmp_ping` | 4 ICMP echo packets: packet loss + min/avg/max RTT. |
| `tcp_trace` | Traceroute to the server (on Windows `tracert`, ICMP only). Shows exactly which hop starts losing packets or adding latency when you can't connect. |
| `control_icmp` / `control_tcp` | ICMP + TCP to a control host (default `1.1.1.1:443`). If the target fails but the control is fine, the problem is on the server's side of the path; if both fail, it's your own network/ISP. |

Each cycle also writes a plain-English verdict line, e.g.:
`TARGET UNREACHABLE but local network healthy -> server-side/path problem`.

### What it can't do

- It measures connectivity, not gameplay bandwidth. A connection that
  accepts the status ping can still be too slow for play; that would show
  up as high `mc_ping`/`tcp_connect` latencies and late-trace-hop latency
  spikes.
- Traceroute only works as far as routers answer TTL-expired packets.
  Many providers blackhole those, so the trace may stop at "?" even when
  the server is reachable. The ping tests are the ground truth; the trace
  is the diagnostic.
- ICMP to the server may be blocked by the server itself (common for game
  servers). 100% ICMP loss with a working `mc_ping` is normal, not an error.

## How to run it

Build the exe (one time, and after code changes):

```powershell
uv run --with pyinstaller python -m PyInstaller --name mc-ping-logger --noconfirm ping_logger.py
```

Then run it:

```powershell
dist\mc-ping-logger\mc-ping-logger.exe
```

The exe is a normal console program (leave the window open while it
runs). It makes only outbound connections - it never listens on a port.
It writes logs to a `data\` folder **next to the executable**, not to the
folder you launched it from. One-shot test run:

```powershell
$env:MAX_CYCLES="1"; .\dist\mc-ping-logger\mc-ping-logger.exe; Remove-Item Env:MAX_CYCLES
```

## Log files (in `data\` next to the exe)

| File | Contents |
| --- | --- |
| `results.jsonl` | One JSON object per test per cycle - the machine-readable source of truth for analysis. |
| `results-YYYY-MM-DD.jsonl` | Same, rotated daily at UTC midnight. |
| `app.log` | Human-readable per-cycle report, including the full traceroute hop list and the verdict. |
| `app-YYYY-MM-DD.log` | Rotated daily. |

Files older than `KEEP_DAYS` (default 90) are deleted automatically.

## Evaluating the data

After a few days, run the summarizer (any Python 3.10+):

```bash
python summarize.py --dir data --tz <your timezone>
```

It prints:

1. **Hour-of-day table** - for each hour 00-23: sample count, target
   failure rate, median latencies, ICMP loss. This directly shows whether
   failures cluster in the evening (your ISP) vs. late day (server).
2. **Per-day table** - cycles run, target failures, control failures.
3. **Failure windows** - each contiguous stretch where the target was
   unreachable, with the verdict from that cycle.

Useful options:

```bash
python summarize.py --dir data --days 14 --tz Europe/Berlin
python summarize.py --dir data --since 2026-10-01 --csv hourly.csv
```

Note: on Windows, `--tz` needs the `tzdata` package
(`pip install tzdata`); without it the summarizer warns and uses UTC.

Or slice the JSONL yourself, e.g. all failed Minecraft pings in the last
week with jq:

```bash
jq -c 'select(.test=="mc_ping" and .ok==false)' data/results*.jsonl
```

## Configuration

All settings are environment variables (set them in the console window
before starting the exe, or permanently via system environment settings):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TARGET_HOST` / `TARGET_PORT` | `169.155.120.31` / `9080` | The Minecraft server. |
| `INTERVAL_MINUTES` | `30` | Cycle interval; runs align to wall-clock boundaries. |
| `CONTROL_HOSTS` | `1.1.1.1` | Comma-separated control hosts for the "whose network is it" comparison. |
| `CONTROL_PORT` | `443` | Port for the control TCP test. |
| `KEEP_DAYS` | `90` | Days to keep rotated log files. |
| `TEST_TIMEOUT_SECONDS` | `10` | Timeout per ping/connect test. |
| `TRACE_TIMEOUT_SECONDS` | `90` | Timeout for the traceroute. |
| `MAX_CYCLES` | `0` (forever) | Run N cycles and exit - handy for testing. |
| `LOG_DIR` | `data\` next to the exe | Where logs are written. |

## If something breaks

- The startup banner (first lines of `data\app.log`) shows target,
  interval, and control hosts.
- A crashed cycle is logged (`cycle N crashed: ...`) and the app keeps
  running; it does not exit on test errors.
- No logs appearing? The `data\` folder next to the exe is created
  automatically - check that folder is writable.
- On Windows the trace uses `tracert` (ICMP only - there is no
  TCP-mode traceroute on Windows). The ping tests are the ground truth;
  the trace is the diagnostic.
