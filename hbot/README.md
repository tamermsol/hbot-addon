# HBot — Home Assistant add-on

Bring your **H-Bot (Tasmota) devices** into Home Assistant over your **local network** — no cloud
broker, no per-device console commands. Your lights, panels and shutters appear as native HA entities
and are controllable both ways.

## How it works

The add-on **auto-discovers** your H-Bot devices on the LAN — no IPs to type. It finds them via:
- **mDNS** (`_tasmota._tcp` / `_http._tcp`), and
- a **subnet sweep** that probes every host on HA's network and keeps the ones that answer like an
  H-Bot device.

It re-runs discovery about every 2 minutes, so devices you power on later show up automatically.

Then, for each discovered device, the add-on:
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
3. **Start** the add-on — that's it. Auto-discovery is on by default, so you don't need to enter
   anything. Check the **Log** tab: you should see `subnet sweep found: …` / `announced <device> …`.
4. Your devices appear under **Settings → Devices & Services → MQTT**, controllable from HA.

If a device is on a **different subnet** than HA (so the sweep won't reach it), or you'd rather pin
exact IPs, add them under `devices` in the **Configuration** tab — they're always included on top of
auto-discovery.

## Options

| Option | Meaning |
|---|---|
| `autodiscover` | Find devices automatically via mDNS + subnet sweep (default `true`). |
| `devices` | Optional list of device IPs to force-include (e.g. on another subnet). Leave empty to rely on auto-discovery. |
| `subnets` | Optional CIDR(s) to sweep, e.g. `192.168.1.0/24`. Empty = derive HA's own subnet. |
| `poll_interval` | Seconds between state refreshes (default 10). |
| `discovery_prefix` | HA MQTT discovery prefix (default `homeassistant`). |

## Notes

- Multi-channel panels appear as one HA **device** with a switch per channel.
- Shutters appear as a **cover** with position control.
- If a device is offline at start, the add-on keeps retrying it on each poll.
