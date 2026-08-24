# screen_brightness.py
#
# Dynamically adjusts LG TV backlight based on screen content luminance
# and (optionally) room ambient light via Home Assistant.
#
# Compatible with LG webOS firmware 43.21.60+ which blacklisted the
# bscpylgtv/aiowebostv certificate. Uses the luna dialog hack discovered
# by Simon (https://hackaday.io/project/195594) to write picture settings
# without requiring WRITE_SETTINGS permission.
#
# How the luna dialog hack works:
#   LG webOS exposes ssap://system.notifications/createAlert which can
#   be called without elevated permissions. By setting the onclose/onfail
#   callbacks to a luna:// URI, the TV executes that luna call internally
#   when the alert closes. We open the alert and immediately close it,
#   so the luna call fires instantly with no visible popup.
#
# pip install mss numpy requests websockets

import asyncio
import configparser
import os
import re
import socket
import ssl
import sys
import time

import numpy as np
import requests
import mss
import websockets
from datetime import datetime

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- single-instance lock ----------------

_LOCK_PORT = 47653
_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _lock_socket.bind(("127.0.0.1", _LOCK_PORT))
except OSError:
    print(f"{ts()} Another copy of this script is already running. Exiting.")
    sys.exit(1)

# ---------------- config ----------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

def load_config():
    cfg = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        print(f"{ts()} Config file not found: {CONFIG_FILE}")
        print(f"{ts()} Please create config.ini next to this script.")
        sys.exit(1)
    cfg.read(CONFIG_FILE)
    return cfg

def save_tv_config(key=None, last_ip=None):
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    if key is not None:
        cfg["tv"]["client_key"] = key
    if last_ip is not None:
        cfg["tv"]["last_ip"] = last_ip
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)

def save_client_key(key):
    save_tv_config(key=key)

def clear_client_key():
    save_client_key("")

# ---------------- tuning constants ----------------

AMBIENT_POLL_INTERVAL_SECONDS = 1
FALLBACK_CEILING = 100
LONG_UNAVAILABLE_SECONDS = 600

AMBIENT_STEP_DOWN_PER_SEC = 12
AMBIENT_STEP_UP_PER_SEC = 5

CEILING_COMMIT_DEAD_ZONE = 3

LUX_POINTS =     [0,  3,  8,  30, 70]
CEILING_POINTS = [50, 50, 60, 80, 100]

POLL_INTERVAL_SECONDS = 0.125
DOWNSAMPLE_SIZE = 64

BACKLIGHT_MIN = 1
EXTREME_BRIGHT_LUMINANCE = 200
DEAD_ZONE = 2
CREEP_FRACTION = 0.15
MIN_WRITE_INTERVAL_SECONDS = 0.3   # slightly higher than before due to two-call hack

WATCHDOG_INTERVAL_SECONDS = 4
WAIT_POLL_INTERVAL_SECONDS = 10
TV_CONNECT_TIMEOUT_SECONDS = 5

# ---------------- WebOS manifest ----------------
# Unsigned manifest (no signatures block). Grants basic permissions
# including WRITE_NOTIFICATION_ALERT which is needed for createAlert.
# WRITE_SETTINGS is requested but will be denied by the firmware --
# that's fine, we route backlight writes through the luna dialog hack.

MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.1",
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE",
        "TEST_OPEN", "TEST_PROTECTED",
        "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
        "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_MEDIA_PLAYBACK",
        "CONTROL_INPUT_TV", "CONTROL_POWER",
        "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
        "READ_NETWORK_STATE", "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST",
        "WRITE_NOTIFICATION_TOAST", "WRITE_NOTIFICATION_ALERT",
        "READ_POWER_STATE", "READ_COUNTRY_INFO",
        "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
        "READ_INSTALLED_APPS", "READ_SETTINGS", "WRITE_SETTINGS",
    ]
}

# ---------------- SSL context ----------------

def make_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

# ---------------- HA helpers ----------------

def ha_get_state(entity_id, ha_url, ha_token):
    resp = requests.get(
        f"{ha_url}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {ha_token}"},
        timeout=3,
    )
    resp.raise_for_status()
    return resp.json()["state"]


def is_tv_on_via_ha(tv_entity, ha_url, ha_token):
    try:
        return ha_get_state(tv_entity, ha_url, ha_token) == "on"
    except Exception:
        return None


def fetch_ambient_lux(light_entity, ha_url, ha_token):
    return float(ha_get_state(light_entity, ha_url, ha_token))

