#!/bin/bash

set -euo pipefail

# Installation script for the Casanode image on an SD card with Wi-Fi configuration
# Usage: ./install-sdcard.sh <image-file> <sd-card-device> [COUNTRY] [SSID] [PASS]
#   COUNTRY: 2-letter code (default FR)
#   SSID: optional override for AP SSID (default Casanode-alpha1)
#   PASS: optional WPA2 passphrase (8-63 chars). If omitted, prompted.
# Example: sudo ./install-sdcard.sh deploy/2025-09-07-casanode-os.img /dev/sda FR MySSID MyStrongPass123

# Default values (used if not provided)
DEFAULT_IMG=""
DEFAULT_COUNTRY="FR"
DEFAULT_SSID="Casanode-alpha1"
DEFAULT_PASS="casanode@alpha1"
DEFAULT_AUTH_TOKEN="422f069c-4a48-4499-872d-30f365320d76"


# AUTH_TOKEN en 6e argument ou prompt si absent
if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <image-file> <sd-card-device> [COUNTRY] [SSID] [PASS] [AUTH_TOKEN]" >&2
  echo "(Defaults: country=$DEFAULT_COUNTRY, ssid=$DEFAULT_SSID, pass=$DEFAULT_PASS, auth_token=$DEFAULT_AUTH_TOKEN)" >&2
  exit 1
fi


IMG="$1"
DEV="$2"
COUNTRY="${3:-$DEFAULT_COUNTRY}"
SSID="${4:-$DEFAULT_SSID}"
PASS="${5:-}" # if empty we'll prompt (but we still have a default available)
AUTH_TOKEN="${6:-}"

if [ -z "$AUTH_TOKEN" ]; then
  read -rsp "Enter API AUTH_TOKEN (leave empty to use default): " ENTERED_AUTH
  echo
  if [ -n "$ENTERED_AUTH" ]; then
    AUTH_TOKEN="$ENTERED_AUTH"
  else
    AUTH_TOKEN="$DEFAULT_AUTH_TOKEN"
  fi
fi

if [ -z "$PASS" ]; then
  read -rsp "Enter Wi-Fi passphrase (8-63 chars, leave empty to use default): " ENTERED
  echo
  if [ -n "$ENTERED" ]; then
    PASS="$ENTERED"
  else
    PASS="$DEFAULT_PASS"
  fi
fi

