#!/usr/bin/env python3
"""HBot bridge — brings H-Bot (Tasmota) devices into Home Assistant over the LAN.

For each device IP (from the add-on options) it:
  1. reads Tasmota `Status 0` over HTTP (http://<ip>/cm?cmnd=Status%200) to learn topic/channels/type,
  2. publishes HA MQTT discovery to HA's built-in Mosquitto so the entity appears automatically,
  3. subscribes to the HA command topics and relays them to the device's HTTP API (POWERn/Shutter*),
  4. polls each device and republishes state so HA stays in sync.

No cloud broker: everything runs on the local network via the device HTTP API + HA's own MQTT.
"""
import json
import os
import time
from urllib.parse import quote
import requests
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
PREFIX = os.environ.get("HBOT_PREFIX", "homeassistant")
POLL = int(os.environ.get("HBOT_POLL", "10"))
DEVICES = [d.strip() for d in os.environ.get("HBOT_DEVICES", "").split(",") if d.strip()]

HTTP_TIMEOUT = 5


def log(*a):
    print("[hbot]", *a, flush=True)


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

    def run(self):
        log(f"config: devices={DEVICES} mqtt={MQTT_HOST}:{MQTT_PORT} prefix={PREFIX} poll={POLL}s")
        if not DEVICES:
            log("NO DEVICE IPS configured — add device IPs in the add-on Configuration tab.")
        # Probe devices first so we know their topics/channels before announcing.
        for ip in DEVICES:
            d = Device(ip)
            ok = False
            for attempt in range(3):
                if d.probe():
                    ok = True
                    break
                log(f"{ip}: probe attempt {attempt+1}/3 failed, retrying…")
                time.sleep(2)
            if ok:
                self.devices[ip] = d
                log(f"{ip}: read OK → topic={d.topic} name='{d.name}' {'shutter' if d.shutter else str(d.channels)+'ch'}")
            else:
                log(f"{ip}: COULD NOT READ device. Check the IP is correct and reachable from HA "
                    f"(try http://{ip}/ in a browser on the HA network). Skipping for now.")
        if not self.devices:
            log("no reachable devices yet; will keep retrying every poll.")

        log(f"connecting to HA MQTT broker {MQTT_HOST}:{MQTT_PORT} …")
        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        except Exception as e:
            log(f"MQTT connect FAILED: {e}. Is the 'Mosquitto broker' add-on installed & running?")
            raise
        self.client.loop_start()

        while True:
            for ip in DEVICES:
                d = self.devices.get(ip)
                if d is None:
                    # retry devices that were offline at start
                    nd = Device(ip)
                    if nd.probe():
                        self.devices[ip] = nd
                        self.announce(nd)
                    continue
                try:
                    self.poll_once(d)
                except Exception as e:
                    log(f"{ip}: poll error {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    Bridge().run()
