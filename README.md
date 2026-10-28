# mc-ping-logger

A small Docker app that tests a Minecraft server every 30 minutes and logs
everything, so you can figure out why it's unreachable in the evening but
fine at night and early morning.

## What it does

Every 30 minutes (aligned to :00 and :30), it runs this battery against
`169.155.120.31:9080` by default:

| Test | What it measures |
| --- | --- |
| `mc_ping` | A real Minecraft server-list ping: TCP connect, handshake, status request, JSON status response (server version, player count, MOTD). This is the "fake connect" your game client does when browsing servers. Works with modern (1.13+) servers; falls back to the legacy 0xFE protocol for very old ones. |
| `tcp_connect` | Bare TCP connect to the port, no payload. Distinguishes "port closed / filtered" from "handshake failed". |
| `icmp_ping` | 4 ICMP echo packets: packet loss + min/avg/max RTT. |
| `tcp_trace` | TCP traceroute to the server port (iproute2 `tracepath -T` in the container). Shows exactly which hop starts losing packets or adding latency when you can't connect. |
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

```bash
docker compose up -d --build
```

Logs go to `./data/` on the host. No ports are exposed; the container only
makes outbound connections.

## Log files (in `./data/`)

| File | Contents |
| --- | --- |
| `results.jsonl` | One JSON object per test per cycle - the machine-readable source of truth for analysis. |
| `results-YYYY-MM-DD.jsonl` | Same, rotated daily at UTC midnight. |
| `app.log` | Human-readable per-cycle report, including the full traceroute hop list and the verdict. |
| `app-YYYY-MM-DD.log` | Rotated daily. |

Files older than `KEEP_DAYS` (default 90) are deleted automatically.

## Evaluating the data

After a few days, run the summarizer (works on the host with any Python 3.10+,
or inside a container):

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

All settings are environment variables in `docker-compose.yml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TARGET_HOST` / `TARGET_PORT` | `169.155.120.31` / `9080` | The Minecraft server. |
| `INTERVAL_MINUTES` | `30` | Cycle interval; runs align to wall-clock boundaries. |
| `CONTROL_HOSTS` | `1.1.1.1` | Comma-separated control hosts for the "whose network is it" comparison. |
| `CONTROL_PORT` | `443` | Port for the control TCP test. |
| `TZ` | UTC | Timezone for the "next cycle" lines in `docker compose logs` (log files are always UTC). |
| `KEEP_DAYS` | `90` | Days to keep rotated log files. |
| `TEST_TIMEOUT_SECONDS` | `10` | Timeout per ping/connect test. |
| `TRACE_TIMEOUT_SECONDS` | `90` | Timeout for the traceroute. |
| `MAX_CYCLES` | `0` (forever) | Run N cycles and exit - handy for testing. |
| `LOG_DIR` | `/data` | Where logs are written inside the container. |

## If something breaks

- `docker compose logs mc-ping-logger` - startup banner shows target,
  interval, and container IP/default route.
- A crashed cycle is logged (`cycle N crashed: ...`) and the app keeps
  running; it does not exit on test errors.
- No logs appearing? Check `./data` exists and is writable; the container
  needs the `NET_RAW` capability (already set in `docker-compose.yml`)
  for ping/traceroute.
- The container runs no published ports and holds no state outside
  `./data/` - `docker compose down && up -d --build` is always safe.
