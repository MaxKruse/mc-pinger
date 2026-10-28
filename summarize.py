#!/usr/bin/env python3
"""summarize.py: analyze mc-ping-logger results and find failure patterns.

Reads all results*.jsonl files in a data directory and prints:

  1. An hour-of-day table: for each hour (00-23) the number of samples,
     target reachability failure rate, median latencies, and ICMP loss.
     This is the key view for "works at 3am, dead at 7pm" style problems.
  2. A per-day table: cycle count, target failures, control failures.
  3. Failure windows: contiguous stretches where the target was unreachable.

Usage:
  python summarize.py --dir data
  python summarize.py --dir data --days 14
  python summarize.py --dir data --since 2026-10-01 --csv hourly.csv
  python summarize.py --dir data --tz Europe/Berlin

Stdlib only. Timestamps in the JSONL are UTC; --tz converts display times.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone

TESTS = ("mc_ping", "tcp_connect", "icmp_ping", "tcp_trace")
LATENCY_KEY = {
    "mc_ping": "total_ms",
    "tcp_connect": "ms",
    "icmp_ping": "rtt_avg_ms",
    "control_icmp": "rtt_avg_ms",
    "control_tcp": "ms",
}


def load_records(dir: str, since: datetime | None, days: float | None) -> list[dict]:
    files = sorted(glob.glob(os.path.join(dir, "results*.jsonl")))
    if not files:
        print(f"no results*.jsonl files found in {dir!r}", file=sys.stderr)
        sys.exit(1)
    recs = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    recs = [r for r in recs if r.get("epoch")]
    if not recs:
        print("no records with epoch timestamps found", file=sys.stderr)
        sys.exit(1)
    if days:
        cutoff = max(r["epoch"] for r in recs) - days * 86400
        recs = [r for r in recs if r["epoch"] >= cutoff]
    if since:
        recs = [r for r in recs if r["epoch"] >= since.timestamp()]
    recs.sort(key=lambda r: r["epoch"])
    return recs


def cycles(recs: list[dict]) -> dict[int, dict]:
    """Group test records by cycle number (each cycle shares a ts/epoch)."""
    out: dict[int, dict] = {}
    for r in recs:
        c = out.setdefault(r.get("cycle", 0), {"epoch": r["epoch"], "tests": {}})
        c["tests"][r.get("test", "?")] = r
        if r["epoch"] > c["epoch"]:
            c["epoch"] = r["epoch"]
    return out


def med(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def fmt_ms(v: float | None) -> str:
    return f"{v:.0f}ms" if v is not None else "-"


def hour_table(recs: list[dict], tz: timezone) -> list[dict]:
    """One row per hour-of-day with aggregate stats over all samples."""
    rows = []
    by_hour: dict[int, dict] = {h: {} for h in range(24)}
    for r in recs:
        test = r.get("test")
        if test not in TESTS:
            continue
        h = datetime.fromtimestamp(r["epoch"], tz).hour
        by_hour[h].setdefault(test, []).append(r)
    for h in range(24):
        row = {"hour": h, "n": 0}
        for test in TESTS:
            rs = by_hour[h].get(test, [])
            row[f"{test}_n"] = len(rs)
            row[f"{test}_fail_pct"] = round(100.0 * sum(1 for x in rs if not x.get("ok")) / len(rs), 1) if rs else None
            if test == "tcp_trace":
                row[f"{test}_med"] = med([x.get("hops_count") for x in rs])
            else:
                row[f"{test}_med"] = med([x.get(LATENCY_KEY[test]) for x in rs])
            if test == "icmp_ping":
                row["icmp_loss_med"] = med([x.get("loss_pct") for x in rs])
        row["n"] = max((row[f"{t}_n"] for t in TESTS), default=0)
        rows.append(row)
    return rows


def print_hour_table(rows: list[dict]) -> None:
    print("Hour-of-day table (display times in --tz; all stats aggregate every sample in that hour):")
    print()
    print(f"{'hour':<6}{'n':>4}  {'mc_ping fail%':>14} {'mc median':>10}  "
          f"{'tcp fail%':>10} {'tcp median':>10}  {'icmp loss%':>10}  {'trace hops':>10}")
    for r in rows:
        if r["n"] == 0:
            print(f"{r['hour']:02d}     0  (no samples)")
            continue
        print(f"{r['hour']:02d}   {r['n']:>3}  "
              f"{r['mc_ping_fail_pct']!s:>14} {fmt_ms(r['mc_ping_med']):>10}  "
              f"{r['tcp_connect_fail_pct']!s:>10} {fmt_ms(r['tcp_connect_med']):>10}  "
              f"{r.get('icmp_loss_med')!s:>10}  "
              f"{r['tcp_trace_med']!s:>10}")


def day_table(cyc: dict[int, dict]) -> None:
    print()
    print("Per-day table:")
    by_day: dict[str, list[dict]] = {}
    for c in sorted(cyc.values(), key=lambda c: c["epoch"]):
        day = datetime.fromtimestamp(c["epoch"], timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(c)
    print(f"{'date':<12}{'cycles':>7}  {'target fails':>13}  {'control fails':>14}")
    for day in sorted(by_day):
        cs = by_day[day]
        tf = sum(1 for c in cs if c["tests"].get("summary", {}).get("target_ok") is False)
        cf = sum(1 for c in cs if c["tests"].get("summary", {}).get("control_ok") is False)
        print(f"{day:<12}{len(cs):>7}  {tf:>13}  {cf:>14}")


def failure_windows(cyc: dict[int, dict], tz: timezone) -> None:
    print()
    print("Target-unreachable windows (summary.target_ok=false, gap-tolerant grouping):")
    failed = [c for c in sorted(cyc.values(), key=lambda c: c["epoch"])
              if c["tests"].get("summary", {}).get("target_ok") is False]
    if not failed:
        print("  none - target was reachable in every completed cycle")
        return
    # Group failed cycles that are temporally contiguous (within 2h, i.e.
    # missing at most one 30-min sample) so container restarts do not split
    # a single failure window into several.
    groups: list[list[dict]] = []
    for c in failed:
        if groups and c["epoch"] - groups[-1][-1]["epoch"] < 2 * 3600:
            groups[-1].append(c)
        else:
            groups.append([c])
    for g in groups:
        start = datetime.fromtimestamp(g[0]["epoch"], tz)
        end = datetime.fromtimestamp(g[-1]["epoch"], tz)
        verdict = g[-1]["tests"].get("summary", {}).get("verdict", "?")
        print(f"  {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}  ({len(g)} cycles)  {verdict}")


def write_csv(path: str, rows: list[dict]) -> None:
    fields = ["hour", "n", "mc_ping_n", "mc_ping_fail_pct", "mc_ping_med",
              "tcp_connect_n", "tcp_connect_fail_pct", "tcp_connect_med",
              "icmp_ping_n", "icmp_ping_fail_pct", "icmp_loss_med",
              "tcp_trace_n", "tcp_trace_fail_pct", "tcp_trace_med"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nCSV written to {path}")


def parse_tz(name: str | None) -> timezone:
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        print(f"warning: unknown timezone {name!r}, using UTC", file=sys.stderr)
        return timezone.utc


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze mc-ping-logger results")
    ap.add_argument("--dir", default="data", help="directory with results*.jsonl (default: data)")
    ap.add_argument("--days", type=float, default=None, help="only last N days")
    ap.add_argument("--since", default=None, help="only records after YYYY-MM-DD (UTC)")
    ap.add_argument("--tz", default=None, help="IANA timezone for display, e.g. Europe/Berlin (default UTC)")
    ap.add_argument("--csv", default=None, help="write hour table to this CSV file")
    args = ap.parse_args()

    tz = parse_tz(args.tz)
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.since else None
    recs = load_records(args.dir, since, args.days)
    cyc = cycles(recs)

    first = datetime.fromtimestamp(recs[0]["epoch"], tz)
    last = datetime.fromtimestamp(recs[-1]["epoch"], tz)
    print(f"{len(recs)} records, {len(cyc)} cycles, {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} ({tz})")

    rows = hour_table(recs, tz)
    print_hour_table(rows)
    day_table(cyc)
    failure_windows(cyc, tz)
    if args.csv:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
