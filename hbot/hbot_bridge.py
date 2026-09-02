#!/usr/bin/env python3
"""HBot bridge — brings H-Bot (Tasmota) devices into Home Assistant over the LAN.

It auto-discovers HBot (Tasmota) devices on your local network — no IPs to type — via
  • mDNS (`_tasmota._tcp` / `_http._tcp`), and
  • a subnet sweep that probes every host on the HA LAN with `Status 0` and keeps the ones
    that answer like an HBot device.
Any IPs you DO type in the add-on options are always included as a manual override.

For each discovered device IP it then:
  1. reads Tasmota `Status 0` over HTTP (http://<ip>/cm?cmnd=Status%200) to learn topic/channels/type,
  2. publishes HA MQTT discovery to HA's built-in Mosquitto so the entity appears automatically,
  3. subscribes to the HA command topics and relays them to the device's HTTP API (POWERn/Shutter*),
  4. polls each device and republishes state so HA stays in sync.

No cloud broker: everything runs on the local network via the device HTTP API + HA's own MQTT.
"""
import base64
import hashlib
import ipaddress
import json
import os
import select
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit
import requests
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
PREFIX = os.environ.get("HBOT_PREFIX", "homeassistant")
POLL = int(os.environ.get("HBOT_POLL", "10"))
# Manually-entered IPs are optional now — they're merged with auto-discovered ones.
MANUAL_DEVICES = [d.strip() for d in os.environ.get("HBOT_DEVICES", "").split(",") if d.strip()]
AUTODISCOVER = os.environ.get("HBOT_AUTODISCOVER", "true").lower() not in ("false", "0", "no")
# Optional explicit subnet(s) to sweep, e.g. "192.168.1.0/24". Empty = derive from HA's own IP.
SCAN_SUBNETS = [s.strip() for s in os.environ.get("HBOT_SUBNETS", "").split(",") if s.strip()]

# Supervisor API — used to auto-start the Mosquitto broker add-on when the bridge can't reach it.
# SUPERVISOR_TOKEN is injected into every add-on; hassio_api:true + hassio_role:manager (config.yaml)
# authorize the /addons/<slug>/start call. core_mosquitto is HA's official broker add-on slug.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
MOSQUITTO_SLUG = "core_mosquitto"


def ensure_broker_up():
    """Best-effort: ask the Supervisor to START the Mosquitto broker add-on. Called when the bridge
    has repeatedly failed to reach MQTT (Errno 111/113). Returns True if the start request was accepted.
    Never raises — a failure just means we keep retrying the plain connect. Idempotent (starting an
    already-running add-on is a no-op on the Supervisor side)."""
    if not SUPERVISOR_TOKEN:
        return False
    try:
        r = requests.post(
            f"http://supervisor/addons/{MOSQUITTO_SLUG}/start",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=15,
        )
        if r.status_code in (200, 400):  # 400 = already running / not-startable-right-now → treat as "tried"
            log(f"ensure_broker_up: requested Mosquitto broker start (HTTP {r.status_code}).")
            return r.status_code == 200
        log(f"ensure_broker_up: Supervisor returned HTTP {r.status_code} starting the broker.")
    except Exception as e:
        log(f"ensure_broker_up: could not reach Supervisor to start the broker ({e}).")
    return False

HTTP_TIMEOUT = 5
# (connect, read) timeouts for the sweep. A short CONNECT timeout is what keeps a /24 sweep fast
# even when many hosts silently drop SYNs (firewalled) instead of refusing — those would otherwise
# each block for the full timeout and make the sweep take ~20s.
PROBE_CONNECT_TIMEOUT = 0.6
PROBE_READ_TIMEOUT = 1.5
SCAN_WORKERS = 128
DEBUG = os.environ.get("HBOT_DEBUG", "false").lower() in ("true", "1", "yes")

# ── account scoping (Option 2) ──
# When the operator's H-Bot account is configured, discovery is restricted to devices REGISTERED
# to that account: we sign in to Supabase and pull the allow-list of topic_base + mac_address
# (owner_user_id = the account's uid), then only bridge LAN devices whose Status 0 topic/MAC matches.
SUPABASE_URL = os.environ.get("HBOT_SUPABASE_URL", "https://mvmvqycvorstsftcldzs.supabase.co").rstrip("/")
SUPABASE_ANON = os.environ.get(
    "HBOT_SUPABASE_ANON",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im12bXZxeWN2b3JzdHNmdGNsZHpzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTUwMDczNTQsImV4cCI6MjA3MDU4MzM1NH0.gA744wsXronSyRXHCS60DVcVqJ3_y0MgkqEhLsFxYDI",
)
ACCOUNT_EMAIL = os.environ.get("HBOT_ACCOUNT_EMAIL", "").strip()
ACCOUNT_PASSWORD = os.environ.get("HBOT_ACCOUNT_PASSWORD", "")
# Populated by fetch_account_allowlist(): sets of normalised topics + MACs owned by the account.
# None = no account configured (fall back to name-based 'hbot' matching); a set (even empty) = enforce.
ACCOUNT_TOPICS = None
ACCOUNT_MACS = None


def log(*a):
    print("[hbot]", *a, flush=True)


# ── Health endpoint for the Supervisor watchdog ─────────────────────────────
# The add-on declares `watchdog: tcp://[HOST]:[PORT:8099]` so the Supervisor polls this port; if the
# whole add-on ever wedges (process alive but not serving), the watchdog restarts it. A plain TCP
# accept loop is enough — the watchdog only checks that the port accepts a connection.
HEALTH_PORT = int(os.environ.get("HBOT_HEALTH_PORT", "8099"))

def _start_health_listener():
    def serve():
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", HEALTH_PORT))
            srv.listen(8)
            log(f"health listener up on :{HEALTH_PORT} (Supervisor watchdog target)")
            while True:
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            log(f"health listener could not bind :{HEALTH_PORT}: {e} (watchdog disabled, add-on continues)")
    threading.Thread(target=serve, name="health", daemon=True).start()


