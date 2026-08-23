#!/usr/bin/env python3
# mint_llat.py — mint a REAL Home Assistant Long-Lived Access Token (LLAT) from inside the add-on.
#
# WHY: the SUPERVISOR_TOKEN authenticates ONLY against the internal proxy (http://supervisor/core/api/).
# It is NOT valid against Core's DIRECT external API (homeassistant.local:8123/api/), which is exactly
# what the phone app calls — so writing SUPERVISOR_TOKEN as the app's access_token yields a 401 and the
# app never connects. An LLAT minted via Core's WebSocket auth is a real JWT (eyJ...) tied to the add-on's
# Core user and IS accepted by the direct /api/ — the token the app needs.
#
# HOW: connect to Core's WebSocket THROUGH the supervisor proxy (ws://supervisor/core/api/websocket),
# authenticate with SUPERVISOR_TOKEN (the standard `auth` message accepts it), then send
# `auth/long_lived_access_token` with a client_name to mint the JWT. HA returns the JWT as `result`.
#
# No third-party websocket dependency: this speaks the tiny slice of RFC6455 we need (client handshake +
# masked text frames + read a single text frame) over a plain socket, using only the Python stdlib.
#
# Usage:  mint_llat.py            → prints the JWT to stdout on success, exits 0; prints nothing + exits 1
#                                    on any failure (addon-connect.sh then writes an EMPTY token).
# Env:    SUPERVISOR_TOKEN (required), LLAT_CLIENT_NAME (optional, default "HBot App").
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time


def _log(msg):
    print(f"[mint-llat] {msg}", file=sys.stderr)


def _ws_handshake(sock, host, path, token):
    """Perform the RFC6455 client handshake. Returns True on HTTP 101."""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    # Read headers until CRLFCRLF.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return False
        buf += chunk
        if len(buf) > 65536:
            return False
    status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status:
        _log(f"handshake failed: {status}")
        return False
    return True


def _ws_send_text(sock, text):
    """Send one masked text frame (client frames MUST be masked)."""
    payload = text.encode()
    header = bytearray([0x81])  # FIN + text opcode
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv_text(sock, timeout=15):
    """Read one server text frame (unmasked, unfragmented — HA sends small JSON frames). Returns str."""
    sock.settimeout(timeout)

    def _recv_exact(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    while True:
        b0, b1 = _recv_exact(2)
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(8))[0]
        mask = _recv_exact(4) if masked else b""
        payload = _recv_exact(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:  # close
            raise ConnectionError("server closed the websocket")
        if opcode in (0x1, 0x2):  # text / binary
            return payload.decode(errors="replace")
        # ping/pong/continuation → ignore and read the next frame


def mint():
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        _log("no SUPERVISOR_TOKEN in env")
        return None
    client_name = os.environ.get("LLAT_CLIENT_NAME", "HBot App")
    # The supervisor proxy resolves 'supervisor' to Core's API; the WS path is /core/api/websocket.
    host = "supervisor"
    path = "/core/api/websocket"
    try:
        sock = socket.create_connection((host, 80), timeout=15)
    except OSError as e:
        _log(f"connect failed: {e}")
        return None
    try:
        if not _ws_handshake(sock, host, path, token):
            return None
        # 1) Core sends {"type":"auth_required"}.
        hello = json.loads(_ws_recv_text(sock))
        if hello.get("type") != "auth_required":
            _log(f"unexpected first frame: {hello}")
            return None
        # 2) Authenticate with the supervisor token.
        _ws_send_text(sock, json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(_ws_recv_text(sock))
        if auth.get("type") != "auth_ok":
            _log(f"auth not ok: {auth}")
            return None
        # 3) Mint the LLAT. lifespan is generous; client_name is what shows in HA's token list.
        req_id = 1
        _ws_send_text(sock, json.dumps({
            "id": req_id,
            "type": "auth/long_lived_access_token",
            "client_name": f"{client_name} {int(time.time())}",
            "lifespan": 3650,  # days (~10y) — a stable token for the app
        }))
        # Read frames until we get our result id (skip any events).
        for _ in range(10):
            msg = json.loads(_ws_recv_text(sock))
            if msg.get("id") == req_id and msg.get("type") == "result":
                if msg.get("success") and isinstance(msg.get("result"), str):
                    return msg["result"]
                _log(f"mint result not successful: {msg}")
                return None
        _log("no result frame for the mint request")
        return None
    except (OSError, ValueError, ConnectionError) as e:
        _log(f"mint error: {e}")
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    jwt = mint()
    if jwt:
        sys.stdout.write(jwt)  # no trailing newline — addon-connect.sh captures it verbatim
        sys.exit(0)
    sys.exit(1)
