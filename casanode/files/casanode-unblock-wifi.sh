#!/usr/bin/env bash
set -euo pipefail

COUNTRY="FR"

# Wait until wlan0 exists (max 15s) because firmware/driver may be slow
for i in $(seq 1 15); do
	[ -e /sys/class/net/wlan0 ] && break
	sleep 1
done

# Clean up any persisted rfkill states
rm -f /var/lib/systemd/rfkill/*wlan* /var/lib/systemd/rfkill/*phy* 2>/dev/null || true

# Unblock Wi-Fi, retry if still soft-blocked
rfkill unblock all || true
for i in $(seq 1 10); do
	if rfkill list 2>/dev/null | awk '/Wireless LAN/{f=1} f&&/Soft blocked/{print $3; exit}' | grep -q '^no$'; then
		break
	fi
	rfkill unblock all || true
	sleep 1
done

# Set regulatory domain (country) and bring wlan0 UP
iw reg set "$COUNTRY" 2>/dev/null || true
ip link set wlan0 up 2>/dev/null || true

exit 0