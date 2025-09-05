#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
from typing import Optional

DEVICE_JSON = "/boot/casanode/device.json"
FLAG_FILE = "/etc/casanode_ap_configured"

if os.path.exists(FLAG_FILE):
    exit(0)

if not os.path.exists(DEVICE_JSON):
    exit(0)

with open(DEVICE_JSON, "r", encoding="utf-8") as f:
    data = json.load(f)
ssid = data.get("ssid", "Casanode")
password = data.get("password", "changeme")
wifi_country: Optional[str] = None

# Prefer country from device.json if provided
country_from_json = data.get("country")
if isinstance(country_from_json, str) and len(country_from_json.strip()) == 2:
    wifi_country = country_from_json.strip().upper()

# Otherwise, try to read Wi‑Fi country from wpa_supplicant (if already set)
if not wifi_country:
    try:
        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "r", encoding="utf-8") as wf:
            for line in wf:
                line = line.strip()
                if line.startswith("country=") and len(line) >= 9:
                    candidate = line.split("=", 1)[1].strip().upper()
                    if len(candidate) == 2:
                        wifi_country = candidate
                    break
    except FileNotFoundError:
        pass

# If we have a country, apply it system-wide and unblock wifi (non-interactive)
if wifi_country:
    try:
        subprocess.run(["raspi-config", "nonint", "do_wifi_country", wifi_country], check=False)
    except Exception:
        pass
    try:
        subprocess.run(["rfkill", "unblock", "wifi"], check=False)
    except Exception:
        pass

hostapd_conf = f"""interface=wlan0
ssid={ssid}
hw_mode=g
channel=6
auth_algs=1
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
"""

if wifi_country:
    # Set regulatory domain for AP explicitly to match system setting
    hostapd_conf += f"country_code={wifi_country}\n"
    hostapd_conf += "ieee80211d=1\n"

with open("/etc/hostapd/hostapd.conf", "w", encoding="utf-8") as f:
    f.write(hostapd_conf)

with open("/etc/default/hostapd", "w", encoding="utf-8") as f:
    f.write('DAEMON_CONF="/etc/hostapd/hostapd.conf"\n')

pathlib.Path(FLAG_FILE).touch()