# ---------------- TV connection ----------------

async def ws_connect(tv_ip):
    ctx = make_ssl_context()
    return await asyncio.wait_for(
        websockets.connect(f"wss://{tv_ip}:3001", ssl=ctx),
        timeout=TV_CONNECT_TIMEOUT_SECONDS,
    )


async def tv_pair(ws, existing_key=None):
    """
    Send hello + register. If existing_key is provided, attempt to resume
    the session without showing a pairing prompt on the TV.
    Returns the client key on success, raises on failure.
    """
    hello = {"type": "hello", "payload": {}}
    await ws.send(__import__("json").dumps(hello))
    await ws.recv()

    payload = {
        "forcePairing": False,
        "pairingType": "PROMPT",
        "manifest": MANIFEST,
    }
    if existing_key:
        payload["client-key"] = existing_key

    register = {"type": "register", "id": "reg0", "payload": payload}
    await ws.send(__import__("json").dumps(register))

    import json
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        t = msg.get("type")
        if t == "registered":
            return msg["payload"].get("client-key")
        if t == "error":
            raise RuntimeError(f"Pairing error: {msg.get('error')}")
        # type == "response" means TV is waiting for the user to accept --
        # just keep waiting


async def try_connect_tv(tv_ip, client_key):
    """
    Attempt to connect and pair. Returns (ws, key) on success, None on failure.
    If the stored key is rejected, clears it and re-pairs (shows TV prompt).
    """
    import json

    # --- try with existing key first ---
    if client_key:
        try:
            ws = await ws_connect(tv_ip)
            key = await asyncio.wait_for(
                tv_pair(ws, existing_key=client_key),
                timeout=TV_CONNECT_TIMEOUT_SECONDS,
            )
            print(f"{ts()} [connect] resumed session with stored key")
            return ws, key, tv_ip
        except Exception as e:
            print(f"{ts()} [connect] stored key rejected ({e}), clearing and re-pairing")
            clear_client_key()
            try:
                await ws.close()
            except Exception:
                pass

    # --- fresh pair ---
    try:
        ws = await ws_connect(tv_ip)
        print(f"{ts()} [connect] waiting for pairing prompt on TV -- accept it to continue")
        key = await asyncio.wait_for(tv_pair(ws), timeout=60)
        save_client_key(key)
        print(f"{ts()} [connect] paired successfully, key saved to config.ini")
        return ws, key, tv_ip
    except Exception as e:
        print(f"{ts()} [connect] failed to connect/pair ({e})")
        try:
            await ws.close()
        except Exception:
            pass
        return None

# ---------------- luna dialog hack ----------------

import json as _json

async def luna_set_picture(ws, settings: dict):
    """
    Write picture settings via the luna dialog hack.
    Opens an ssap createAlert with onclose/onfail pointing at
    luna://com.webos.settingsservice/setSystemSettings, then immediately
    closes the alert. The TV executes the luna call internally.
    No return value confirmation (fire and forget from our side).
    """
    luna_uri = "luna://com.webos.settingsservice/setSystemSettings"
    luna_params = {"category": "picture", "settings": settings}

    alert_payload = {
        "title": "x",
        "message": "x",
        "modal": True,
        "isSysReq": True,
        "type": "confirm",
        "buttons": [{
            "label": "Ok",
            "focus": True,
            "buttonType": "ok",
            "onClick": luna_uri,
            "params": luna_params,
        }],
        "onclose": {"uri": luna_uri, "params": luna_params},
        "onfail": {"uri": luna_uri, "params": luna_params},
    }

    await ws.send(_json.dumps({
        "type": "request",
        "id": "luna_open",
        "uri": "ssap://system.notifications/createAlert",
        "payload": alert_payload,
    }))
    resp = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    alert_id = resp.get("payload", {}).get("alertId")

    if not alert_id:
        raise RuntimeError(f"createAlert failed: {resp}")

    await ws.send(_json.dumps({
        "type": "request",
        "id": "luna_close",
        "uri": "ssap://system.notifications/closeAlert",
        "payload": {"alertId": alert_id},
    }))
    await asyncio.wait_for(ws.recv(), timeout=5)


async def get_backlight(ws):
    """Read current backlight value from the TV."""
    await ws.send(_json.dumps({
        "type": "request",
        "id": "get_bl",
        "uri": "ssap://settings/getSystemSettings",
        "payload": {"category": "picture", "keys": ["backlight"]},
    }))
    resp = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    settings = resp.get("payload", {}).get("settings", {})
    val = settings.get("backlight")
    return int(val) if val is not None else None

