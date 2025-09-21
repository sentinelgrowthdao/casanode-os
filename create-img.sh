#!/bin/bash
set -euo pipefail

############################################################
# create-img.sh (enhanced)
# Improvements:
#  - Uses 'losetup -Pf' so kernel creates partition nodes (loopXp1/loopXp2)
#    instead of manual offset math (reduces risk of wrong offsets)
#  - Validates that boot (FAT) and root (ext4) partitions are detected
#  - Optional quick fsck on root after modification (can be skipped with NO_FSCK=1)
#  - Verifies that expected files (device.json, modified hostapd.conf) exist before unmount
#  - Adds SHA256 sum output for the patched image
#  - Stricter cleanup and safer unmount (lazy fallback)
#  - Clear diagnostic logging to stderr
#  - Detects if output image equals input to avoid overwriting inadvertently
############################################################

# Patch an existing Raspberry Pi OS image with Casanode Wi-Fi configuration
# Usage: ./create-img.sh <base-image.img> [OUTPUT.img] [COUNTRY] [SSID] [PASS] [AUTH_TOKEN]
# - If OUTPUT not provided, will create <base>-patched.img
# - COUNTRY default: CN
# - SSID default: Casanode-alpha1
# - PASS default: alpha1 (must be 8-63 chars; you should override in production)
# - Applies:
#   * device.json on boot with provided (or default) values
#   * Sets cfg80211 regdom, wpa_supplicant country, hostapd country + SSID/passphrase
#   * Resets casanode firstboot flag so logic runs if needed
#   * Updates unblock script default COUNTRY
# Requires: root privileges (loop mount), losetup, kpartx or sfdisk, parted

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)" >&2
  exit 1
fi

