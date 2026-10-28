#!/usr/bin/env python3
"""mc-ping-logger: periodic reachability tester for a Minecraft server.

Every INTERVAL_MINUTES (default 30) it runs a battery of tests against the
target and writes:

  data/results.jsonl          - one JSON object per test per cycle (machine)
  data/app.log                - human-readable log, incl. traceroute hops

Test battery per cycle:

  mc_ping       - real Minecraft status ping (modern 1.13+ protocol with
                  legacy 0xFE fallback). Records protocol version,
                  player count, MOTD. Most diagnostic test.
  tcp_connect   - raw TCP connect to the port, latency in ms.
  icmp_ping     - ICMP ping (needs NET_RAW in containers), loss % + RTT.
  tcp_trace     - TCP traceroute to the port (tracepath -T / -p), the hop
                  list shows WHICH router on the path stops answering.
  control_*     - the same icmp/tcp tests against CONTROL_HOSTS
                  (default 1.1.1.1) so a failure can be attributed:
                  "my network" vs "the server".

Analysis after a few days:

  python summarize.py            # hour-of-day failure table (UTC), per-day
                                 # table, failure windows, CSV export

Environment variables (see README.md): TARGET_HOST, TARGET_PORT,
INTERVAL_MINUTES, LOG_DIR, CONTROL_HOSTS, CONTROL_PORT, MC_PROTOCOL_VERSION,
KEEP_DAYS, MAX_CYCLES, TEST_TIMEOUT_SECONDS, TRACE_TIMEOUT_SECONDS.

Modules:
  config.py      - environment configuration
  net_tools.py   - TCP connect / ICMP ping / traceroute via system tools
  mc_protocol.py - Minecraft protocol pings
  ping_logger.py - this file: scheduling, logging, verdict logic
  summarize.py   - offline analysis of results*.jsonl
"""

from __future__ import annotations

import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

from config import (CONTROL_HOSTS, CONTROL_PORT, INTERVAL_MIN, KEEP_DAYS,
                    LOG_DIR, MC_PROTOCOL_VERSION, MAX_CYCLES, TARGET_HOST,
                    TARGET_PORT)
from mc_protocol import mc_ping_test
from net_tools import icmp_ping_test, run_cmd, tcp_connect_test, tcp_trace_test

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def _log_paths() -> tuple[str, str]:
    return os.path.join(LOG_DIR, "results.jsonl"), os.path.join(LOG_DIR, "app.log")


def write_record(rec: dict) -> None:
    jsonl, _ = _log_paths()
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_log_line(text: str) -> None:
    _, app = _log_paths()
    with open(app, "a", encoding="utf-8") as f:
        f.write(text + "\n")


_last_date = ""


def maybe_rotate() -> None:
    """Roll current-day log files to dated files when the UTC day changes."""
    global _last_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today == _last_date:
        return
    if _last_date:
        jsonl, app = _log_paths()
        for src, name in ((jsonl, "results"), (app, "app")):
            if os.path.exists(src):
                dst = os.path.join(LOG_DIR, f"{name}-{_last_date}.jsonl"
                                   if name == "results" else f"{name}-{_last_date}.log")
                os.replace(src, dst)
        _prune_old(today)
    _last_date = today


def _prune_old(today: str) -> None:
    cutoff = datetime.strptime(today, "%Y-%m-%d").timestamp() - KEEP_DAYS * 86400
    for fn in os.listdir(LOG_DIR):
        if fn.startswith(("results-", "app-")) and ("-" in fn):
            date = fn.split("-", 2)[1]
            try:
                if datetime.strptime(date, "%Y-%m-%d").timestamp() < cutoff:
                    os.unlink(os.path.join(LOG_DIR, fn))
            except ValueError:
                pass


# --------------------------------------------------------------------------
# One test cycle
# --------------------------------------------------------------------------

def _summary_line(test: str, res: dict) -> str:
    if test == "mc_ping":
        who = f" {res['server_version']} {res['players_online']}/{res['players_max']}" if res["server_version"] else ""
        return f"mc_ping      : {'OK' if res['ok'] else 'FAIL'}  {res['total_ms']}ms total, {res['connect_ms']}ms connect{who}"
    if test == "tcp_connect":
        return f"tcp_connect  : {'OK' if res['ok'] else 'FAIL'}  {res['ms']}ms"
    if test == "icmp_ping":
        rtt = f" avg {res['rtt_avg_ms']}ms" if res["rtt_avg_ms"] is not None else ""
        return f"icmp_ping    : {'OK' if res['ok'] else 'FAIL'}  {res['recv']}/{res['sent']} ({res['loss_pct']:.0f}% loss){rtt}"
    if test == "tcp_trace":
        where = f" reached {res['host']} in {res['hops_count']} hops" if res["ok"] else f" NOT reached ({res['hops_count']} hops seen)"
        return f"tcp_trace    : {'OK' if res['ok'] else 'FAIL'}  {res['tool']}{where}"
    return f"{test:13s}: {'OK' if res['ok'] else 'FAIL'}  {json.dumps({k: v for k, v in res.items() if v is not None and k != 'error'})[:120]}"