# ── Core API reverse-proxy (SUPERVISOR_TOKEN) ───────────────────────────────
# The H-Bot app hits HA as $baseUrl/api/…  Directly against Core that needs a real Core bearer (an
# LLAT), which the add-on's system user CANNOT mint (HA refuses — proven dead end). But the Supervisor
# hands every add-on a SUPERVISOR_TOKEN that IS accepted by Core THROUGH the internal proxy
# http://supervisor/core/api/ (developers.home-assistant.io/docs/add-ons/communication). So we stand up
# a tiny reverse-proxy on 8098 that the app (via the CF tunnel) points its baseUrl at:
#     app → 8098/api/…  →  http://supervisor/core/api/…  (+ Authorization: Bearer SUPERVISOR_TOKEN)
# The app therefore needs NO Core token at all. Covers GET (states, camera_proxy[_stream]) and POST
# (services/*); the camera stream is forwarded chunk-by-chunk so live video is not buffered.
PROXY_PORT = int(os.environ.get("HBOT_PROXY_PORT", "8098"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", os.environ.get("HASSIO_TOKEN", ""))
CORE_API = "http://supervisor/core/api"
# Hop-by-hop headers that must NOT be forwarded (RFC 7230 §6.1) plus content-length/host which we
# recompute, and authorization which we inject ourselves.
_STRIP_REQ_HEADERS = {"host", "authorization", "content-length", "connection",
                      "keep-alive", "proxy-authenticate", "proxy-authorization",
                      "te", "trailers", "transfer-encoding", "upgrade", "accept-encoding",
                      # X-Forwarded-* come from cloudflared; Core-via-supervisor doesn't need them and
                      # forwarding an untrusted proxy chain risks a 400 — drop them at the proxy edge.
                      "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "cf-connecting-ip"}
# RFC 6455 WebSocket GUID for the Sec-WebSocket-Accept handshake.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_STRIP_RESP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                       "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
                       "content-length"}


# HA Core's camera_proxy (snapshot) WORKS but is dead-slow cold (5–14s) and intermittently 500s/times
# out under load; camera_proxy_stream (native MJPEG) returns 0 BYTES on idle cameras. A bare
# single-attempt forward therefore paints "Camera offline" in the app on the first flaky frame. To make
# a reachable-but-slow camera ALWAYS deliver a frame, the proxy:
#   • retries snapshot fetches (3x, backoff) with a generous read timeout, and caches the last-good JPEG
#     per entity so a transient 500 serves the stale frame (200 + X-HBot-Cache: stale) instead of failing;
#   • synthesizes an MJPEG multipart stream from those cached/polled snapshots when the native stream
#     yields no bytes, so "LIVE" shows moving still-frames even on a dead native stream.
_SNAPSHOT_READ_TIMEOUT = 20      # cold frames can take 14s — don't cut them off
_SNAPSHOT_RETRIES = 3            # attempts on >=500 / raise
_SNAPSHOT_BACKOFF = (0.5, 1.0)   # sleeps between attempts (last value reused if more attempts)
_CACHE_MAX_BYTES = 1_000_000     # cap a single cached JPEG at ~1MB
_CACHE_MAX_ENTRIES = 32          # cap total distinct cameras cached
_SYNTH_FIRST_BYTES_TIMEOUT = 4.0 # native stream must produce bytes within this or we synthesize
_SYNTH_FPS_INTERVAL = 1.0        # synthesized-stream snapshot cadence (cameras can't produce faster)
_SYNTH_BOUNDARY = "hbotframe"    # multipart boundary for the synthesized MJPEG stream

_cam_cache = {}                  # entity_id -> jpeg bytes (last-good)
_cam_cache_lock = threading.Lock()


def _cam_entity_from_path(path):
    """Extract the camera entity_id from a camera_proxy[_stream] path.

    Paths look like /api/camera_proxy/camera.dev_room?token=… or
    /api/camera_proxy_stream/camera.dev_room?token=… — the entity is the segment after the
    camera_proxy(_stream)/ marker, up to the query string."""
    for marker in ("camera_proxy_stream/", "camera_proxy/"):
        i = path.find(marker)
        if i != -1:
            rest = path[i + len(marker):]
            return rest.split("?", 1)[0].split("/", 1)[0] or None
    return None


def _cache_put(entity, data):
    if not entity or not data or len(data) > _CACHE_MAX_BYTES:
        return
    with _cam_cache_lock:
        # Simple bound: if full and this is a new key, drop an arbitrary existing entry.
        if entity not in _cam_cache and len(_cam_cache) >= _CACHE_MAX_ENTRIES:
            _cam_cache.pop(next(iter(_cam_cache)), None)
        _cam_cache[entity] = data


def _cache_get(entity):
    if not entity:
        return None
    with _cam_cache_lock:
        return _cam_cache.get(entity)


def _fetch_snapshot(path, fwd_headers):
    """Fetch a single camera snapshot from Core with retry, caching the last-good JPEG per entity.

    Returns (status_code, content_bytes, from_cache: bool). On total failure with a cached frame,
    returns the cached bytes with status 200 (from_cache=True). On total failure with no cache,
    returns the last upstream (status, body) — or (502, b"") if the request never completed."""
    entity = _cam_entity_from_path(path)
    target = f"{CORE_API}{path[len('/api'):]}"
    last_status, last_body = 502, b""
    for attempt in range(_SNAPSHOT_RETRIES):
        try:
            r = requests.get(target, headers=fwd_headers, timeout=(5, _SNAPSHOT_READ_TIMEOUT))
            last_status = r.status_code
            if r.status_code < 500:
                last_body = r.content
                if 200 <= r.status_code < 300 and last_body:
                    _cache_put(entity, last_body)
                return r.status_code, last_body, False
            last_body = r.content[:200]  # keep only a snippet of an error body
        except Exception as e:
            log(f"proxy snapshot {path} attempt {attempt+1} error: {e}")
        if attempt < _SNAPSHOT_RETRIES - 1:
            time.sleep(_SNAPSHOT_BACKOFF[min(attempt, len(_SNAPSHOT_BACKOFF) - 1)])
    # All attempts failed (>=500 or raised). Serve the last-good cached frame if we have one.
    cached = _cache_get(entity)
    if cached:
        log(f"proxy snapshot {path} → all {_SNAPSHOT_RETRIES} attempts failed; serving cached frame "
            f"({len(cached)} bytes, X-HBot-Cache: stale)")
        return 200, cached, True
    return last_status, last_body, False


# ── Minimal RFC 6455 WebSocket frame codec (text/binary/close/ping/pong), no external deps ─────────
# HA's WS API speaks small text-JSON frames, so a tiny stdlib codec is enough and avoids adding an
# asyncio websockets dependency to the Alpine image. Used only by the Core proxy's /api/websocket path.
def _ws_read_frame(sock):
    """Read one WebSocket frame from `sock`. Returns (opcode, payload_bytes) or (None, None) on close/EOF.
    Handles masking (client→server frames are masked) and the 3 length encodings. Not fragmentation-
    aware beyond a single continuation-less frame — HA never fragments its control JSON."""
    try:
        hdr = _recv_exactly(sock, 2)
        if hdr is None:
            return None, None
        b0, b1 = hdr[0], hdr[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        ln = b1 & 0x7F
        if ln == 126:
            ext = _recv_exactly(sock, 2)
            if ext is None:
                return None, None
            ln = struct.unpack(">H", ext)[0]
        elif ln == 127:
            ext = _recv_exactly(sock, 8)
            if ext is None:
                return None, None
            ln = struct.unpack(">Q", ext)[0]
        mask = _recv_exactly(sock, 4) if masked else None
        payload = _recv_exactly(sock, ln) if ln else b""
        if payload is None:
            return None, None
        if masked and mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload
    except Exception:
        return None, None


def _ws_build_frame(opcode, payload, mask=False):
    """Build a WebSocket frame. mask=True (client role) is used for proxy→upstream; server→client is
    unmasked per RFC 6455."""
    b0 = 0x80 | (opcode & 0x0F)  # FIN + opcode
    ln = len(payload)
    if ln < 126:
        header = struct.pack(">BB", b0, (0x80 if mask else 0) | ln)
    elif ln < 65536:
        header = struct.pack(">BBH", b0, (0x80 if mask else 0) | 126, ln)
    else:
        header = struct.pack(">BBQ", b0, (0x80 if mask else 0) | 127, ln)
    if mask:
        mk = os.urandom(4)
        masked = bytes(payload[i] ^ mk[i % 4] for i in range(ln))
        return header + mk + masked
    return header + payload


def _recv_exactly(sock, n):
    """Read exactly n bytes from a blocking socket, or None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _ws_send_text(sock, obj, mask=False):
    sock.sendall(_ws_build_frame(0x1, json.dumps(obj).encode("utf-8"), mask=mask))


def _read_http_headers(sock):
    """Read an HTTP response header block (up to the blank line) byte-by-byte, so no WS frame bytes are
    consumed. Returns True on a 101 Switching Protocols, False otherwise."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        b = sock.recv(1)
        if not b:
            return False
        data.extend(b)
        if len(data) > 8192:
            return False
    first = data.split(b"\r\n", 1)[0]
    return b"101" in first


def _start_core_proxy():
    """Reverse-proxy /api/* → http://supervisor/core/api/* with the add-on's SUPERVISOR_TOKEN.

    Threaded so a long-lived camera_proxy_stream request never blocks state/service calls. Runs in a
    daemon thread; a bind failure logs and leaves the rest of the add-on (MQTT bridge) running."""
    from http.server import BaseHTTPRequestHandler
    try:
        from socketserver import ThreadingMixIn
        from http.server import HTTPServer

        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
    except Exception:  # pragma: no cover — 3.7+ always has these
        return

    if not SUPERVISOR_TOKEN:
        log("proxy: SUPERVISOR_TOKEN not present in env — Core proxy DISABLED "
            "(is homeassistant_api:true set in config.yaml?)")
        return

    class _ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence the default stderr access log; we log our own line
            pass

        def _forward(self, method):
            # Only proxy the HA API surface. Anything else is a 404 — the proxy is not a general
            # open relay to Core.
            if not self.path.startswith("/api"):
                self.send_error(404, "not a proxied path")
                return
            target = f"{CORE_API}{self.path[len('/api'):]}"  # /api/states → http://supervisor/core/api/states
            # Pass through the request body (POST services/*), if any.
            body = None
            clen = self.headers.get("Content-Length")
            if clen:
                try:
                    body = self.rfile.read(int(clen))
                except Exception:
                    body = None
            # Rebuild headers: drop hop-by-hop/auth/length, inject the SUPERVISOR bearer.
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in _STRIP_REQ_HEADERS}
            fwd_headers["Authorization"] = f"Bearer {SUPERVISOR_TOKEN}"

            # ── Camera reliability paths (see module-level _fetch_snapshot / _synth_stream) ──
            # A snapshot (camera_proxy/, NOT camera_proxy_stream) always gets retry+cache so a slow/
            # flaky camera returns a real frame (fresh or last-good) instead of "Camera offline".
            if method == "GET" and "camera_proxy_stream" not in self.path and "camera_proxy/" in self.path:
                self._forward_snapshot(fwd_headers)
                return
            # A native MJPEG stream that produces no first bytes → synthesize one from snapshots so LIVE
            # shows moving still-frames even when the camera's native stream is dead.
            if method == "GET" and "camera_proxy_stream" in self.path:
                self._forward_camera_stream(fwd_headers)
                return
            # stream=True so camera_proxy_stream (multipart/x-mixed-replace) is forwarded live, not
            # read fully into memory. A long read timeout keeps a slow stream alive.
            is_stream = "camera_proxy_stream" in self.path
            try:
                resp = requests.request(
                    method, target, headers=fwd_headers, data=body,
                    stream=True, timeout=(5, None if is_stream else 30))
            except Exception as e:
                log(f"proxy {method} {self.path} → upstream error: {e}")
                self.send_error(502, "upstream error")
                return
            try:
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in _STRIP_RESP_HEADERS:
                        self.send_header(k, v)
                # We stream the body ourselves with chunked transfer so we don't need Content-Length.
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    self.wfile.write(b"%X\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                log(f"proxy {method} {self.path} → {resp.status_code}")
            except (BrokenPipeError, ConnectionResetError):
                # Client (app) closed the camera stream — normal; don't spam the log.
                pass
            except Exception as e:
                log(f"proxy {method} {self.path} write error: {e}")
            finally:
                resp.close()

        # ── Camera snapshot: retry + last-good cache (never false-offline a reachable camera) ─────
        def _forward_snapshot(self, fwd_headers):
            status, body, from_cache = _fetch_snapshot(self.path, fwd_headers)
            try:
                self.send_response(status)
                ctype = "image/jpeg" if body[:2] == b"\xff\xd8" else "application/octet-stream"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if from_cache:
                    self.send_header("X-HBot-Cache", "stale")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                    self.wfile.flush()
                log(f"proxy GET {self.path} → {status} "
                    f"({len(body)} bytes{', cached' if from_cache else ''})")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"proxy snapshot {self.path} write error: {e}")

        # ── Camera stream: native MJPEG, or synthesize one from snapshots if native yields 0 bytes ─
        def _forward_camera_stream(self, fwd_headers):
            target = f"{CORE_API}{self.path[len('/api'):]}"
            resp = None
            try:
                # Read timeout = probe window: a native stream that sends no bytes within this raises
                # ReadTimeout (caught below) so we synthesize. A LIVE native stream sends its multipart
                # preamble/first frame well inside this window, then we forward with no further limit.
                resp = requests.get(target, headers=fwd_headers, stream=True,
                                    timeout=(5, _SYNTH_FIRST_BYTES_TIMEOUT + 1))
            except Exception as e:
                log(f"proxy stream {self.path} → native upstream error: {e}; synthesizing.")
                self._synth_stream(fwd_headers)
                return
            # Probe for the first native bytes within a short window. If none arrive (idle-camera
            # 0-byte case), abandon the native stream and synthesize from snapshots instead.
            first_chunk = None
            try:
                if resp.status_code < 400:
                    # A 5s socket read timeout on the native stream bounds the wait: a 0-byte idle
                    # stream raises ReadTimeout instead of blocking forever, so we fall to synth.
                    start = time.monotonic()
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            first_chunk = chunk
                            break
                        if time.monotonic() - start > _SYNTH_FIRST_BYTES_TIMEOUT:
                            break
            except Exception as e:
                log(f"proxy stream {self.path} native probe error: {e}")
            if not first_chunk:
                log(f"proxy stream {self.path} → native produced 0 bytes in "
                    f"{_SYNTH_FIRST_BYTES_TIMEOUT}s (status {resp.status_code if resp else '?'}); synthesizing MJPEG.")
                try:
                    resp.close()
                except Exception:
                    pass
                self._synth_stream(fwd_headers)
                return
            # Native stream is alive — forward it through, chunked, WITHOUT buffering: chain the probe's
            # first chunk with the continued live iterator (never materialize the stream into a list).
            from itertools import chain
            try:
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in _STRIP_RESP_HEADERS:
                        self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in chain([first_chunk], resp.iter_content(chunk_size=8192)):
                    if not chunk:
                        continue
                    self.wfile.write(b"%X\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                log(f"proxy stream {self.path} → native forwarded ({resp.status_code})")
            except (BrokenPipeError, ConnectionResetError):
                pass  # client closed the stream — normal
            except requests.exceptions.RequestException as e:
                # Mid-stream upstream stall/drop: end our chunked stream cleanly so the client can retry.
                log(f"proxy stream {self.path} native stalled mid-stream: {e}")
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
            except Exception as e:
                log(f"proxy stream {self.path} native write error: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        def _synth_stream(self, fwd_headers):
            """Generate a multipart/x-mixed-replace MJPEG stream by polling camera_proxy snapshots
            (reusing the retry+cache path) at ~1 fps. Keeps LIVE showing moving still-frames even when
            the camera's native MJPEG stream is dead. Runs until the client disconnects."""
            # Derive the snapshot path from the stream path.
            snap_path = self.path.replace("camera_proxy_stream", "camera_proxy", 1)
            try:
                self.send_response(200)
                self.send_header(
                    "Content-Type", f"multipart/x-mixed-replace; boundary={_SYNTH_BOUNDARY}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-HBot-Synth", "mjpeg")
                # No Content-Length / chunked: multipart/x-mixed-replace is a length-less byte stream.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
            except Exception as e:
                log(f"proxy synth {snap_path} header error: {e}")
                return
            entity = _cam_entity_from_path(snap_path)
            frames = 0

            def _emit(jpeg):
                """Write one MJPEG part. Returns False if the client has gone (caller stops)."""
                nonlocal frames
                try:
                    self.wfile.write(
                        b"--" + _SYNTH_BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n")
                    self.wfile.flush()
                    frames += 1
                    return True
                except (BrokenPipeError, ConnectionResetError):
                    return False
                except Exception as e:
                    log(f"proxy synth {snap_path} write error after {frames} frame(s): {e}")
                    return False

            # 1) Emit the last-good cached frame IMMEDIATELY so LIVE shows an image within ms — never
            #    make the client wait out a full retry window (up to ~20s on a dead box) for frame 1.
            last = _cache_get(entity)
            if last and not _emit(last):
                log(f"proxy synth {snap_path} → client closed after {frames} frame(s)")
                return

            # 2) Poll snapshots ~1 fps; each successful/cached fetch refreshes `last`. Re-emit the
            #    freshest frame every interval so the stream keeps moving even when fresh polls fail.
            while True:
                status, body, from_cache = _fetch_snapshot(snap_path, fwd_headers)
                if body and body[:2] == b"\xff\xd8":
                    last = body
                if last:
                    if not _emit(last):
                        log(f"proxy synth {snap_path} → client closed after {frames} frame(s)")
                        return
                time.sleep(_SYNTH_FPS_INTERVAL)

        # ── WebSocket bridge for /api/websocket (live state_changed) ──────────────────────────────
        # The app opens ws://$baseUrl/api/websocket and expects HA's auth handshake. In proxy mode the
        # app has NO Core token (it sends the sentinel `__hbot_proxy__`), so we CANNOT relay its auth to
        # Core. Instead the proxy: (1) completes the client handshake, (2) opens its OWN upstream WS to
        # http://supervisor/core/api/websocket and authenticates it with SUPERVISOR_TOKEN, (3) swallows
        # the client's auth frame and answers `auth_ok` itself, then (4) relays every other frame both
        # ways. The client thus gets a fully-authenticated live feed with no Core token.
        def _handle_ws(self):
            client = self.connection
            # 1. Complete the client-side WebSocket handshake.
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400, "missing Sec-WebSocket-Key")
                return
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            client.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")

            # 2. Open the upstream WS to Core through the supervisor proxy and authenticate it.
            up = None
            try:
                up_host = urlsplit(CORE_API).hostname or "supervisor"  # 'supervisor'
                up = socket.create_connection((up_host, 80), timeout=15)
                up_key = base64.b64encode(os.urandom(16)).decode()
                up.sendall(
                    ("GET /core/api/websocket HTTP/1.1\r\n"
                     f"Host: {up_host}\r\n"
                     "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {up_key}\r\n"
                     "Sec-WebSocket-Version: 13\r\n\r\n").encode())
                # Read + discard the upstream 101 handshake response headers.
                if not _read_http_headers(up):
                    raise RuntimeError("upstream WS handshake failed")
                # Upstream HA sends auth_required → reply with SUPERVISOR_TOKEN → expect auth_ok.
                op, pl = _ws_read_frame(up)
                if op != 0x1:
                    raise RuntimeError("upstream: no auth_required text frame")
                _ws_send_text(up, {"type": "auth", "access_token": SUPERVISOR_TOKEN}, mask=True)
                op, pl = _ws_read_frame(up)
                authed = op == 0x1 and json.loads(pl.decode()).get("type") == "auth_ok"
                if not authed:
                    raise RuntimeError(f"upstream auth rejected: {pl[:120] if pl else pl}")
            except Exception as e:
                log(f"proxy ws: upstream connect/auth failed: {e}")
                try:
                    client.sendall(_ws_build_frame(0x8, b""))  # close
                except Exception:
                    pass
                if up:
                    up.close()
                return

            # 3. Drive the client-side auth: send auth_required, swallow its auth frame, answer auth_ok.
            try:
                _ws_send_text(client, {"type": "auth_required", "ha_version": "proxy"})
                op, pl = _ws_read_frame(client)
                if op == 0x8 or op is None:
                    raise ConnectionError("client closed during auth")
                # We ignore whatever token the client sent (it's the sentinel) — upstream is already authed.
                _ws_send_text(client, {"type": "auth_ok", "ha_version": "proxy"})
            except Exception as e:
                log(f"proxy ws: client auth failed: {e}")
                up.close()
                return

            log("proxy ws: bridged /api/websocket (client↔Core, SUPERVISOR_TOKEN authed)")
            # 4. Relay frames both ways until either side closes. Re-frame each side (unmask client→up,
            #    mask up→client) so masking rules and length encodings are always correct.
            self.close_connection = True  # stop BaseHTTPRequestHandler from reusing this hijacked socket
            try:
                self._ws_relay(client, up)
            finally:
                try: up.close()
                except Exception: pass

        def _ws_relay(self, client, up):
            client.setblocking(True)
            up.setblocking(True)
            socks = [client, up]
            while True:
                r, _, x = select.select(socks, [], socks, 60)
                if x:
                    return
                if not r:
                    # 60s idle: send a ping upstream to keep the tunnel/socket warm.
                    try:
                        up.sendall(_ws_build_frame(0x9, b"", mask=True))
                    except Exception:
                        return
                    continue
                for s in r:
                    src_is_client = s is client
                    op, pl = _ws_read_frame(s)
                    if op is None:
                        return  # EOF on one side → tear down both
                    if op == 0x8:  # close
                        try:
                            (up if src_is_client else client).sendall(
                                _ws_build_frame(0x8, b"", mask=src_is_client))
                        except Exception:
                            pass
                        return
                    if op == 0x9:  # ping → pong back to the same side
                        try:
                            s.sendall(_ws_build_frame(0xA, pl or b"", mask=src_is_client))
                        except Exception:
                            return
                        continue
                    if op == 0xA:  # pong → ignore
                        continue
                    # text/binary/continuation: forward to the other side with correct masking.
                    dst = up if src_is_client else client
                    try:
                        dst.sendall(_ws_build_frame(op, pl or b"", mask=src_is_client))
                    except Exception:
                        return

        def _serve_pair_nonce(self):
            # Serve the LAN pairing nonce from the add-on's OWN port, INDEPENDENT of HA's /local static
            # mount. On a fresh HA install /local is never registered at boot (www/ didn't exist), so
            # <base>/local/hbot_pair_nonce.txt 404s and the app can't complete the 0-tap bind. This
            # fallback path lets the app read the exact same nonce straight from the add-on (which wrote
            # it to /data/pair_nonce), so pairing works even when /local is dead. No auth: the nonce is a
            # proof-of-LAN-possession token, not a secret — only a client on this LAN/tunnel can reach it,
            # which is exactly the co-location proof hbot-connect requires (identical to the /local file).
            try:
                with open("/data/pair_nonce", "r") as f:
                    nonce = f.read().strip()
            except Exception:
                nonce = ""
            _n = nonce or ""
            _is_hex_nonce = (32 <= len(_n) <= 128
                             and all(c in "0123456789abcdefABCDEF" for c in _n))
            if not _is_hex_nonce:
                # No live pairing in progress (already paired → nonce retired, or not written yet).
                self.send_error(404, "no pairing nonce")
                return
            body = nonce.encode("ascii")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                log(f"proxy GET /hbot_pair_nonce → 200 (served nonce fallback, /local-independent)")
            except Exception as e:
                log(f"proxy GET /hbot_pair_nonce write error: {e}")

        def do_GET(self):
            # Add-on-served pairing nonce (fallback for a dead HA /local mount — see _serve_pair_nonce).
            if self.path.split("?", 1)[0] == "/hbot_pair_nonce":
                self._serve_pair_nonce()
                return
            # WebSocket upgrade on /api/websocket → live-state bridge; everything else → HTTP proxy.
            if (self.path.startswith("/api/websocket")
                    and "websocket" in (self.headers.get("Upgrade", "").lower())):
                self._handle_ws()
                return
            self._forward("GET")

        def do_POST(self):
            self._forward("POST")

        def do_DELETE(self):
            self._forward("DELETE")

    def serve():
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), _ProxyHandler)
            log(f"Core API proxy up on :{PROXY_PORT} → {CORE_API} (SUPERVISOR_TOKEN injected)")
            srv.serve_forever()
        except Exception as e:
            log(f"proxy could not bind :{PROXY_PORT}: {e} (Core proxy disabled, add-on continues)")

    threading.Thread(target=serve, name="core-proxy", daemon=True).start()


# ── account scoping (Option 2) ──────────────────────────────────────────────
def _norm_mac(mac):
    """Normalise a MAC to lowercase hex with no separators, for reliable comparison."""
    return "".join(c for c in str(mac or "").lower() if c in "0123456789abcdef")


def _norm_topic(t):
    return str(t or "").strip().lower()


def fetch_account_allowlist():
    """Sign in to Supabase with the operator's H-Bot account and load the set of devices that
    belong to it (owner_user_id = the account uid). Populates ACCOUNT_TOPICS / ACCOUNT_MACS.

    Sets both to None if no account is configured (→ fall back to name-based matching). On auth or
    query failure it logs and leaves them None so discovery still works (name-based) rather than
    silently bridging nothing."""
    global ACCOUNT_TOPICS, ACCOUNT_MACS
    if not (ACCOUNT_EMAIL and ACCOUNT_PASSWORD):
        ACCOUNT_TOPICS = ACCOUNT_MACS = None
        return
    try:
        # 1) password sign-in → access token + user id
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
            json={"email": ACCOUNT_EMAIL, "password": ACCOUNT_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            log(f"account sign-in failed (HTTP {r.status_code}: {r.text[:120]}). "
                f"Check account_email/account_password. Falling back to name-based discovery.")
            ACCOUNT_TOPICS = ACCOUNT_MACS = None
            return
        auth = r.json()
        token = auth.get("access_token")
        uid = (auth.get("user") or {}).get("id")
        if not token or not uid:
            log("account sign-in returned no token/uid; falling back to name-based discovery.")
            ACCOUNT_TOPICS = ACCOUNT_MACS = None
            return
        # 2) devices owned by this account → topic_base + mac_address allow-list
        dr = requests.get(
            f"{SUPABASE_URL}/rest/v1/devices",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {token}"},
            params={
                "owner_user_id": f"eq.{uid}",
                "is_deleted": "eq.false",
                "select": "topic_base,mac_address,display_name",
            },
            timeout=HTTP_TIMEOUT,
        )
        if dr.status_code != 200:
            log(f"account device query failed (HTTP {dr.status_code}: {dr.text[:120]}); "
                f"falling back to name-based discovery.")
            ACCOUNT_TOPICS = ACCOUNT_MACS = None
            return
        rows = dr.json() or []
        ACCOUNT_TOPICS = {_norm_topic(d.get("topic_base")) for d in rows if d.get("topic_base")}
        ACCOUNT_MACS = {_norm_mac(d.get("mac_address")) for d in rows if d.get("mac_address")}
        log(f"account '{ACCOUNT_EMAIL}': {len(rows)} registered device(s) "
            f"({len(ACCOUNT_TOPICS)} topics, {len(ACCOUNT_MACS)} MACs) — discovery scoped to these.")
        if DEBUG:
            log(f"  account topics={sorted(ACCOUNT_TOPICS)} macs={sorted(ACCOUNT_MACS)}")
    except Exception as e:
        log(f"account allow-list error ({e}); falling back to name-based discovery.")
        ACCOUNT_TOPICS = ACCOUNT_MACS = None


def _account_configured():
    """True once fetch_account_allowlist has an allow-list to enforce (account sign-in succeeded)."""
    return ACCOUNT_TOPICS is not None or ACCOUNT_MACS is not None


def status_net_mac(status):
    """Tasmota reports the MAC in the top-level StatusNET.Mac block of a `Status 0` reply."""
    net = (status or {}).get("StatusNET") or {}
    return net.get("Mac") or ""


def _in_account(status):
    """True if this device's Status 0 identity (topic or MAC) is in the account allow-list.

    `status` is the FULL Status 0 reply (topic lives in status['Status'], MAC in status['StatusNET'])."""
    st = (status or {}).get("Status") or {}
    topic = _norm_topic(st.get("Topic"))
    if topic and ACCOUNT_TOPICS and topic in ACCOUNT_TOPICS:
        return True
    mac = _norm_mac(status_net_mac(status))
    if mac and ACCOUNT_MACS and mac in ACCOUNT_MACS:
        return True
    return False


# ── auto-discovery ──────────────────────────────────────────────────────────
# HBot devices identify themselves by name/topic starting with "hbot" (e.g. Hbot_2CH_ABC123,
# Hbot_shutter_ABC123, hbot_F24188). We match on that so discovery picks the operator's H-Bot
# devices specifically and ignores unrelated Tasmota gear on the same LAN. Set HBOT_MATCH_ANY=true
# to fall back to accepting ANY Tasmota device (the old lenient behaviour) if a device is named
# oddly and isn't being found.
MATCH_ANY_TASMOTA = os.environ.get("HBOT_MATCH_ANY", "false").lower() in ("true", "1", "yes")


def _is_hbot_named(st):
    """True if any of the device's identity fields start with 'hbot' (case-insensitive)."""
    fields = [st.get("Topic"), st.get("DeviceName"), st.get("Hostname")]
    fn = st.get("FriendlyName")
    if isinstance(fn, list):
        fields.extend(fn)
    elif fn:
        fields.append(fn)
    return any(str(v).strip().lower().startswith("hbot") for v in fields if v)


def _looks_like_hbot(status):
    """True if a `Status 0` JSON reply is a device we should bridge.

    A Tasmota `Status 0` reply always has a top-level "Status" object with a non-empty "Topic".
    Selection rule, in order:
      • account configured  → accept ONLY devices in the account allow-list (topic OR MAC match)
                               [Option 2 — devices registered to the operator's H-Bot account];
      • HBOT_MATCH_ANY=true → accept any Tasmota device;
      • otherwise           → accept HBot-NAMED devices (topic/name starts with 'hbot')."""
    if not isinstance(status, dict):
        return False
    st = status.get("Status")
    if not isinstance(st, dict):
        return False
    topic = str(st.get("Topic") or "")
    if not topic:
        return False
    is_tasmota = any(k in st for k in ("FriendlyName", "Module", "DeviceName", "Power"))
    if not is_tasmota:
        return False
    if _account_configured():
        return _in_account(status)          # strict: only this account's registered devices
    if MATCH_ANY_TASMOTA:
        return True
    return _is_hbot_named(st)


def _probe_ip(ip):
    """Cheap reachability + identity probe for the subnet sweep. Returns ip if it's an HBot, else None.

    In HBOT_DEBUG mode, logs every host that answers on :80 and why it was accepted/rejected —
    use it to find where your device actually is when discovery comes up empty."""
    url = f"http://{ip}/cm?cmnd={quote('Status 0', safe='')}"
    try:
        r = requests.get(url, timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT))
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                if DEBUG:
                    log(f"  {ip}:80 answered but not JSON (not an HBot): {r.text[:80]!r}")
                return None
            if _looks_like_hbot(body):
                return ip
            if DEBUG:
                st = (body.get("Status") or {}) if isinstance(body, dict) else {}
                tp = st.get("Topic")
                if tp and _account_configured():
                    log(f"  {ip}:80 is Tasmota (topic={tp}, mac={status_net_mac(body)}) "
                        f"but NOT registered to this account — skipped")
                elif tp:
                    log(f"  {ip}:80 is Tasmota (topic={tp}) but name doesn't start with 'hbot' — skipped")
                else:
                    log(f"  {ip}:80 answered JSON but not a Tasmota Status 0 reply")
        elif DEBUG:
            log(f"  {ip}:80 HTTP {r.status_code} (not an HBot)")
    except Exception as e:
        if DEBUG:
            # Only interesting when the host actually refused/exists — skip pure timeouts.
            msg = str(e)
            if "refused" in msg.lower() or "reset" in msg.lower():
                log(f"  {ip}:80 up but refused/reset (not serving HTTP)")
    return None


# Docker/container bridge ranges that never host real devices. Tasmota devices live on the
# home LAN — almost always 192.168.x or 10.x — so we DESELECT the 172.16/12 docker space and
# only fall back to it if nothing better is found.
_DOCKER_NETS = [ipaddress.ip_network(n) for n in ("172.16.0.0/12",)]


def _is_lan_ipv4(ip):
    """True for a private LAN IPv4 we should sweep — excludes loopback and link-local."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local


def _is_dockerish(ip):
    """True if the IP is in the Docker/HA-supervisor bridge space (172.16/12) — likely NOT the LAN."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(a in n for n in _DOCKER_NETS)


def _all_host_ipv4s():
    """Every IPv4 the container/host has, across ALL interfaces (not just the default route).

    In a HA add-on the default-route interface is usually the internal Docker net, so the old
    connect-to-8.8.8.8 trick derived the WRONG subnet and the sweep found nothing. Read the real
    interface addresses from /proc/net/fib_trie (works even without `ip`/`ifconfig` in the image),
    with getaddrinfo(hostname) as a fallback.
    """
    ips = set()
    # 1) /proc/net/fib_trie lists every locally-configured address (the /32 "host" leaves).
    try:
        with open("/proc/net/fib_trie") as f:
            lines = f.read().splitlines()
        for i, ln in enumerate(lines):
            ln = ln.strip()
            if ln.startswith("|--") and i + 1 < len(lines) and "host LOCAL" in lines[i + 1]:
                ips.add(ln.split("|--")[1].strip())
    except Exception:
        pass
    # 2) fallback: resolve our own hostname.
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except Exception:
        pass
    # 3) last-ditch: default-route IP (may be the docker net, filtered out below if so).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


def _default_route_ip():
    """IP of the interface that reaches the internet/LAN gateway. With host_network:true this is
    the host's real LAN interface — the single most reliable signal for where devices are."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _local_subnets():
    """Derive /24 subnet(s) to sweep, PREFERRING the real LAN over Docker bridges.

    Order of preference so we sweep the right net first (and usually only it):
      1. the default-route interface's /24 (the LAN when host_network:true),
      2. any other non-docker private /24 (192.168.x, 10.x),
      3. docker-ish 172.16/12 /24s only as a last resort.
    """
    all_ips = _all_host_ipv4s()
    lan_ips = [ip for ip in all_ips if _is_lan_ipv4(ip)]

    ordered = []
    dr = _default_route_ip()
    if dr and _is_lan_ipv4(dr):
        ordered.append(dr)                                   # 1) default-route LAN IP first
    ordered += [ip for ip in sorted(lan_ips) if not _is_dockerish(ip) and ip not in ordered]  # 2)
    ordered += [ip for ip in sorted(lan_ips) if _is_dockerish(ip) and ip not in ordered]      # 3)

    subnets = []
    for ip in ordered:
        net = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        if net not in subnets:
            subnets.append(net)

    # If we have a real (non-docker) LAN subnet, don't waste time sweeping docker bridges.
    non_docker = [n for n in subnets if not _is_dockerish(n.split("/")[0])]
    chosen = non_docker or subnets

    if chosen:
        log(f"LAN subnet(s) to sweep: {chosen}"
            + (" (docker bridges skipped)" if non_docker and len(subnets) > len(non_docker) else ""))
    else:
        log("could not derive a LAN subnet — set 'subnets' in the add-on options "
            "(e.g. 192.168.1.0/24) so discovery knows where to look.")
    return chosen


def discover_mdns(timeout=4):
    """Find Tasmota devices advertised over mDNS. Returns a set of IPs. Best-effort — must NEVER raise:
    Zeroconf()/ServiceBrowser can throw when the add-on container can't bind the mDNS multicast
    socket, and that used to crash the whole add-on ~20s in. Every failure here → empty set."""
    ips = set()
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except Exception:
        return ips  # zeroconf not installed / unavailable — the subnet sweep covers us

    found = []

    class _L:
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=2000)
                if info:
                    for addr in info.parsed_addresses():
                        if ":" not in addr:  # IPv4 only
                            found.append(addr)
            except Exception:
                pass

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, ["_tasmota._tcp.local.", "_http._tcp.local."], _L())
        time.sleep(timeout)
    except Exception as e:
        log(f"mDNS unavailable ({e}); relying on subnet sweep.")
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
    ips.update(a for a in found if _is_lan_ipv4(a))
    return ips


def discover_subnet():
    """Sweep the local subnet(s) with a quick Status 0 probe. Returns a set of HBot IPs."""
    hosts = []
    for cidr in (SCAN_SUBNETS or _local_subnets()):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            log(f"ignoring invalid subnet '{cidr}'")
            continue
        # Cap sweeps to /24-sized ranges so we never scan the whole internet by mistake.
        if net.num_addresses > 512:
            log(f"subnet {cidr} too large ({net.num_addresses} hosts) — limiting to first 254")
            hosts.extend(str(h) for h in list(net.hosts())[:254])
        else:
            hosts.extend(str(h) for h in net.hosts())
    if not hosts:
        return set()
    log(f"sweeping {len(hosts)} host(s) for HBot devices "
        f"(set HBOT_DEBUG=true in options to see per-host results) …")
    found = set()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for ip in ex.map(_probe_ip, hosts):
            if ip:
                found.add(ip)
    if not found:
        log("sweep finished: no HBot device answered Status 0 on port 80 in this subnet. "
            "If your device is here, check it's powered on and its web UI opens at http://<its-ip>/ ; "
            "otherwise set 'subnets' to the device's network or add its IP under 'devices'.")
    return found


def discover_devices():
    """Return IPs that are CONFIRMED HBot devices, plus any manually-listed IPs.

    Every auto-discovered candidate (mDNS OR subnet sweep) is verified with `_probe_ip` before
    it's returned, so non-HBot hosts on your LAN — printers, NAS, phones that also answer/advertise
    HTTP — are dropped silently here instead of producing scary 'Connection refused' logs later.
    Manually-listed IPs are always included (they may be a real HBot that's briefly offline).

    Wrapped so a failure in either method can never crash the add-on."""
    confirmed = set(MANUAL_DEVICES)  # manual IPs are trusted; they retry if offline
    if AUTODISCOVER:
        candidates = set()
        try:
            m = discover_mdns()
            if m:
                log(f"mDNS advertised {len(m)} HTTP host(s); verifying which are HBot …")
            candidates |= m
        except Exception as e:
            log(f"mDNS discovery error (continuing): {e}")
        try:
            # The subnet sweep already returns only verified HBot IPs — take them as-is.
            confirmed |= discover_subnet()
        except Exception as e:
            log(f"subnet sweep error (continuing): {e}")
        # Verify mDNS candidates (minus ones the sweep already confirmed) so non-HBot hosts drop out.
        to_check = [ip for ip in candidates if ip not in confirmed]
        if to_check:
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                for ip in ex.map(_probe_ip, to_check):
                    if ip:
                        confirmed.add(ip)
    found = sorted(confirmed)
    if found:
        log(f"confirmed HBot device(s): {found}")
    return found


def tasmota(ip, cmnd):
    """Run a Tasmota command over the device's local HTTP API. Returns parsed JSON or None.

    IMPORTANT: encode the command with %20 for spaces (NOT '+'). Tasmota's /cm parser treats '+'
    literally, so `Status+0` fails — requests' default param encoding uses '+', which silently breaks
    device reads. Build the query with urllib.parse.quote so 'Status 0' → 'Status%200'.
    """
    url = f"http://{ip}/cm?cmnd={quote(cmnd, safe='')}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        log(f"{ip}: HTTP {r.status_code} for '{cmnd}'")
    except Exception as e:
        log(f"{ip}: HTTP error for '{cmnd}': {e}")
    return None


def detect_channels(status):
    """Channel count from Status 0 → Status.Power bit-string length (authoritative), else FriendlyName."""
    st = (status or {}).get("Status", {})
    power = st.get("Power")
    if isinstance(power, str) and power and all(c in "01" for c in power):
        return len(power)
    fn = st.get("FriendlyName")
    if isinstance(fn, list):
        n = len([x for x in fn if str(x).strip()])
        if n:
            return n
    return 1


def is_shutter(status):
    sns = (status or {}).get("StatusSNS", {})
    return isinstance(sns, dict) and any(k.startswith("Shutter") for k in sns)


class Device:
    def __init__(self, ip):
        self.ip = ip
        self.topic = None
        self.name = f"H-Bot {ip}"
        self.channels = 1
        self.shutter = False

    def probe(self):
        """Read identity from the device. Returns True once we have a topic."""
        s = tasmota(self.ip, "Status 0")
        if not s:
            return False
        st = s.get("Status", {})
        self.topic = (st.get("Topic") or "").strip() or self.topic
        self.name = (st.get("DeviceName") or self.name).strip()
        self.shutter = is_shutter(s)
        self.channels = 1 if self.shutter else detect_channels(s)
        return bool(self.topic)


class Bridge:
    def __init__(self):
        # paho-mqtt 2.x requires an explicit callback API version; 1.x doesn't have the kwarg. Build
        # the client so BOTH work — otherwise on 2.x the callbacks silently mismatch and the command
        # subscription never delivers (device shows in HA but can't be controlled).
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hbot-addon")
            self._v2 = True
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id="hbot-addon")
            self._v2 = False
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.devices = {}          # ip -> Device
        self.cmd_map = {}          # command_topic -> (ip, kind, index)
        self._connected_once = False   # True after the first successful MQTT connect
        self._need_rediscover = False  # set on a RE-connect so the main loop re-runs discovery
        # Runtime broker-drop recovery (v1.4.8): after loop_start(), paho owns reconnects and the initial
        # connect loop's ensure_broker_up() never runs again. Track live-connection state + when the drop
        # began so the main loop can auto-start a broker that STOPS AT RUNTIME (not just at startup).
        self._connected = False        # True between on_connect and on_disconnect
        self._disconnected_since = None # monotonic ts of the current sustained disconnect, else None
        self._last_broker_kick = 0.0    # monotonic ts of the last ensure_broker_up() during a drop

    # ── discovery ──
    def announce(self, d: Device):
        base = d.topic  # topic is already unique (e.g. hbot_FAKE01) — don't double-prefix
        dev_block = {
            "identifiers": [base],
            "name": d.name,
            "manufacturer": "H-Bot",
            "model": ("Shutter" if d.shutter else f"{d.channels}-channel"),
        }
        if d.shutter:
            obj = f"{base}_cover"
            cfg_topic = f"{PREFIX}/cover/{obj}/config"
            cmd_topic = f"hbot/{base}/cover/set"
            pos_cmd = f"hbot/{base}/cover/pos"
            self.client.publish(cfg_topic, json.dumps({
                "name": d.name, "unique_id": obj,
                "command_topic": cmd_topic,
                "set_position_topic": pos_cmd,
                "position_topic": f"hbot/{base}/cover/state",
                "position_open": 100, "position_closed": 0,
                "payload_open": "OPEN", "payload_close": "CLOSE", "payload_stop": "STOP",
                "device": dev_block,
            }), qos=1, retain=True)
            self.cmd_map[cmd_topic] = (d.ip, "cover", 1)
            self.cmd_map[pos_cmd] = (d.ip, "cover_pos", 1)
        else:
            for i in range(1, d.channels + 1):
                obj = f"{base}_{i}"
                cfg_topic = f"{PREFIX}/switch/{obj}/config"
                cmd_topic = f"hbot/{base}/{i}/set"
                self.client.publish(cfg_topic, json.dumps({
                    "name": (d.name if d.channels == 1 else f"{d.name} Channel {i}"),
                    "unique_id": obj,
                    "command_topic": cmd_topic,
                    "state_topic": f"hbot/{base}/{i}/state",
                    "payload_on": "ON", "payload_off": "OFF",
                    # Never let HA render an optimistic guess — it waits for the state topic, which the
                    # command relay now publishes immediately. Together these stop the toggle bounce.
                    "optimistic": False, "qos": 1,
                    "device": dev_block,
                }), qos=1, retain=True)
                self.cmd_map[cmd_topic] = (d.ip, "power", i)
        # subscribe to this device's command topics
        for t in list(self.cmd_map):
            self.client.subscribe(t)
        log(f"announced {d.name} ({'shutter' if d.shutter else str(d.channels)+'ch'}) topic={d.topic}")

    # ── command relay: HA → device HTTP ──
    def _on_message(self, _c, _u, msg):
        entry = self.cmd_map.get(msg.topic)
        if not entry:
            return
        ip, kind, idx = entry
        payload = msg.payload.decode(errors="ignore").strip()
        log(f"command {msg.topic} = {payload} → {ip} ({kind}{idx})")
        d = self.devices.get(ip)
        base = d.topic if d else None
        if kind == "power":
            # Tasmota's /cm reply to a POWER command echoes the resulting relay state as
            # {"POWERn":"ON"} — publish it straight back to the state topic so HA confirms the
            # toggle IMMEDIATELY. Without this, HA had only the STALE retained state until the next
            # ~POLL-second poll, so the dashboard toggle flipped back then settled (the on→off→on
            # bounce). Republishing on-command removes that gap entirely.
            res = tasmota(ip, f"POWER{idx} {payload}")  # ON / OFF
            if base:
                val = None
                if isinstance(res, dict):
                    val = res.get(f"POWER{idx}") or (res.get("POWER") if (d and d.channels == 1) else None)
                # Fall back to the requested payload if the device didn't echo a parseable state.
                if val not in ("ON", "OFF"):
                    val = payload.upper() if payload.upper() in ("ON", "OFF") else None
                if val in ("ON", "OFF"):
                    self.client.publish(f"hbot/{base}/{idx}/state", val, retain=True)
        elif kind == "cover":
            cmd = {"OPEN": "ShutterOpen1", "CLOSE": "ShutterClose1", "STOP": "ShutterStop1"}.get(payload.upper())
            if cmd:
                tasmota(ip, cmd)
        elif kind == "cover_pos":
            try:
                tasmota(ip, f"ShutterPosition1 {int(float(payload))}")
            except ValueError:
                pass

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_args):
        # Signature covers paho 1.x (client,userdata,flags,rc) AND 2.x (…,reason_code,properties).
        log(f"connected to MQTT (rc={reason_code})")
        # Live again → clear the runtime-drop recovery state so the monitor stops kicking the broker.
        self._connected = True
        self._disconnected_since = None
        # Re-announce + re-subscribe on every (re)connect so command topics are always live.
        for d in self.devices.values():
            self.announce(d)
        # If this is a RE-connect (broker went down→up), flag the main loop to re-run discovery so any
        # devices missed while the broker was unreachable are picked up + re-announced immediately —
        # otherwise they'd sit "unavailable" in HA until the ~2min rediscover tick.
        if self._connected_once:
            self._need_rediscover = True
            log("MQTT reconnected — scheduling device rediscovery so nothing stays unavailable.")
        self._connected_once = True

    def _on_disconnect(self, _client, _userdata, *args):
        # paho 1.x: (client,userdata,rc); 2.x: (client,userdata,disconnect_flags,reason_code,properties).
        # Records the RUNTIME drop so the main-loop monitor can auto-start a broker that stopped after
        # startup (the initial connect loop's ensure_broker_up never runs again once loop_start owns
        # reconnects). paho keeps trying to reconnect in the background; we only ASSIST after a grace.
        rc = args[-1] if args else "?"
        self._connected = False
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
        log(f"disconnected from MQTT (rc={rc}) — paho will retry; monitoring for a sustained broker drop.")

    # Called every main-loop tick: if we've been disconnected past the grace, ask the Supervisor to
    # (re)start the broker, rate-limited by capped backoff. Stops as soon as _on_connect fires again.
    # Idempotent + non-fatal. Grace/backoff are short so runtime recovery is quick but not chatty.
    def _broker_drop_monitor(self, grace=40, min_kick_interval=45):
        if self._connected or self._disconnected_since is None:
            return
        down_for = time.monotonic() - self._disconnected_since
        if down_for < grace:
            return  # brief blip — let paho self-recover without kicking the broker
        now = time.monotonic()
        if now - self._last_broker_kick < min_kick_interval:
            return  # capped backoff — don't hammer the Supervisor
        self._last_broker_kick = now
        log(f"MQTT down for ~{int(down_for)}s (>{grace}s) — asking Supervisor to (re)start the broker.")
        ensure_broker_up()

    # ── state polling: device HTTP → HA ──
    def poll_once(self, d: Device):
        base = d.topic
        if d.shutter:
            s = tasmota(d.ip, "Status 10") or {}
            sh = (s.get("StatusSNS", {}) or {}).get("Shutter1", {})
            pos = sh.get("Position")
            if pos is not None:
                self.client.publish(f"hbot/{base}/cover/state", str(pos), retain=True)
        else:
            s = tasmota(d.ip, "Status 11") or {}
            sts = s.get("StatusSTS", {}) or {}
            for i in range(1, d.channels + 1):
                key = "POWER" if d.channels == 1 else f"POWER{i}"
                val = sts.get(key) or sts.get(f"POWER{i}")
                if val in ("ON", "OFF"):
                    self.client.publish(f"hbot/{base}/{i}/state", val, retain=True)

    def add_device(self, ip):
        """Probe a newly-seen IP and, if it's a real HBot, register + announce it. Returns True if added."""
        if ip in self.devices:
            return False
        d = Device(ip)
        if d.probe():
            self.devices[ip] = d
            self.announce(d)
            log(f"{ip}: read OK → topic={d.topic} name='{d.name}' "
                f"{'shutter' if d.shutter else str(d.channels)+'ch'}")
            return True
        return False

    def run(self):
        # Bring the watchdog health port up first thing, before any slow discovery/MQTT work, so the
        # Supervisor sees the add-on as healthy from the very start.
        _start_health_listener()
        # Stand up the Core API reverse-proxy so the app reaches Core through the add-on's
        # SUPERVISOR_TOKEN (no Core LLAT needed). This is what the CF tunnel exposes.
        _start_core_proxy()
        log(f"config: manual={MANUAL_DEVICES} autodiscover={AUTODISCOVER} "
            f"subnets={SCAN_SUBNETS or 'auto'} account={ACCOUNT_EMAIL or 'none'} "
            f"mqtt={MQTT_HOST}:{MQTT_PORT} prefix={PREFIX} poll={POLL}s")

        # If an H-Bot account is configured, sign in and scope discovery to devices registered to it.
        fetch_account_allowlist()

        # Discover the device IPs (manual + mDNS + subnet sweep) before announcing.
        log("discovering HBot devices on the LAN …")
        ips = discover_devices()
        if ips:
            log(f"candidate device IPs: {ips}")
        else:
            log("no devices discovered yet. Auto-discovery will keep retrying; "
                "you can also add IPs manually in the add-on Configuration tab.")

        # Connect to MQTT, RETRYING forever — never exit the add-on just because the broker
        # isn't up yet. Exiting here was the "add-on stops after ~20s" symptom.
        log(f"connecting to HA MQTT broker {MQTT_HOST}:{MQTT_PORT} …")
        attempt = 0
        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, 60)
                break
            except Exception as e:
                attempt += 1
                # After a few failures the broker add-on is probably stopped/slow — ask the Supervisor
                # to START it (best-effort), instead of spinning forever on Errno 111/113. Then keep
                # retrying with capped backoff so we auto-recover the instant the broker is up.
                if attempt in (3, 8) or attempt % 12 == 0:
                    ensure_broker_up()
                wait = min(10 + attempt * 2, 30)
                log(f"MQTT connect failed (attempt {attempt}): {e}. Is the 'Mosquitto broker' add-on "
                    f"running? Retrying in {wait}s…")
                time.sleep(wait)
        self.client.loop_start()  # paho auto-reconnects after drops; _on_connect flags rediscovery.

        # Announce everything we found up-front.
        for ip in ips:
            if not self.add_device(ip):
                log(f"{ip}: could not read device (unreachable or not an HBot) — will retry.")

        # Re-discover roughly every ~2 min so newly powered-on devices appear without a restart.
        # The whole loop is guarded so a transient error never kills the add-on.
        rediscover_every = max(1, int(120 / max(POLL, 1)))
        tick = 0
        while True:
            tick += 1
            try:
                # v1.4.8: recover a broker that DROPS AT RUNTIME. After loop_start() paho owns reconnects
                # and never re-runs the initial connect loop's ensure_broker_up(); this monitor covers that
                # gap — if we've been disconnected past the grace it (re)starts the broker with backoff.
                self._broker_drop_monitor()
                # Re-run discovery on the scheduled tick OR immediately after an MQTT reconnect (broker
                # came back) so devices missed during the outage are re-added + re-announced at once.
                due_rediscover = AUTODISCOVER and (tick % rediscover_every == 0)
                if self._need_rediscover:
                    self._need_rediscover = False
                    due_rediscover = True
                    log("running post-reconnect rediscovery…")
                if due_rediscover:
                    for ip in discover_devices():
                        if ip not in self.devices and self.add_device(ip):
                            log(f"{ip}: newly discovered and added.")
                for ip, d in list(self.devices.items()):
                    try:
                        self.poll_once(d)
                    except Exception as e:
                        log(f"{ip}: poll error {e}")
            except Exception as e:
                log(f"loop error (continuing): {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    Bridge().run()
