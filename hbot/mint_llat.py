#!/usr/bin/env python3
# mint_llat.py — obtain a REAL Home Assistant Long-Lived Access Token (LLAT) the phone app can use against
# Core's DIRECT api, FULLY AUTONOMOUSLY (zero operator typing) from inside the add-on.
#
# ═══ WHY THE OLD APPROACH WAS A DEAD END (source-proven) ═══
# The prior version authenticated the Core WebSocket with SUPERVISOR_TOKEN and called
# `auth/long_lived_access_token`. That WS runs as the Supervisor's Core user, which is created
# system_generated=True (core/components/hassio: async_create_system_user(HASSIO_USER_NAME,
# group_ids=[GROUP_ID_ADMIN])). Core's auth manager REFUSES an LLAT for a system user:
#   async_create_refresh_token: `if user.system_generated != (token_type == TOKEN_TYPE_SYSTEM): raise`
# → the WS command surfaces the generic {"code":"unknown_error"} we saw on the operator's box. An add-on
# can NEVER mint an LLAT as the supervisor identity. Confirmed against home-assistant/core dev.
#
# ═══ THE WORKING APPROACH — OPTION A: a DEDICATED NON-SYSTEM user + its LLAT ═══
# The Supervisor's Core user is system_generated BUT **admin** (group_ids=[GROUP_ID_ADMIN]). Admin is all
# that Core's user-admin WS commands require (each is @websocket_api.require_admin, which checks
# user.is_admin — the supervisor user passes). So over the supervisor proxy WS we:
#   1. config/auth/create {name, group_ids:[system-admin group]}  → creates a NORMAL (non-system) user.
#      (core/components/config/auth.py::websocket_create → async_create_user → NOT system_generated.)
#   2. config/auth_provider/homeassistant/create {user_id, username, password}  → attaches a login
#      credential (core/components/config/auth_provider_homeassistant.py::websocket_create →
#      provider.async_add_auth).
#   3. Log in AS THAT USER via the standard OAuth/IndieAuth login_flow (username+password) → an auth code
#      → POST /auth/token grant_type=authorization_code → a refresh_token + access_token for the NEW user.
#   4. Open a fresh Core WS, auth with that access_token (now we ARE the non-system user), and call
#      auth/long_lived_access_token → SUCCEEDS (the system-user guard does not apply). Return that JWT.
# A non-system user CAN own an LLAT — that is exactly why ours failed and this one works.
#
# IndieAuth note: login_flow verifies client_id/redirect_uri (core/components/auth/indieauth.py). It accepts
# same-scheme+same-domain client_id==redirect_uri, and _parse_client_id rejects IPv4 EXCEPT 127.0.0.1. So we
# drive the login flow against http://127.0.0.1:8123 with client_id==redirect_uri==that origin (the add-on
# runs host_network, so 127.0.0.1:8123 reaches Core). The MINTED LLAT is user-scoped, not origin-scoped, so
# it is valid on the app's own base_url/api/ afterwards.
#
# Idempotent: the created username/password are persisted at /data/{hbot_user,hbot_pass}. On re-run we reuse
# them (re-login + re-mint) instead of creating a second user. client_name for the LLAT is unique per mint
# (Core rejects a duplicate client_name), so re-mints just add a fresh token for the same user.
#
# Contract with addon-connect.sh is UNCHANGED: prints ONLY the JWT to stdout (no trailing newline), exit 0
# on success; nothing to stdout + diagnostics to stderr + exit 1 on failure.
#
# Self-test: `SUPERVISOR_TOKEN=<token> python3 mint_llat.py` inside the add-on prints eyJ... and exit 0; the
# created user "HBot App" appears under Settings → People (system-managed). Failures print the exact Core
# error (create/login/token/mint) to stderr, which addon-connect.sh records as mint_error server-side.
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MINT_ATTEMPTS = int(os.environ.get("MINT_ATTEMPTS", "5"))
BACKOFF_S = 3

USER_FILE = "/data/hbot_user"
PASS_FILE = "/data/hbot_pass"
HBOT_USER_NAME = "HBot App"  # display name in Settings → People


def _log(msg):
    print(f"[mint-llat] {msg}", file=sys.stderr)


# ── tiny RFC6455 client (unchanged primitives) ──────────────────────────────────────────────────────────
def _ws_handshake(sock, host, path):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
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


def _ws_send(sock, obj):
    payload = json.dumps(obj).encode()
    header = bytearray([0x81])
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


