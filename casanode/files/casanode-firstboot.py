#!/usr/bin/env python3
import json
import os
import pathlib

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
cert = data.get("cert")

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

with open("/etc/hostapd/hostapd.conf", "w", encoding="utf-8") as f:
    f.write(hostapd_conf)

with open("/etc/default/hostapd", "w", encoding="utf-8") as f:
    f.write('DAEMON_CONF="/etc/hostapd/hostapd.conf"\n')

if cert:
    os.makedirs("/etc/casanode", exist_ok=True)
    with open("/etc/casanode/cert.pem", "w", encoding="utf-8") as cf:
        cf.write(cert)

pathlib.Path(FLAG_FILE).touch()