# ---------------- ambient ceiling task ----------------

async def ambient_ceiling_task(loop, state, light_entity, ha_url, ha_token):
    tracked_lux = None
    consecutive_failures = 0
    fallen_back = False
    last_printed = None

    while True:
        try:
            raw_lux = await loop.run_in_executor(
                None, fetch_ambient_lux, light_entity, ha_url, ha_token
            )
            consecutive_failures = 0

            if tracked_lux is None or fallen_back:
                tracked_lux = raw_lux
                fallen_back = False
                print(f"{ts()} [ambient] seeded tracked lux at {tracked_lux:.1f}")
            elif raw_lux < tracked_lux:
                tracked_lux = max(raw_lux, tracked_lux - AMBIENT_STEP_DOWN_PER_SEC)
            elif raw_lux > tracked_lux:
                tracked_lux = min(raw_lux, tracked_lux + AMBIENT_STEP_UP_PER_SEC)

            state["lux"] = tracked_lux

            new_ceiling = float(np.interp(tracked_lux, LUX_POINTS, CEILING_POINTS))
            if abs(new_ceiling - state["ceiling"]) > CEILING_COMMIT_DEAD_ZONE:
                state["ceiling"] = new_ceiling
                changed = True
            else:
                changed = False

            snapshot = (round(raw_lux, 1), round(tracked_lux, 1), round(state["ceiling"]))
            if snapshot != last_printed:
                tag = "->" if changed else " "
                print(f"{ts()} [ambient] raw={raw_lux:.1f} tracked={tracked_lux:.1f} {tag} ceiling={state['ceiling']:.0f}")
                last_printed = snapshot

        except Exception as e:
            consecutive_failures += 1
            seconds_failing = consecutive_failures * AMBIENT_POLL_INTERVAL_SECONDS
            if consecutive_failures == 1:
                print(f"{ts()} [ambient] poll failed ({e}) -- ignoring for now")
            if seconds_failing >= LONG_UNAVAILABLE_SECONDS and not fallen_back:
                print(f"{ts()} [ambient] WARNING: unreachable for {seconds_failing:.0f}s, "
                      f"falling back to ceiling={FALLBACK_CEILING}")
                state["ceiling"] = FALLBACK_CEILING
                fallen_back = True
                tracked_lux = None
                last_printed = None

        await asyncio.sleep(AMBIENT_POLL_INTERVAL_SECONDS)

# ---------------- screen luminance ----------------

