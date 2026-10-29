"""Minecraft protocol tests: modern (1.13+) status ping, pre-1.13
handshake-order variant, and legacy 0xFE fallback.

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


def _read_status_response(sock: socket.socket) -> dict:
    """Read one status response packet; extract version/players/motd.

    The reply is a length-prefixed JSON string with packet id 0x00, as
    sent by 1.13+ servers (and by front-ends that forward their status).
    """
    packet = _read_mc_packet(sock)
    _pid, off = varint_decode(packet, 0)  # packet id 0x00
    data_len, off = varint_decode(packet, off)  # json string length
    info = json.loads(packet[off:off + data_len].decode("utf-8", "replace"))
    # A real status payload has version and/or description; anything else
    # (e.g. a disconnect JSON) means the server did not enter status mode.
    if "version" not in info and "description" not in info:
        raise ValueError("reply is not a status response")
    ver = info.get("version") or {}
    players = info.get("players") or {}
    motd = info.get("description")
    return {"server_version": ver.get("name"),
            "players_online": players.get("online"),
            "players_max": players.get("max"),
            "motd": motd.get("text") if isinstance(motd, dict) else str(motd)}


def _new_ping_res(protocol: str) -> dict:
    return {"connect_ms": None, "total_ms": None, "server_version": None,
            "players_online": None, "players_max": None, "motd": None,
            "ok": False, "error": None, "protocol": protocol}


def _status_ping(host: str, port: int, protocol: str, handshake: bytes) -> dict:
    """Send one handshake + status request, parse the status JSON reply."""
    res = _new_ping_res(protocol)
    try:
        s, conn_ms = _open_tcp(host, port, TEST_TIMEOUT)
    except OSError as e:
        res["error"] = f"connect failed: {e}"
        return res
    res["connect_ms"] = round(conn_ms, 1)
    t0 = time.perf_counter()
    try:
        s.sendall(handshake)
        # status request: packet id 0x00, empty body
        s.sendall(varint_encode(1) + b"\x00")
        res.update(_read_status_response(s))
        res["total_ms"] = round((time.perf_counter() - t0) * 1000.0 + res["connect_ms"], 1)
        res["ok"] = True
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as e:
        res["error"] = str(e)
    finally:
        s.close()
    return res


def _modern_handshake(host: str, port: int, version: int) -> bytes:
    # packet id 0x00, next state 2 (status), server address, port,
    # protocol version.
    addr_bytes = host.encode("utf-8")
    body = bytes([0x00]) + varint_encode(2) + varint_encode(len(addr_bytes)) \
        + addr_bytes + struct.pack(">H", port) + varint_encode(version)
    return varint_encode(len(body)) + body


def _pre113_handshake(host: str, port: int, version: int) -> bytes:
    # Pre-1.13 field order: packet id 0x00, protocol version, address,
    # port, next state 1 (login). The follow-up empty packet 0x00 then
    # elicits a status reply from front-ends that parse this old order
    # (see mc_pre113_order_ping).
    addr_bytes = host.encode("utf-8")
    body = bytes([0x00]) + varint_encode(version) + varint_encode(len(addr_bytes)) \
        + addr_bytes + struct.pack(">H", port) + varint_encode(1)
    return varint_encode(len(body)) + body


def mc_modern_ping(host: str, port: int, protocol: int) -> dict:
    """1.13+ status ping: handshake (state 2) + status request."""
    return _status_ping(host, port, "modern", _modern_handshake(host, port, protocol))


def mc_pre113_order_ping(host: str, port: int, protocol: int) -> dict:
    """Status ping with the pre-1.13 handshake field order (protocol before
    next state).

    Some front-ends/proxies in front of modern servers parse the handshake
    in this old order and only answer status requests in this shape - the
    default target does; a standard 1.13+ order ping to it gets a clean
    close. This is the exact byte sequence Prism Launcher's server list
    sends, and the reply is a standard modern status JSON, so response
    parsing is shared with mc_modern_ping.
    """
    return _status_ping(host, port, "pre113_order", _pre113_handshake(host, port, protocol))


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
    """Modern 1.13+ order first, then the pre-1.13 field-order variant
    (required by some server front-ends), then 0xFE legacy for very old
    servers. Returns the first attempt that got a status reply."""
    res = mc_modern_ping(host, port, MC_PROTOCOL_VERSION)
    if res["ok"]:
        return res
    pre113 = mc_pre113_order_ping(host, port, MC_PROTOCOL_VERSION)
    if pre113["ok"]:
        return pre113
    legacy = mc_legacy_ping(host, port)
    if legacy["ok"]:
        return legacy
    # Keep the modern attempt's error; note what the fallbacks saw too
    res["fallback_errors"] = {"pre113_order": pre113.get("error"),
                              "legacy": legacy.get("error")}
    return res
