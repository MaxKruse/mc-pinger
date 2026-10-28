"""Shell-tool network tests: raw TCP connect, ICMP ping, traceroute.

Wraps system tools (ping, tracepath/traceroute/tracert) and parses their
output. Parsing must stay tolerant of busybox (alpine), GNU, English
Windows, and German Windows locales - the logger may run in the container
or directly on the user's machine.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time

from config import TEST_TIMEOUT, TRACE_TIMEOUT


def run_cmd(args: list[str], timeout: float) -> tuple[int | None, str, str]:
    """Run a command; (returncode, stdout, stderr). rc is None on timeout.

    Bytes are decoded with errors="replace" instead of text=True because
    some pings/traceroutes emit locale-specific bytes that would crash the
    subprocess reader thread on a non-UTF-8 locale (e.g. Windows cp1252).
    """
    def _dec(b: bytes | None) -> str:
        return b.decode("utf-8", "replace") if b else ""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, _dec(p.stdout), _dec(p.stderr)
    except subprocess.TimeoutExpired as e:
        return None, _dec(e.output), _dec(e.stderr)
    except FileNotFoundError:
        return -1, "", f"not found: {args[0]}"


# --------------------------------------------------------------------------
# Raw TCP connect
# --------------------------------------------------------------------------

def _open_tcp(host: str, port: int, timeout: float) -> tuple[socket.socket, float]:
    """Connect and return (socket, connect_ms)."""
    t0 = time.perf_counter()
    s = socket.create_connection((host, port), timeout=timeout)
    return s, (time.perf_counter() - t0) * 1000.0


def tcp_connect_test(host: str, port: int) -> dict:
    res = {"ms": None, "ok": False, "error": None}
    try:
        s, ms = _open_tcp(host, port, TEST_TIMEOUT)
        s.close()
        res.update(ms=round(ms, 1), ok=True)
    except OSError as e:
        res["error"] = f"connect failed: {e}"
    return res


# --------------------------------------------------------------------------
# ICMP ping
# --------------------------------------------------------------------------

# Loss line: (sent, received, loss_pct). Covers busybox (alpine), GNU
# ("N packets transmitted, M received"), English Windows, and German
# Windows statistics output.
_PING_LOSS_RES = (
    re.compile(r"(\d+) packets transmitted, (\d+) (?:packets? )?received, ([\d.]+)% packet loss"),
    re.compile(r"Sent = (\d+), Received = (\d+), Lost = \d+ \((\d+)% loss\)"),
    re.compile(r"Gesendet = (\d+), Empfangen = (\d+), Verloren = \d+\s*\((\d+)% Verlust\)"),
)
# Round-trip line: (min_ms, avg_ms, max_ms). Note the Windows/German
# forms order the fields min, max, avg (average last). German uses
# "Mittelwert"; the German loss percentage sits on the line after
# "Verloren = N", hence \s* between them.
_PING_RTT_RES = (
    re.compile(r"round-trip min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)(?:/[\d.]+)? ms"),
    re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/[\d.]+ ms"),
    re.compile(r"Minimum = (\d+)ms, Maximum = (\d+)ms, (?:Average|Mittelwert|Durchschnitt) = (\d+)ms"),
)


def _ping_args(host: str, count: int) -> list[str]:
    """Ping flags differ: busybox/GNU use -c/-W, Windows uses -n/-w (ms)."""
    if os.name == "nt":
        return ["ping", "-n", str(count), "-w", "2000", host]
    return ["ping", "-c", str(count), "-W", "2", host]


def _first_match(patterns: tuple[re.Pattern, ...], text: str):
    for p in patterns:
        m = p.search(text)
        if m:
            return m
    return None


def icmp_ping_test(host: str, count: int = 4) -> dict:
    res = {"host": host, "sent": count, "recv": 0, "loss_pct": 100.0,
           "rtt_min_ms": None, "rtt_avg_ms": None, "rtt_max_ms": None,
           "ok": False, "error": None}
    rc, out, err = run_cmd(_ping_args(host, count), 20.0)
    if rc is None:
        res["error"] = "ping timed out (partial/empty output ignored)"
        return res
    if rc not in (0, 1):
        res["error"] = (err or out).strip()[:200]
        return res
    m = _first_match(_PING_LOSS_RES, out)
    if not m:
        res["error"] = f"unparseable ping output: {(out + err).strip()[:200]}"
        return res
    res["sent"], res["recv"], res["loss_pct"] = int(m.group(1)), int(m.group(2)), float(m.group(3))
    m = _first_match(_PING_RTT_RES, out)
    if m:
        if m.re is _PING_RTT_RES[0]:  # busybox/GNU: min/avg/max
            a, b, c = m.group(1), m.group(2), m.group(3)
        else:  # Windows/German: min/max/avg
            a, b, c = m.group(1), m.group(3), m.group(2)
        res["rtt_min_ms"], res["rtt_avg_ms"], res["rtt_max_ms"] = float(a), float(b), float(c)
    res["ok"] = res["recv"] > 0
    return res


# --------------------------------------------------------------------------
# Traceroute
# --------------------------------------------------------------------------

# There are two incompatible "tracepath" implementations:
#   iproute2:  tracepath -n -T <host:port>     (TCP via -T)
#   iputils:   tracepath -n -p <port> <host>   (TCP via -p)
# Modern alpine ships the iputils one, so detect from the usage text.
_tracepath_style: str | None = None  # None=unchecked, ""=absent


def _detect_tracepath_style() -> str:
    global _tracepath_style
    if _tracepath_style is not None:
        return _tracepath_style
    if not shutil.which("tracepath"):
        _tracepath_style = ""
        return _tracepath_style
    rc, out, err = run_cmd(["tracepath", "--help"], 5.0)
    text = out + err
    if "-T" in text:
        _tracepath_style = "iproute2"
    else:
        _tracepath_style = "iputils"
    return _tracepath_style


def _pick_trace_args(host: str, port: int) -> list[str] | None:
    style = _detect_tracepath_style()
    if style == "iproute2":
        return ["tracepath", "-n", "-T", f"{host}:{port}"]
    if style == "iputils":
        return ["tracepath", "-n", "-p", str(port), host]
    if shutil.which("traceroute"):
        return ["traceroute", "-n", "-w", "2", host]
    if shutil.which("tracert"):
        return ["tracert", "-d", host]
    return None


_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV6_RE = re.compile(r"^[0-9A-Fa-f:]*:[0-9A-Fa-f:]+$")


def _is_ip(token: str) -> bool:
    return bool(_IPV4_RE.match(token) or _IPV6_RE.match(token))


def _parse_trace_line(line: str) -> dict | None:
    """Parse one hop line from tracepath / traceroute / tracert output."""
    line = line.strip()
    m = re.match(r"^(\d+):\s+(\S+)(?:\s+([\d.]+)ms)?\s*(.*)$", line)  # tracepath
    if m:
        n, addr, ms, extra = m.groups()
        if addr == "no":  # iputils "no reply" probe
            addr = "?"
        return {"n": int(n), "addr": addr, "ms": float(ms) if ms else None,
                "reached": "Reached" in extra}
    m = re.match(r"^(\d+)\s+(.*)$", line)  # traceroute / tracert
    if m:
        n, rest = int(m.group(1)), m.group(2)
        times = re.findall(r"([\d.]+) ms", rest)
        # All tools run numeric-only (-n / -d), so the hop's address is the
        # last IPv4/IPv6 token; "<number> ms" pairs are RTTs and "*"/locale
        # words (e.g. German "Anforderung.") mark silent hops.
        tokens = [t for t in rest.split() if _is_ip(t)]
        return {"n": n, "addr": tokens[-1] if tokens else "?",
                "ms": float(times[0]) if times else None, "reached": False}
    return None


def tcp_trace_test(host: str, port: int) -> dict:
    res = {"host": host, "port": port, "tool": None, "hops": [],
           "reached": False, "hops_count": 0, "ok": False, "error": None, "raw": ""}
    args = _pick_trace_args(host, port)
    if args is None:
        res["error"] = "no traceroute tool available (tracepath/traceroute/tracert)"
        return res
    res["tool"] = args[0]
    rc, out, err = run_cmd(args, TRACE_TIMEOUT)
    res["raw"] = (out + ("\n" + err if err else ""))[:4000]
    # Dedupe by hop number: tracepath prints one line per reply (two
    # replies on the first hop are common); prefer a line that has an
    # address over "no reply", and an RTT over "???".
    hops: dict[int, dict] = {}
    for line in out.splitlines():
        hop = _parse_trace_line(line)
        if not hop:
            continue
        hop["reached"] = hop["reached"] or hop["addr"] == host
        prev = hops.get(hop["n"])
        if prev is None:
            hops[hop["n"]] = hop
        elif prev["addr"] == "?" and hop["addr"] != "?":
            hops[hop["n"]] = hop
        else:
            prev["reached"] = prev["reached"] or hop["reached"]
            if prev["ms"] is None:
                prev["ms"] = hop["ms"]
    res["hops"] = [hops[k] for k in sorted(hops)]
    res["hops_count"] = max((h["n"] for h in res["hops"]), default=0)
    res["reached"] = any(h["addr"] == host for h in res["hops"])
    res["ok"] = res["reached"]
    if rc is None:
        res["error"] = "timed out (partial trace recorded)"
    elif res["hops_count"] == 0:
        res["error"] = f"no hops parsed (rc={rc}): {out.strip()[:200]}"
    return res