def _ws_recv(sock, timeout=15):
    sock.settimeout(timeout)

    def _exact(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    while True:
        b0, b1 = _exact(2)
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _exact(8))[0]
        mask = _exact(4) if masked else b""
        payload = _exact(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("server closed the websocket")
        if opcode in (0x1, 0x2):
            return json.loads(payload.decode(errors="replace"))


def _read_until_type(sock, want_type, max_frames=8):
    for _ in range(max_frames):
        try:
            msg = _ws_recv(sock)
        except ValueError:
            continue
        if msg.get("type") == want_type:
            return msg
    return None


class WS:
    """An authenticated Core WS session (admin, via the supervisor proxy). Sends commands with auto ids and
    returns the matching result frame (skipping events)."""

    def __init__(self, host, port, path, access_token):
        self.sock = socket.create_connection((host, port), timeout=15)
        self._id = 0
        if not _ws_handshake(self.sock, host, path):
            raise ConnectionError("ws handshake failed")
        if _read_until_type(self.sock, "auth_required") is None:
            raise ConnectionError("auth_required not seen")
        _ws_send(self.sock, {"type": "auth", "access_token": access_token})
        if _read_until_type(self.sock, "auth_ok") is None:
            raise ConnectionError("auth not ok")

    def cmd(self, obj):
        self._id += 1
        obj = {**obj, "id": self._id}
        _ws_send(self.sock, obj)
        for _ in range(12):
            msg = _ws_recv(self.sock)
            if msg.get("id") == self._id and msg.get("type") == "result":
                return msg
        raise ConnectionError(f"no result for {obj.get('type')}")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ── HTTP helper (login_flow + token exchange) ────────────────────────────────────────────────────────────
def _http(method, url, body=None, headers=None, timeout=15):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode(errors="replace")
            return r.getcode(), (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, {"_raw": txt}


def _load_or_make_creds():
    user = None
    pw = None
    try:
        if os.path.exists(USER_FILE):
            user = open(USER_FILE).read().strip() or None
        if os.path.exists(PASS_FILE):
            pw = open(PASS_FILE).read().strip() or None
    except OSError:
        pass
    if not user:
        user = "hbot_app_" + base64.b32encode(os.urandom(5)).decode().lower().rstrip("=")
    if not pw:
        pw = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    try:
        os.makedirs("/data", exist_ok=True)
        with open(USER_FILE, "w") as f:
            f.write(user)
        os.chmod(USER_FILE, 0o600)
        with open(PASS_FILE, "w") as f:
            f.write(pw)
        os.chmod(PASS_FILE, 0o600)
    except OSError:
        pass
    return user, pw


def _admin_group_id(ws):
    """Return Core's system admin group id (usually 'system-admin'). Read config/auth/list is admin-only;
    instead we use the well-known constant, falling back if a create rejects it."""
    return "system-admin"  # homeassistant.auth.const.GROUP_ID_ADMIN


def _ensure_user_and_credentials(ws, username, password):
    """Create (idempotently) a NON-system admin user + a homeassistant-provider login credential. Returns
    the user_id. Reuses an existing user with the same name if present (so re-runs don't pile up users)."""
    # Is a user with our name already present? (config/auth/list is admin-only; the supervisor user is admin.)
    listing = ws.cmd({"type": "config/auth/list"})
    uid = None
    if listing.get("success"):
        for u in listing.get("result", []):
            if u.get("name") == HBOT_USER_NAME and not u.get("system_generated"):
                uid = u.get("id")
                break
    if not uid:
        res = ws.cmd({"type": "config/auth/create", "name": HBOT_USER_NAME,
                      "group_ids": [_admin_group_id(ws)]})
        if not res.get("success"):
            raise RuntimeError(f"config/auth/create failed: {json.dumps(res.get('error'))}")
        uid = res["result"]["user"]["id"]
        _log(f"created non-system user {HBOT_USER_NAME} ({uid})")
    # (Re)attach the login credential. If it already exists Core errors; ignore that and proceed to login.
    cred = ws.cmd({"type": "config/auth_provider/homeassistant/create",
                   "user_id": uid, "username": username, "password": password})
    if not cred.get("success"):
        err = json.dumps(cred.get("error"))
        # A pre-existing credential for this username is fine — we still know the password (persisted).
        _log(f"credential create note (continuing, may already exist): {err}")
    return uid


def _login_and_get_access_token(origin, username, password):
    """Drive the OAuth/IndieAuth login_flow as the new user → authorization_code → access_token.
    origin MUST be a hostname/127.0.0.1 URL (IndieAuth rejects other IPv4). client_id==redirect_uri==origin
    (same scheme+domain → verify_redirect_uri passes)."""
    client_id = origin + "/"
    redirect_uri = origin + "/"
    # 1) start a login flow for the homeassistant provider.
    code, start = _http("POST", f"{origin}/auth/login_flow",
                        {"client_id": client_id, "redirect_uri": redirect_uri,
                         "handler": ["homeassistant", None]})
    if code != 200 or "flow_id" not in start:
        raise RuntimeError(f"login_flow start failed ({code}): {json.dumps(start)}")
    flow_id = start["flow_id"]
    # 2) submit credentials.
    code, step = _http("POST", f"{origin}/auth/login_flow/{flow_id}",
                       {"client_id": client_id, "username": username, "password": password})
    if code != 200:
        raise RuntimeError(f"login_flow step failed ({code}): {json.dumps(step)}")
    if step.get("type") != "create_entry" or not step.get("result"):
        raise RuntimeError(f"login did not create an entry: {json.dumps(step)}")
    auth_code = step["result"]
    # 3) exchange the auth code for tokens (form-encoded, per the OAuth token endpoint).
    form = f"client_id={urllib.parse.quote(client_id, safe='')}&grant_type=authorization_code&code={urllib.parse.quote(auth_code, safe='')}"
    req = urllib.request.Request(f"{origin}/auth/token", data=form.encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/auth/token failed ({e.code}): {e.read().decode(errors='replace')}")
    at = tok.get("access_token")
    if not at:
        raise RuntimeError(f"/auth/token returned no access_token: {json.dumps(tok)}")
    return at


def _mint_llat_as_user(origin_host, origin_port, user_access_token):
    """Open a Core WS AS THE NEW USER (its access token) and mint an LLAT — succeeds (non-system user)."""
    ws = WS(origin_host, origin_port, "/api/websocket", user_access_token)
    try:
        res = ws.cmd({"type": "auth/long_lived_access_token",
                      "client_name": f"HBot App {int(time.time())}", "lifespan": 3650})
        if res.get("success") and isinstance(res.get("result"), str):
            return res["result"]
        err = res.get("error") or {}
        raise RuntimeError(f"LLAT mint (as new user) failed: code={err.get('code')!r} "
                           f"message={err.get('message')!r} full={json.dumps(res)}")
    finally:
        ws.close()


def _attempt():
    sup = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not sup:
        _log("no SUPERVISOR_TOKEN in env")
        return None
    username, password = _load_or_make_creds()
    # 1+2) create the non-system user + credential over the supervisor proxy WS (admin identity).
    admin_ws = WS("supervisor", 80, "/core/api/websocket", sup)
    try:
        _ensure_user_and_credentials(admin_ws, username, password)
    finally:
        admin_ws.close()
    # 3) login AS that user against a hostname/127.0.0.1 origin (IndieAuth-legal) to get an access token.
    #    Try 127.0.0.1 first (host_network reaches Core), then the mDNS hostname.
    last_err = None
    for origin, mint_host, mint_port in (
        ("http://127.0.0.1:8123", "127.0.0.1", 8123),
        ("http://homeassistant.local:8123", "homeassistant.local", 8123),
    ):
        try:
            at = _login_and_get_access_token(origin, username, password)
            # 4) mint the LLAT as the new user over a WS to the SAME origin.
            return _mint_llat_as_user(mint_host, mint_port, at)
        except (OSError, ValueError, ConnectionError, RuntimeError, urllib.error.URLError) as e:
            last_err = e
            _log(f"origin {origin} failed: {e}")
            continue
    if last_err:
        raise last_err
    return None


def mint():
    for attempt in range(1, MINT_ATTEMPTS + 1):
        try:
            jwt = _attempt()
            if jwt:
                if attempt > 1:
                    _log(f"succeeded on attempt {attempt}")
                return jwt
        except (OSError, ValueError, ConnectionError, RuntimeError, urllib.error.URLError) as e:
            _log(f"attempt {attempt}/{MINT_ATTEMPTS} error: {e}")
        if attempt < MINT_ATTEMPTS:
            wait = BACKOFF_S * attempt
            _log(f"retrying in {wait}s (Core may still be starting)")
            time.sleep(wait)
    _log(f"FAILED to obtain a user LLAT after {MINT_ATTEMPTS} attempts")
    return None


if __name__ == "__main__":
    jwt = mint()
    if jwt:
        sys.stdout.write(jwt)
        sys.exit(0)
    sys.exit(1)
