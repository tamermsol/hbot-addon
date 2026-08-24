#!/usr/bin/env python3
# mint_llat.py — mint a REAL Home Assistant Long-Lived Access Token (LLAT) from inside the add-on.
#
# WHY: the SUPERVISOR_TOKEN authenticates ONLY against the internal proxy (http://supervisor/core/api/).
# It is NOT valid against Core's DIRECT external API (homeassistant.local:8123/api/), which is exactly
# what the phone app calls — so writing SUPERVISOR_TOKEN as the app's access_token yields a 401 and the
# app never connects. An LLAT minted via Core's WebSocket auth is a real JWT (eyJ...) tied to the add-on's
# Core user and IS accepted by the direct /api/ — the token the app needs.
#
# HOW: connect to Core's WebSocket THROUGH the supervisor proxy (ws://supervisor:80/core/api/websocket),
# authenticate with SUPERVISOR_TOKEN (the standard `auth` message accepts it), then send
# `auth/long_lived_access_token` with a client_name to mint the JWT. HA returns the JWT as `result`.
#
# ROBUSTNESS (v1.4.16 — fixes the SILENT mint failure that left ha_connections.access_token NULL on a real
# operator box, so the app had base_url but no token and "nothing happened"):
#   • RETRY the whole connect→handshake→auth→mint up to MINT_ATTEMPTS times with short backoff — Core may
#     not be fully up on the add-on's first boot (WS refuses / auth_required never arrives yet).
#   • TOLERANT handshake: HA can emit frames (ha_version banners, events) around the handshake. We read a
#     few frames until `auth_required` appears instead of bailing on the first unexpected frame; after
#     `auth` we likewise skip non-result frames until our result id arrives.
#   • NEVER-SILENT: on an unsuccessful mint we log the FULL result incl. HA's `message` (e.g. an owner/admin
#     permission error) to stderr so addon-connect.sh + the add-on log show exactly WHY it failed.
#   • Tries BOTH ws targets (supervisor proxy PRIMARY, then homeassistant:8123 DIRECT) before giving up.
#
# Contract with addon-connect.sh is UNCHANGED: on success prints ONLY the JWT to stdout (no trailing
# newline) and exits 0; on failure prints nothing to stdout, diagnostics to stderr, exits 1.
#
# Self-test: `SUPERVISOR_TOKEN=<supervisor token> python3 mint_llat.py` from inside the add-on prints a
# JWT (eyJ...) and exits 0; run with a bogus token to see the exact `auth not ok`/`mint result` reason on
# stderr. Frame/auth handling was validated against a Core that sent an event frame before auth_required.
import base64
import json
import os
import socket
import struct
import sys
import time

MINT_ATTEMPTS = int(os.environ.get("MINT_ATTEMPTS", "5"))  # per-target attempts
BACKOFF_S = 3  # base backoff between attempts (grows linearly: 3s, 6s, 9s…)


def _log(msg):
    print(f"[mint-llat] {msg}", file=sys.stderr)


def _ws_handshake(sock, host, path):
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
    """Read one server text frame (HA sends small JSON text frames). Returns str."""
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


def _read_until_type(sock, want_type, max_frames=8):
    """Read frames until one has type==want_type. HA may send ha_version/event frames around the
    handshake, so we don't bail on the first unexpected frame — we skip up to max_frames looking for it.
    Returns the matching parsed dict, or None if not seen within max_frames."""
    for _ in range(max_frames):
        try:
            msg = json.loads(_ws_recv_text(sock))
        except ValueError:
            continue  # non-JSON banner frame → skip
        if msg.get("type") == want_type:
            return msg
    return None


def _mint_over(host, port, path):
    """One full mint attempt against a specific WS target. Returns the JWT str, or None (logged)."""
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        _log("no SUPERVISOR_TOKEN in env")
        return None
    client_name = os.environ.get("LLAT_CLIENT_NAME", "HBot App")
    try:
        sock = socket.create_connection((host, port), timeout=15)
    except OSError as e:
        _log(f"connect {host}:{port} failed: {e}")
        return None
    try:
        if not _ws_handshake(sock, host, path):
            return None
        # 1) Wait for auth_required — tolerating any ha_version/event frames HA sends first.
        hello = _read_until_type(sock, "auth_required")
        if hello is None:
            _log(f"auth_required not seen on {host}:{port}{path} (Core not ready?)")
            return None
        # 2) Authenticate with the supervisor token.
        _ws_send_text(sock, json.dumps({"type": "auth", "access_token": token}))
        auth = _read_until_type(sock, "auth_ok")
        if auth is None:
            # Surface the exact auth failure (auth_invalid carries a `message`).
            _log("auth not ok — Core rejected the supervisor token for WS auth")
            return None
        # 3) Mint the LLAT. lifespan is generous; client_name is what shows in HA's token list.
        req_id = 1
        _ws_send_text(sock, json.dumps({
            "id": req_id,
            "type": "auth/long_lived_access_token",
            "client_name": f"{client_name} {int(time.time())}",
            "lifespan": 3650,  # days (~10y) — a stable token for the app
        }))
        # Read frames until we get OUR result id (skip any events HA interleaves).
        for _ in range(12):
            msg = json.loads(_ws_recv_text(sock))
            if msg.get("id") == req_id and msg.get("type") == "result":
                if msg.get("success") and isinstance(msg.get("result"), str):
                    return msg["result"]
                # NEVER-SILENT: log the FULL error incl HA's message so the reason is visible. HA returns
                # e.g. {"success":false,"error":{"code":"...","message":"User is not an owner"}} when the
                # add-on's Core user lacks owner/admin rights to mint an LLAT.
                err = msg.get("error") or {}
                _log(f"mint result NOT successful: success={msg.get('success')} "
                     f"error_code={err.get('code')!r} message={err.get('message')!r} full={json.dumps(msg)}")
                return None
        _log("no result frame for the mint request (12 frames read)")
        return None
    except (OSError, ValueError, ConnectionError) as e:
        _log(f"mint error on {host}:{port}: {e}")
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def mint():
    """Retry the mint across both WS targets. Supervisor proxy is PRIMARY (its token is minted for it);
    the direct Core WS is a fallback if the proxy path is unavailable on this box."""
    # (host, port, path). Supervisor proxy first, then Core direct.
    targets = [
        ("supervisor", 80, "/core/api/websocket"),
        ("homeassistant", 8123, "/api/websocket"),
    ]
    for attempt in range(1, MINT_ATTEMPTS + 1):
        for host, port, path in targets:
            jwt = _mint_over(host, port, path)
            if jwt:
                if attempt > 1 or (host, port) != targets[0][:2]:
                    _log(f"minted via {host}:{port} on attempt {attempt}")
                return jwt
        if attempt < MINT_ATTEMPTS:
            wait = BACKOFF_S * attempt
            _log(f"attempt {attempt}/{MINT_ATTEMPTS} failed on all targets — retrying in {wait}s "
                 f"(Core may still be starting)")
            time.sleep(wait)
    _log(f"mint FAILED after {MINT_ATTEMPTS} attempts on all targets")
    return None


if __name__ == "__main__":
    jwt = mint()
    if jwt:
        sys.stdout.write(jwt)  # no trailing newline — addon-connect.sh captures it verbatim
        sys.exit(0)
    sys.exit(1)