def get_average_luminance(sct, monitor):
    img = np.array(sct.grab(monitor))
    h, w = img.shape[0], img.shape[1]
    step_h = max(1, h // DOWNSAMPLE_SIZE)
    step_w = max(1, w // DOWNSAMPLE_SIZE)
    small = img[::step_h, ::step_w]
    b = small[:, :, 0]
    g = small[:, :, 1]
    r = small[:, :, 2]
    gray = 0.114 * b + 0.587 * g + 0.299 * r
    return float(gray.mean())


def luminance_to_backlight(avg_luminance_0_255, ceiling):
    if avg_luminance_0_255 >= EXTREME_BRIGHT_LUMINANCE:
        return BACKLIGHT_MIN
    frac = avg_luminance_0_255 / 255.0
    target = ceiling - frac * (ceiling - BACKLIGHT_MIN)
    return int(round(max(BACKLIGHT_MIN, min(ceiling, target))))

# ---------------- active session ----------------

async def run_active_session(loop, ws, sct, monitor, ha_cfg):
    ha_enabled  = ha_cfg["enabled"]
    tv_entity   = ha_cfg.get("tv_entity")
    light_entity = ha_cfg.get("light_entity")
    ha_url      = ha_cfg.get("url")
    ha_token    = ha_cfg.get("token")

    state = {"ceiling": FALLBACK_CEILING, "lux": float("nan")}

    ambient_task = None
    if ha_enabled:
        ambient_task = asyncio.create_task(
            ambient_ceiling_task(loop, state, light_entity, ha_url, ha_token)
        )

    committed = None
    last_write_time = 0.0
    last_power_check = time.monotonic()
    last_watchdog_check = time.monotonic()

    try:
        while True:
            now = time.monotonic()

            # Watchdog
            if committed is not None and now - last_watchdog_check >= WATCHDOG_INTERVAL_SECONDS:
                last_watchdog_check = now
                try:
                    actual_val = await get_backlight(ws)
                    if actual_val is not None and actual_val != committed:
                        print(f"{ts()} [watchdog] MISMATCH: committed={committed}, TV={actual_val} -- something overriding us")
                except Exception as e:
                    print(f"{ts()} [watchdog] check failed: {e}")

            ceiling = state["ceiling"]
            room_lux = state.get("lux", float("nan"))
            avg_luminance = await loop.run_in_executor(None, get_average_luminance, sct, monitor)
            target = luminance_to_backlight(avg_luminance, ceiling)

            if committed is None:
                await luna_set_picture(ws, {"backlight": target})
                committed = target
                last_write_time = time.monotonic()
                print(f"{ts()} lum={avg_luminance:6.1f} ceiling={ceiling:.0f} room={room_lux:.1f}  init backlight -> {target}")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Clamp committed to ceiling if ceiling dropped
            if committed > ceiling:
                committed = int(ceiling)
                await luna_set_picture(ws, {"backlight": committed})
                last_write_time = time.monotonic()
                print(f"{ts()} [ceiling clamp] backlight -> {committed}")

            write_now = time.monotonic()
            can_write = (write_now - last_write_time) >= MIN_WRITE_INTERVAL_SECONDS

            if avg_luminance >= EXTREME_BRIGHT_LUMINANCE:
                if committed != target and can_write:
                    await luna_set_picture(ws, {"backlight": target})
                    last_write_time = write_now
                    committed = target
                    print(f"{ts()} lum={avg_luminance:6.1f} ceiling={ceiling:.0f} room={room_lux:.1f}  SNAP -> {target}")
            else:
                diff = target - committed
                if abs(diff) > DEAD_ZONE:
                    raw_step = diff * CREEP_FRACTION
                    step = max(1, round(raw_step)) if raw_step > 0 else min(-1, round(raw_step))
                    new_value = committed + step
                    if can_write:
                        await luna_set_picture(ws, {"backlight": new_value})
                        last_write_time = write_now
                        committed = new_value
                        print(f"{ts()} lum={avg_luminance:6.1f} ceiling={ceiling:.0f} room={room_lux:.1f}  creep -> {new_value} (target={target})")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    except Exception as e:
        print(f"{ts()} [session] lost the TV mid-session ({e}). Ending session.")
    finally:
        if ambient_task:
            ambient_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass

# ---------------- main loop ----------------

async def main():
    cfg = load_config()

    tv_ips = [ip.strip() for ip in cfg["tv"]["ip"].split(",") if ip.strip()]
    last_ip = cfg["tv"].get("last_ip", "").strip()
    if last_ip and last_ip in tv_ips and tv_ips[0] != last_ip:
        tv_ips.remove(last_ip)
        tv_ips.insert(0, last_ip)
    client_key = cfg["tv"].get("client_key", "").strip()

    ha_enabled = cfg["home_assistant"].getboolean("enabled", fallback=False)
    ha_cfg = {
        "enabled": ha_enabled,
        "url": cfg["home_assistant"].get("url", "").strip(),
        "token": cfg["home_assistant"].get("token", "").strip(),
        "light_entity": cfg["home_assistant"].get("light_entity", "").strip(),
        "tv_entity": cfg["home_assistant"].get("tv_entity", "").strip(),
    }

    loop = asyncio.get_event_loop()
    sct = mss.mss()
    monitor = sct.monitors[1]

    print(f"{ts()} Starting. Waiting for TV to be on...\n")

    try:
        while True:
            # Always probe the TV directly -- never trust HA for TV state.
            # HA lux sensor is used inside the session for ceiling adjustment only.
            result = None
            for tv_ip in tv_ips:
                result = await try_connect_tv(tv_ip, client_key)
                if result is not None:
                    break
            if result is None:
                await asyncio.sleep(WAIT_POLL_INTERVAL_SECONDS)
                continue

            ws, client_key, connected_ip = result  # key may have been refreshed
            save_tv_config(last_ip=connected_ip)

            print(f"{ts()} [wait] TV connected. Starting session.\n")
            await run_active_session(loop, ws, sct, monitor, ha_cfg)
            print(f"{ts()} \n[wait] Back to waiting for the TV...\n")

    except KeyboardInterrupt:
        pass
    finally:
        sct.close()


if __name__ == "__main__":
    asyncio.run(main())
