#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
from typing import Optional

# Where the boot (vfat) partition is mounted changed on Bookworm.
# Prefer /boot/firmware (vfat root), then fall back to legacy /boot.
DEVICE_JSON_CANDIDATES = [
    "/boot/firmware/casanode/device.json",
    "/boot/casanode/device.json",
]
FLAG_FILE = "/etc/casanode_ap_configured"

if os.path.exists(FLAG_FILE):
    exit(0)

# Defaults (match image). We will only overwrite hostapd.conf if
# device.json provides explicit values; otherwise we leave the image defaults.
default_ssid = "Casanode-SSID"
default_password = "casanode"

data = {}
device_json_path = None
for candidate in DEVICE_JSON_CANDIDATES:
    if os.path.exists(candidate):
        device_json_path = candidate
        break

if device_json_path:
    try:
        with open(device_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
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

# Apply Wi‑Fi country if available; always unblock Wi‑Fi
if wifi_country:
    try:
        subprocess.run(["raspi-config", "nonint", "do_wifi_country", wifi_country], check=False)
    except Exception:
        pass
    # Ensure country is reflected in wpa_supplicant and kernel regs
    try:
        wpa_conf = "/etc/wpa_supplicant/wpa_supplicant.conf"
        os.makedirs(os.path.dirname(wpa_conf), exist_ok=True)
        if not os.path.exists(wpa_conf):
            with open(wpa_conf, "w", encoding="utf-8") as wf:
                wf.write("ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n")
        # Replace any existing country= line with the desired country
        try:
            with open(wpa_conf, "r", encoding="utf-8") as wf:
                lines = wf.readlines()
        except FileNotFoundError:
            lines = []
        new_lines = [l for l in lines if not l.strip().startswith("country=")]
        new_lines.append(f"country={wifi_country}\n")
        with open(wpa_conf, "w", encoding="utf-8") as wf:
            wf.writelines(new_lines)
    except Exception:
        pass
    try:
        subprocess.run(["iw", "reg", "set", wifi_country], check=False)
    except Exception:
        pass
try:
    subprocess.run(["rfkill", "unblock", "wifi"], check=False)
except Exception:
    pass

ssid_from_json = data.get("ssid") if isinstance(data, dict) else None
password_from_json = data.get("password") if isinstance(data, dict) else None

# Only rewrite hostapd.conf if SSID or password was provided in device.json.
updated = False
if ssid_from_json or password_from_json:
    ssid = ssid_from_json or default_ssid
    password = password_from_json or default_password
    hostapd_conf = f"""interface=wlan0
driver=nl80211
ssid={ssid}
hw_mode=g
channel=1
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
"""
    if wifi_country:
        hostapd_conf += f"country_code={wifi_country}\n"
        hostapd_conf += "ieee80211d=1\n"

    with open("/etc/hostapd/hostapd.conf", "w", encoding="utf-8") as f:
        f.write(hostapd_conf)

    with open("/etc/default/hostapd", "w", encoding="utf-8") as f:
        f.write('DAEMON_CONF="/etc/hostapd/hostapd.conf"\n')

    updated = True  # we actually changed hostapd config

def _svc_active(name: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0

try:
    # Restart if config changed OR hostapd isn't running yet (first boot)
    need_restart = updated or (not _svc_active("hostapd"))
    if need_restart:
        subprocess.run(["systemctl", "try-reload-or-restart", "dnsmasq"], check=False)
        subprocess.run(["systemctl", "try-reload-or-restart", "hostapd"], check=False)
    # Create the flag only at the very end, after attempting restarts
    pathlib.Path(FLAG_FILE).touch()
except Exception:
    pass

# # Reboot to ensure all changes take effect (Wi‑Fi country, hostapd, etc)
# try:
#     subprocess.run(["sync"])
#     subprocess.run(["reboot"], check=False)
# except Exception:
#     pass