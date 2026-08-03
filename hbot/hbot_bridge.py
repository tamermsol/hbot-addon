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
import ipaddress
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
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

HTTP_TIMEOUT = 5
# (connect, read) timeouts for the sweep. A short CONNECT timeout is what keeps a /24 sweep fast
# even when many hosts silently drop SYNs (firewalled) instead of refusing — those would otherwise
# each block for the full timeout and make the sweep take ~20s.
PROBE_CONNECT_TIMEOUT = 0.6
PROBE_READ_TIMEOUT = 1.5
SCAN_WORKERS = 128
DEBUG = os.environ.get("HBOT_DEBUG", "false").lower() in ("true", "1", "yes")


def log(*a):
    print("[hbot]", *a, flush=True)


# ── auto-discovery ──────────────────────────────────────────────────────────
def _looks_like_hbot(status):
    """True if a `Status 0` JSON reply looks like an HBot/Tasmota device.

    A Tasmota `Status 0` reply always has a top-level "Status" object with a non-empty "Topic".
    That alone is a reliable Tasmota signature (nothing else answers /cm with this shape), so we
    accept on Topic + any of the usual Tasmota keys — kept lenient so an unusual firmware build
    isn't wrongly rejected."""
    if not isinstance(status, dict):
        return False
    st = status.get("Status")
    if not isinstance(st, dict):
        return False
    topic = str(st.get("Topic") or "")
    if not topic:
        return False
    return any(k in st for k in ("FriendlyName", "Module", "DeviceName", "Power"))


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
                log(f"  {ip}:80 answered JSON but not an HBot Status 0 reply")
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
        self.client.on_message = self._on_message
        self.devices = {}          # ip -> Device
        self.cmd_map = {}          # command_topic -> (ip, kind, index)

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
        if kind == "power":
            tasmota(ip, f"POWER{idx} {payload}")  # ON / OFF
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
        # Re-announce + re-subscribe on every (re)connect so command topics are always live.
        for d in self.devices.values():
            self.announce(d)

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
        log(f"config: manual={MANUAL_DEVICES} autodiscover={AUTODISCOVER} "
            f"subnets={SCAN_SUBNETS or 'auto'} mqtt={MQTT_HOST}:{MQTT_PORT} prefix={PREFIX} poll={POLL}s")

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
        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, 60)
                break
            except Exception as e:
                log(f"MQTT connect failed: {e}. Is the 'Mosquitto broker' add-on running? "
                    f"Retrying in 10s…")
                time.sleep(10)
        self.client.loop_start()

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
                if AUTODISCOVER and tick % rediscover_every == 0:
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