LEN=${#PASS}
if [ $LEN -lt 8 ] || [ $LEN -gt 63 ]; then
  echo "Error: Wi-Fi passphrase length must be 8-63 characters (got $LEN)." >&2
  exit 1
fi

if [ ! -f "$IMG" ]; then
  echo "Image not found: $IMG" >&2
  exit 1
fi

if [ ! -b "$DEV" ]; then
  echo "Block device not found: $DEV" >&2
  exit 1
fi

read -p "Enable SSH diagnostics on eth0 (create enable-ssh-eth0 file)? (y/N): " ENABLE_SSH_ETH0

echo "Writing image to $DEV ... (this may take several minutes)"
sudo dd if="$IMG" of="$DEV" bs=4M status=progress conv=fsync
echo "Image written. Syncing..."
sync

# Handle partition suffixes (mmcblk0p1, nvme0n1p1, loopXp1)
if [[ "$DEV" =~ (mmcblk|loop|nvme) ]]; then
  PART1="${DEV}p1"
  PART2="${DEV}p2"
else
  PART1="${DEV}1"
  PART2="${DEV}2"
fi

# Configure Wi‑Fi country on the SD card (modifies files on both partitions)
# - The default image uses US. Set COUNTRY to your target (example: FR)
# - This updates boot (device.json) and rootfs (cfg80211, wpa_supplicant, hostapd, unblock script)

sudo mkdir -p /mnt/sdcard/boot /mnt/sdcard/root
sudo mount "$PART1" /mnt/sdcard/boot
sudo mount "$PART2" /mnt/sdcard/root

# 1) Boot partition: device.json for first-boot logic + enable SSH
sudo mkdir -p /mnt/sdcard/boot/casanode
cat <<EOF | sudo tee /mnt/sdcard/boot/casanode/device.json >/dev/null
{
  "ssid": "${SSID}",
  "password": "${PASS}",
  "country": "${COUNTRY}"
}
EOF
sudo touch /mnt/sdcard/boot/ssh

# SSH diag on eth0 (marker file read at first boot by casanode-firstboot logic)
if [[ "$ENABLE_SSH_ETH0" =~ ^[Yy]$ ]]; then
  sudo touch /mnt/sdcard/boot/enable-ssh-eth0
  echo "SSH diag eth0: enabled (marker enable-ssh-eth0)."
else
  echo "SSH diag eth0: left disabled."
fi

# 2) RootFS: kernel regulatory domain (cfg80211)
sudo install -d /mnt/sdcard/root/etc/modprobe.d
echo "options cfg80211 ieee80211_regdom=${COUNTRY}" | sudo tee /mnt/sdcard/root/etc/modprobe.d/cfg80211.conf >/dev/null

# 3) RootFS: wpa_supplicant country (preserve other settings, enforce country)
sudo install -d /mnt/sdcard/root/etc/wpa_supplicant
if [ ! -f /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf ]; then
  sudo install -m 600 /dev/null /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf
fi
sudo sed -i '/^country=/d' /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf
sudo bash -c 'grep -q "^ctrl_interface=" /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf || echo "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev" >> /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf'
sudo bash -c 'grep -q "^update_config=" /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf || echo "update_config=1" >> /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf'
echo "country=${COUNTRY}" | sudo tee -a /mnt/sdcard/root/etc/wpa_supplicant/wpa_supplicant.conf >/dev/null

# 4) RootFS: hostapd country
if [ -f /mnt/sdcard/root/etc/hostapd/hostapd.conf ]; then
  sudo sed -i "s/^country_code=.*/country_code=${COUNTRY}/" /mnt/sdcard/root/etc/hostapd/hostapd.conf || true
  sudo bash -c "grep -q '^country_code=' /mnt/sdcard/root/etc/hostapd/hostapd.conf || echo 'country_code=${COUNTRY}' >> /mnt/sdcard/root/etc/hostapd/hostapd.conf"
  # Ensure SSID & passphrase reflect provided values
  sudo sed -i "s/^ssid=.*/ssid=${SSID//\//\/}/" /mnt/sdcard/root/etc/hostapd/hostapd.conf || true
  sudo sed -i "s/^wpa_passphrase=.*/wpa_passphrase=${PASS//\//\/}/" /mnt/sdcard/root/etc/hostapd/hostapd.conf || true
fi

# 5) RootFS: unblock script (ensures early regulatory domain)
if [ -f /mnt/sdcard/root/usr/local/sbin/casanode-unblock-wifi.sh ]; then
  sudo sed -i "s/^COUNTRY=.*/COUNTRY=\"${COUNTRY}\"/" /mnt/sdcard/root/usr/local/sbin/casanode-unblock-wifi.sh || true
fi

# 6) Configure AUTH_TOKEN into /etc/casanode.conf
CONF_FILE="/mnt/sdcard/root/etc/casanode.conf"
if [ -f "$CONF_FILE" ]; then
  if grep -q '^API_AUTH=' "$CONF_FILE"; then
    sudo sed -i "s|^API_AUTH=.*$|API_AUTH=$AUTH_TOKEN|" "$CONF_FILE"
  else
    echo "API_AUTH=$AUTH_TOKEN" | sudo tee -a "$CONF_FILE" >/dev/null
  fi
else
  echo "API_AUTH=$AUTH_TOKEN" | sudo tee "$CONF_FILE" >/dev/null
fi

# Flush and unmount
sync
sudo umount /mnt/sdcard/boot || true
sudo umount /mnt/sdcard/root || true

# Default user
# The image ships with the default user `sentinel` (password: `sentinel`).
# No manual user creation is required.

# Notes
# - If you skip edits, the image starts an AP with SSID "Casanode-SSID",
#   passphrase "casanode", and default country US.
# - Using COUNTRY above updates boot+root so first boot is consistent.

echo "SD card installation complete."
