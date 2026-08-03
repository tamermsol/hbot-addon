# HBot — Home Assistant add-on

Bring your **H-Bot (Tasmota) devices** into Home Assistant over your **local network** — no cloud
broker, no per-device console commands. Your lights, panels and shutters appear as native HA entities
and are controllable both ways.

## How it works

For each device IP you enter, the add-on:
1. reads the device over the LAN (`http://<ip>/cm?cmnd=Status 0`) to learn its topic, channel count and type,
2. publishes **Home Assistant MQTT discovery** to HA's built-in Mosquitto broker so the entity appears automatically,
3. relays HA commands to the device's local HTTP API (`POWERn`, `ShutterOpen/Close/Position`),
4. polls each device and keeps HA in sync.

Everything stays on your LAN: HA ⇄ built-in Mosquitto ⇄ this add-on ⇄ device HTTP API.

## Requirements

- The **Mosquitto broker** add-on installed (this add-on uses it — declared as `mqtt:need`).
- Your H-Bot devices reachable on the same network as Home Assistant.

## Setup

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add:
   `https://github.com/tamermsol/hbot-addon`
2. Install **HBot**.
3. Open the add-on **Configuration** tab and enter your device IPs:
   ```yaml
   devices:
     - 192.168.1.152
     - 192.168.1.153
   poll_interval: 10
   ```
   (Find each device's IP in your router, or in the H-Bot app under the device's info.)
4. **Start** the add-on. Check the **Log** tab — you should see `announced <device> …` lines.
5. Your devices now appear under **Settings → Devices & Services → MQTT**, controllable from HA.

## Options

| Option | Meaning |
|---|---|
| `devices` | List of device IP addresses on your LAN. |
| `poll_interval` | Seconds between state refreshes (default 10). |
| `discovery_prefix` | HA MQTT discovery prefix (default `homeassistant`). |

## Notes

- Multi-channel panels appear as one HA **device** with a switch per channel.
- Shutters appear as a **cover** with position control.
- If a device is offline at start, the add-on keeps retrying it on each poll.