def run_cycle(cycle: int) -> dict:
    ts = utc_now_iso()
    epoch = time.time()
    base = {"ts": ts, "epoch": round(epoch, 3), "cycle": cycle,
            "host": TARGET_HOST, "port": TARGET_PORT}

    write_log_line(f"===== {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} cycle {cycle} =====")
    results: dict[str, dict] = {}
    for name in ("mc_ping", "tcp_connect", "icmp_ping", "tcp_trace"):
        if _stop:
            return {}
        t0 = time.time()
        if name == "mc_ping":
            res = mc_ping_test(TARGET_HOST, TARGET_PORT)
        elif name == "tcp_connect":
            res = tcp_connect_test(TARGET_HOST, TARGET_PORT)
        elif name == "icmp_ping":
            res = icmp_ping_test(TARGET_HOST)
        else:
            res = tcp_trace_test(TARGET_HOST, TARGET_PORT)
        res["duration_s"] = round(time.time() - t0, 1)
        results[name] = res
        rec = {**base, "test": name, **res}
        write_record(rec)
        line = _summary_line(name, res)
        if res.get("error"):
            line += f"  [{res['error']}]"
        write_log_line(line)
        if name == "tcp_trace" and res["hops"]:
            for h in res["hops"]:
                ms = f"{h['ms']}ms" if h["ms"] is not None else "???"
                write_log_line(f"    {h['n']:>2}  {h['addr']:<20} {ms}")

    control_ok = True
    for i, chost in enumerate(CONTROL_HOSTS):
        if _stop:
            return {}
        ci = icmp_ping_test(chost)
        ci["control"] = i
        write_record({**base, "test": "control_icmp", **ci})
        write_log_line(f"control_icmp : {'OK' if ci['ok'] else 'FAIL'}  {chost} {ci['recv']}/{ci['sent']}"
                       + (f" avg {ci['rtt_avg_ms']}ms" if ci["rtt_avg_ms"] is not None else ""))
        ct = tcp_connect_test(chost, CONTROL_PORT)
        ct["host"], ct["control"] = chost, i
        write_record({**base, "test": "control_tcp", **ct})
        write_log_line(f"control_tcp  : {'OK' if ct['ok'] else 'FAIL'}  {chost}:{CONTROL_PORT} {ct['ms']}ms")
        control_ok = control_ok and ci["ok"] and ct["ok"]
        if ci.get("error"):
            write_log_line(f"    icmp error: {ci['error']}")
        if ct.get("error"):
            write_log_line(f"    tcp  error: {ct['error']}")

    target_ok = results["mc_ping"]["ok"] and results["tcp_connect"]["ok"]
    if target_ok and control_ok:
        verdict = "target reachable, local network healthy"
    elif target_ok and not control_ok:
        verdict = "target reachable, local network has issues"
    elif not target_ok and control_ok:
        verdict = "TARGET UNREACHABLE but local network healthy -> server-side/path problem"
    else:
        verdict = "TARGET UNREACHABLE and local network has issues -> likely own network/ISP"
    write_log_line(f"--> {verdict}")

    summary = {"ts": ts, "epoch": round(time.time(), 3), "cycle": cycle,
               "test": "summary", "host": TARGET_HOST, "port": TARGET_PORT,
               "target_ok": target_ok, "control_ok": control_ok, "verdict": verdict,
               "mc_ping_ms": results["mc_ping"]["total_ms"],
               "tcp_ms": results["tcp_connect"]["ms"]}
    write_record(summary)
    return summary


def print_startup() -> None:
    write_log_line(f"===== mc-ping-logger started (pid {os.getpid()}) =====")
    write_log_line(f"target={TARGET_HOST}:{TARGET_PORT} interval={INTERVAL_MIN}min "
                   f"controls={CONTROL_HOSTS} mc_protocol={MC_PROTOCOL_VERSION} keep_days={KEEP_DAYS}")
    rc, out, _ = run_cmd(["hostname", "-i"], 5.0)
    if rc == 0 and out.strip():
        write_log_line(f"container ip: {out.strip()}")
    rc, out, _ = run_cmd(["ip", "route", "show", "default"], 5.0)
    if rc == 0 and out.strip():
        write_log_line(f"default route: {out.strip()}")


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    os.makedirs(LOG_DIR, exist_ok=True)
    print_startup()

    cycle = 0
    while not _stop:
        maybe_rotate()
        cycle += 1
        try:
            summary = run_cycle(cycle)
        except Exception as e:  # never let one bad cycle kill the logger
            write_log_line(f"cycle {cycle} crashed: {e!r}")
            summary = None
        if summary:
            print(f"[{summary['ts']}] cycle {cycle}: target_ok={summary['target_ok']} "
                  f"control_ok={summary['control_ok']} mc={summary['mc_ping_ms']}ms", flush=True)
        if MAX_CYCLES and cycle >= MAX_CYCLES:
            break
        period = INTERVAL_MIN * 60
        next_t = (int(time.time()) // period + 1) * period
        wait_s = next_t - time.time() + random.uniform(0, 5)
        print(f"next cycle in {wait_s:.0f}s (at {datetime.fromtimestamp(next_t).strftime('%H:%M:%S')} local)", flush=True)
        while wait_s > 0 and not _stop:
            time.sleep(min(1.0, wait_s))
            wait_s -= 1.0

    write_log_line("===== mc-ping-logger stopped =====")
    maybe_rotate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
