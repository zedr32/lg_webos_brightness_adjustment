# screen_brightness.py

Dynamic backlight control for LG OLED TVs running webOS firmware 43.21.60+.

Adjusts the TV's backlight in real time based on what's on screen and (optionally) how bright the room is. A dark scene dims the backlight. A bright scene raises it. The transition creeps gradually so it's invisible while you're watching. If you have a room lux sensor connected to Home Assistant, the maximum backlight ceiling rises and falls with the ambient light in the room.

<img width="1568" height="711" alt="image" src="https://github.com/user-attachments/assets/d19be849-da99-480d-af36-de0a14187cb1" />


---

## Why this exists

LG's built-in ambient light mode is coarse and slow. What I wanted was something that reacts to actual content — not just the room — and does it smoothly enough that you never notice it happening. An OLED with a high backlight in a dark room causes real eye strain. With this running, the TV is always at the right brightness for whatever is on screen and whatever time of day it is, without touching the remote.

This ran without issue for months using the `bscpylgtv` library. Then LG shipped firmware **43.21.60** on 18 August 2026 and killed it.

---

## What LG changed in firmware 43.21.60

The update made two breaking changes:

**Port 3000 is dead.** The TV no longer accepts WebSocket connections on port 3000. Only port 3001 (WSS/SSL) works now.

**The certificate is blacklisted.** The signed certificate embedded in `bscpylgtv`, `aiowebostv`, and every other third-party WebOS control library was blacklisted by the firmware. Any pairing attempt that includes the `signatures` block in the manifest returns:

```
403 Pairing rejected: blacklisted certificate detected
```

That certificate was what granted elevated permissions, specifically `WRITE_SETTINGS`. Without it, calling `ssap://settings/setSystemSettings` for picture settings returns `401 insufficient permissions`. There is no workaround through the standard SSAP path.

This was confirmed independently by other users across multiple LG OLED models. The `aiowebostv` 0.9.2 fix (shipped in Home Assistant 2026.8.3) restores HA integration for basic TV control, but explicitly does not restore `WRITE_SETTINGS`. That permission is gone on this firmware through the normal path.

**Previous working firmware:** 43.11.78 (released 2026-07-30)  
**Broken firmware:** 43.21.60 (released 2026-08-18)

---

## The fix: luna dialog hack

Credit to Simon's May 2024 Hackaday post for the original technique:  
https://hackaday.io/project/195594-home-sweat-home/log/229399-getting-our-lg-tvs-picture-settings-onto-the-ha-network

The SSAP endpoint `ssap://system.notifications/createAlert` is still accessible without elevated permissions. By setting the `onclose` and `onfail` callbacks of a dialog to a `luna://` URI, the TV executes that luna call internally when the alert closes. Immediately calling `ssap://system.notifications/closeAlert` triggers the callback before the dialog ever appears on screen.

```
createAlert(onclose=luna://com.webos.settingsservice/setSystemSettings, params={backlight: N})
closeAlert(alertId)
```

The TV fires the luna call from inside webOS itself, bypassing the external permission check entirely. No popup appears. The backlight changes. It is 100% reliable in testing.

This script uses that technique for every backlight write.

---

## Strongly recommended: block the TV from the internet

If LG patches `createAlert` in a future firmware update, this script stops working. Once you have it running, block the TV's IP from accessing the internet at your router or firewall. The TV does not need internet access for this script to function. Local network access (port 3001) is all it needs.

---

## Requirements

- Python 3.10+
- LG WebOS TV on your local network (tested on OLED77C6PSA, firmware 43.21.60)
- The PC running this script must be connected to the same network as the TV
- The screen being captured must be the display connected to the TV (or mirrored to it)
- Optional: Home Assistant with a lux sensor entity for room brightness adjustment

```
pip install mss numpy requests websockets
```

---

## Setup

**1. Copy both files** (`screen_brightness.py` and `config.ini`) into the same folder.

**2. Edit `config.ini`:**

```ini
[tv]
ip = 192.168.1.251          # Your TV's IP address
client_key =                # Leave blank -- filled automatically on first run

[home_assistant]
enabled = false             # Set to true if you have HA with a lux sensor
url = http://192.168.1.x:8123
token = your_long_lived_token_here
light_entity = sensor.your_lux_sensor
tv_entity = media_player.your_lg_tv
```

**3. Run the script:**

```
python screen_brightness.py
```

On first run, a pairing prompt will appear on the TV. Accept it. The client key is saved to `config.ini` automatically and reused on every subsequent run. You should never need to pair again unless the TV is factory reset.

---

## How it works

The script captures a downsampled screenshot of the connected display ~8 times per second and calculates average luminance. That luminance value is mapped to a target backlight level. The backlight creeps toward the target at 15% of the remaining gap per poll cycle, so changes are gradual and invisible during normal viewing. On extremely bright content (avg luminance > 200/255) it snaps immediately to minimum backlight.

If Home Assistant is enabled, a separate task polls a lux sensor every second and adjusts the backlight ceiling based on room brightness. A dim room gets a lower ceiling (darker max backlight). A bright room allows the full range. The ceiling transitions slowly using a rate-limited tracker so a light switching on doesn't immediately blow out the screen.

The WebSocket connection to the TV is kept open for the duration of the session. If it drops, the script detects it and reconnects automatically.

---

## Limitations

This only works when the TV is being used as a PC monitor (or with screen content mirrored from a PC). It captures your PC's display output, not the TV's internal apps. Netflix on the TV, YouTube on the TV, nothing running on the TV natively will be captured. For that use case you would need a different approach entirely.

---

## Firmware note

If you are still on firmware 43.11.78 or earlier, the original `bscpylgtv`-based approach works and is simpler. This script is specifically for 43.21.60+ where `bscpylgtv` is broken. If you are on an older firmware and want to stay there, block internet access on the TV immediately — the update is pushed automatically and there is no opt-out.