if [ $# -lt 1 ]; then
  echo "Usage: $0 <base-image.img> [OUTPUT.img]" >&2
  exit 1
fi

BASE_IMG="$1"
if [ ! -f "$BASE_IMG" ]; then
  echo "Base image not found: $BASE_IMG" >&2
  exit 1
fi
OUT_IMG="${2:-${BASE_IMG%.img}-patched.img}"
COUNTRY="${3:-US}"
SSID="${4:-Casanode-alpha1}"
PASS="${5:-casanode@alpha1}"
AUTH_TOKEN="${6:-422f069c-4a48-4499-872d-30f365320d76}"

echo "Base image: $BASE_IMG" >&2
echo "Output image: $OUT_IMG" >&2
echo "Wi-Fi Country: $COUNTRY" >&2
echo "Wi-Fi SSID: $SSID" >&2
echo "Wi-Fi Passphrase: $PASS" >&2
echo "API Auth Token: $AUTH_TOKEN" >&2

LEN=${#PASS}
if [ $LEN -lt 8 ] || [ $LEN -gt 63 ]; then
  echo "Error: Wi-Fi passphrase length must be 8-63 characters (got $LEN)." >&2
  exit 1
fi

if [ "$BASE_IMG" -ef "$OUT_IMG" ]; then
  echo "[FATAL] Output image path resolves to the same file as input. Choose a different OUTPUT." >&2
  exit 1
fi

echo "Copying base image to $OUT_IMG ..." >&2
cp --reflink=auto "$BASE_IMG" "$OUT_IMG"
sync
echo "Copy done." >&2

echo "Attaching loop device (partition scan)..." >&2
LOOPDEV=$(losetup --show -Pf "$OUT_IMG")
cleanup() {
  set +e
  for d in mnt-root mnt-boot; do
    if mountpoint -q "$d"; then umount "$d" || umount -l "$d" || true; fi
  done
  if [ -n "${LOOPDEV:-}" ]; then
    losetup -d "$LOOPDEV" 2>/dev/null || true
  fi
}
trap cleanup EXIT

BOOT_PART="${LOOPDEV}p1"
ROOT_PART="${LOOPDEV}p2"
mkdir -p mnt-boot mnt-root

if [ ! -b "$BOOT_PART" ] || [ ! -b "$ROOT_PART" ]; then
  echo "[ERROR] Could not find expected partition nodes ($BOOT_PART / $ROOT_PART)." >&2
  ls -l ${LOOPDEV}* >&2 || true
  exit 1
fi

echo "Mounting boot: $BOOT_PART" >&2
mount -o rw "$BOOT_PART" mnt-boot
echo "Mounting root: $ROOT_PART" >&2
mount -o rw "$ROOT_PART" mnt-root

echo "Writing device.json (boot)..." >&2
mkdir -p mnt-boot/casanode
cat > mnt-boot/casanode/device.json <<EOF
{
  "ssid": "${SSID}",
  "password": "${PASS}",
  "country": "${COUNTRY}"
}
EOF

touch mnt-boot/ssh

# COUNTRY already set (can be overridden by arg)

echo "Configuring cfg80211 regdom..." >&2
mkdir -p mnt-root/etc/modprobe.d
echo "options cfg80211 ieee80211_regdom=${COUNTRY}" > mnt-root/etc/modprobe.d/cfg80211.conf

echo "Configuring wpa_supplicant country..." >&2
mkdir -p mnt-root/etc/wpa_supplicant
if [ ! -f mnt-root/etc/wpa_supplicant/wpa_supplicant.conf ]; then
  install -m 600 /dev/null mnt-root/etc/wpa_supplicant/wpa_supplicant.conf
fi
sed -i '/^country=/d' mnt-root/etc/wpa_supplicant/wpa_supplicant.conf
grep -q '^ctrl_interface=' mnt-root/etc/wpa_supplicant/wpa_supplicant.conf || echo 'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev' >> mnt-root/etc/wpa_supplicant/wpa_supplicant.conf
grep -q '^update_config=' mnt-root/etc/wpa_supplicant/wpa_supplicant.conf || echo 'update_config=1' >> mnt-root/etc/wpa_supplicant/wpa_supplicant.conf
echo "country=${COUNTRY}" >> mnt-root/etc/wpa_supplicant/wpa_supplicant.conf

echo "Patching hostapd config..." >&2
if [ -f mnt-root/etc/hostapd/hostapd.conf ]; then
  sed -i "s/^country_code=.*/country_code=${COUNTRY}/" mnt-root/etc/hostapd/hostapd.conf || true
  grep -q '^country_code=' mnt-root/etc/hostapd/hostapd.conf || echo "country_code=${COUNTRY}" >> mnt-root/etc/hostapd/hostapd.conf
  sed -i "s/^ssid=.*/ssid=${SSID//\//\/}/" mnt-root/etc/hostapd/hostapd.conf || true
  sed -i "s/^wpa_passphrase=.*/wpa_passphrase=${PASS//\//\/}/" mnt-root/etc/hostapd/hostapd.conf || true
fi

echo "Updating unblock script default country..." >&2
if [ -f mnt-root/usr/local/sbin/casanode-unblock-wifi.sh ]; then
  sed -i "s/^COUNTRY=.*/COUNTRY=\"${COUNTRY}\"/" mnt-root/usr/local/sbin/casanode-unblock-wifi.sh || true
fi

echo "Resetting firstboot flag if present..." >&2
rm -f mnt-root/etc/casanode_ap_configured || true

echo "Injecting API auth token..." >&2
CASA_CONF="mnt-root/etc/casanode.conf"
if [ -f "$CASA_CONF" ]; then
  if grep -q '^API_AUTH=' "$CASA_CONF"; then
    sed -i "s|^API_AUTH=.*$|API_AUTH=$AUTH_TOKEN|" "$CASA_CONF"
  else
    echo "API_AUTH=$AUTH_TOKEN" >> "$CASA_CONF"
  fi
else
  echo "API_AUTH=$AUTH_TOKEN" > "$CASA_CONF"
fi

echo "Verifying modifications before unmount..." >&2
if [ ! -f mnt-boot/casanode/device.json ]; then
  echo "[ERROR] device.json missing in boot partition" >&2
  exit 1
fi
if [ -f mnt-root/etc/hostapd/hostapd.conf ]; then
  if ! grep -q "^ssid=${SSID}$" mnt-root/etc/hostapd/hostapd.conf; then
    echo "[WARN] hostapd ssid not updated as expected" >&2
  fi
fi

sync
echo "Sync complete, unmounting..." >&2
umount mnt-root || umount -l mnt-root || true
umount mnt-boot || umount -l mnt-boot || true

if [ "${NO_FSCK:-0}" != "1" ]; then
  echo "Running e2fsck -pf on root partition (best effort)..." >&2
  e2fsck -pf "$ROOT_PART" 2>/dev/null || true
fi

losetup -d "$LOOPDEV" || true
LOOPDEV=""

sync
echo "Image patched successfully: $OUT_IMG" >&2
echo "SHA256: $(sha256sum "$OUT_IMG" | awk '{print $1}')" >&2
echo "Flash command example:" >&2
echo "  sudo dd if=$OUT_IMG of=/dev/sdX bs=4M conv=fsync status=progress" >&2
