"""Minecraft protocol tests: modern (1.13+) status ping with legacy 0xFE
fallback.

The modern ping speaks the real protocol: handshake (state 0 -> 1) followed
by a status request (state 1 -> 2). The reply is length-prefixed JSON with
version/players/MOTD. This is much more diagnostic than a blind TCP connect
- timeouts vs. connection resets vs. a slow but answered handshake each mean
different things.
"""

from __future__ import annotations

import json
import socket
import struct
import time

from config import MC_PROTOCOL_VERSION, TEST_TIMEOUT
from net_tools import _open_tcp


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        buf += chunk
    return bytes(buf)


def varint_encode(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def varint_decode(buf: bytes, off: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off
        shift += 7
        if shift >= 35:
            raise ValueError("varint too long")


def _read_mc_packet(sock: socket.socket) -> bytes:
    """Read one length-prefixed Minecraft packet body."""
    length, off = 0, 0
    while True:  # length itself is a varint
        b = recv_exact(sock, 1)[0]
        length |= (b & 0x7F) << off
        if not b & 0x80:
            break
        off += 7
    return recv_exact(sock, length)


def mc_modern_ping(host: str, port: int, protocol: int) -> dict:
    """1.13+ status ping: handshake + status request, parse the JSON reply."""
    res = {"connect_ms": None, "total_ms": None, "server_version": None,
           "players_online": None, "players_max": None, "motd": None,
           "ok": False, "error": None, "protocol": "modern"}
    try:
        s, conn_ms = _open_tcp(host, port, TEST_TIMEOUT)
    except OSError as e:
        res["error"] = f"connect failed: {e}"
        return res
    res["connect_ms"] = round(conn_ms, 1)
    t0 = time.perf_counter()
    try:
        # handshake: packet id 0x00, next state 2, server address, port,
        # protocol version.
        addr_bytes = host.encode("utf-8")
        handshake = bytes([0x00]) + varint_encode(2) + varint_encode(len(addr_bytes)) \
            + addr_bytes + struct.pack(">H", port) + varint_encode(protocol)
        s.sendall(varint_encode(len(handshake)) + handshake)
        # status request: packet id 0x00, empty body
        s.sendall(varint_encode(1) + b"\x00")
        packet = _read_mc_packet(s)
        _pid, off = varint_decode(packet, 0)  # packet id 0x00
        data_len, off = varint_decode(packet, off)  # json string length
        info = json.loads(packet[off:off + data_len].decode("utf-8", "replace"))
        ver = info.get("version") or {}
        res["server_version"] = ver.get("name")
        players = info.get("players") or {}
        res["players_online"] = players.get("online")
        res["players_max"] = players.get("max")
        motd = info.get("description")
        res["motd"] = motd.get("text") if isinstance(motd, dict) else str(motd)
        res["total_ms"] = round((time.perf_counter() - t0) * 1000.0 + res["connect_ms"], 1)
        res["ok"] = True
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as e:
        res["error"] = str(e)
    finally:
        s.close()
    return res


def mc_legacy_ping(host: str, port: int) -> dict:
    """Pre-1.13 0xFE legacy ping. Fallback for very old servers."""
    res = {"connect_ms": None, "total_ms": None, "server_version": None,
           "players_online": None, "players_max": None, "motd": None,
           "ok": False, "error": None, "protocol": "legacy"}
    try:
        s, conn_ms = _open_tcp(host, port, TEST_TIMEOUT)
    except OSError as e:
        res["error"] = f"connect failed: {e}"
        return res
    res["connect_ms"] = round(conn_ms, 1)
    t0 = time.perf_counter()
    try:
        s.sendall(b"\xfe\x00")
        data = b""
        try:
            while len(data) < 512:
                chunk = s.recv(512)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        if data[:1] == b"\xff":
            parts = data[1:].split(b"\x00")
            if len(parts) > 1:
                res["motd"] = parts[0].decode("utf-8", "replace")
            if len(parts) > 3:
                res["server_version"] = parts[1].decode("utf-8", "replace")
            if len(parts) > 5:
                try:
                    res["players_online"] = int(parts[4])
                    res["players_max"] = int(parts[5])
                except ValueError:
                    pass
        res["total_ms"] = round((time.perf_counter() - t0) * 1000.0 + res["connect_ms"], 1)
        res["ok"] = bool(data)
    except OSError as e:
        res["error"] = str(e)
    finally:
        s.close()
    return res


def mc_ping_test(host: str, port: int) -> dict:
    """Modern protocol first; fall back to legacy if the server is very old."""
    res = mc_modern_ping(host, port, MC_PROTOCOL_VERSION)
    if res["ok"]:
        return res
    legacy = mc_legacy_ping(host, port)
    if legacy["ok"]:
        return legacy
    # Keep the modern attempt's error; note what legacy saw too
    res["legacy_error"] = legacy.get("error")
    return res
