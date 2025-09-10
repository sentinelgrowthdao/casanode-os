#!/bin/bash -e

echo "Starting casanode configuration..."

# Set environment variables
SENTRY_DSN="<sentry-dsn>"
export SENTRY_DSN

# Ensure first-boot user wizard is disabled (first user is created by pi-gen)
on_chroot <<'EOF'
systemctl disable userconf-pi.service userconf.service 2>/dev/null || true
EOF

# Install Docker
on_chroot << EOF
set -o pipefail
curl -fsSL get.docker.com | sh
apt-get install -y docker-ce-rootless-extras
EOF

# Install Node.js
echo "Installing Node.js..."
on_chroot << EOF
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get update
apt-get install -y nodejs
EOF

# Install casanode
echo "Installing casanode..."
on_chroot << EOF
echo "deb [trusted=yes] https://sentinelgrowthdao.github.io/casanode-ble/ stable main" > /etc/apt/sources.list.d/casanode.list
apt-get update
apt-get install -y casanode=<deb-version>
sed -i "s|^SENTRY_DSN=.*$|SENTRY_DSN=${SENTRY_DSN}|" /etc/casanode.conf || echo "Failed to set SENTRY_DSN in casanode.conf."
EOF

# Network stack prep for AP mode (remove conflicts, ensure dhcpcd)
echo "Preparing network stack for AP..."
on_chroot << 'EOF'
set -e
set -o pipefail
# Remove NetworkManager and RPi net mods which conflict with classic AP stack
apt-get purge -y network-manager raspberrypi-net-mods || true
# Ensure dhcpcd and rfkill are present (also listed in 00-packages)
apt-get install -y dhcpcd5 rfkill || true
# Prevent rfkill from re-applying soft blocks at boot
systemctl mask systemd-rfkill.service systemd-rfkill.socket || true
# Use dhcpcd to manage addresses (required for static IP on wlan0)
systemctl enable dhcpcd || true
# Do not let wpa_supplicant manage wlan0 in AP mode
systemctl mask wpa_supplicant.service wpa_supplicant@wlan0.service || true
EOF

# Radio country/regulatory domain defaults (US) and wpa_supplicant base
echo "Setting Wi-Fi regdom (US) and base wpa_supplicant.conf..."
install -d "${ROOTFS_DIR}/etc/modprobe.d" "${ROOTFS_DIR}/etc/wpa_supplicant"
install -m 644 files/cfg80211.conf "${ROOTFS_DIR}/etc/modprobe.d/cfg80211.conf"
install -m 600 files/wpa_supplicant.conf "${ROOTFS_DIR}/etc/wpa_supplicant/wpa_supplicant.conf"

# Create logrotate configuration
echo "Creating logrotate configuration..."
install -m 644 files/logrotate "${ROOTFS_DIR}/etc/logrotate.d/casanode"
# Configure wireless access point
echo "Configuring access point..."
mkdir -p "${ROOTFS_DIR}/boot/firmware/casanode"
if [ -f files/device.json ]; then
    install -m 600 files/device.json "${ROOTFS_DIR}/boot/firmware/casanode/device.json"
fi

# Add wlan0 static IP stanza if not present
if ! grep -qE '^interface[[:space:]]+wlan0(\b|$)' "${ROOTFS_DIR}/etc/dhcpcd.conf" 2>/dev/null; then
cat <<'EOT' >> "${ROOTFS_DIR}/etc/dhcpcd.conf"

interface wlan0
    static ip_address=192.168.50.1/24
    nohook wpa_supplicant
EOT
fi

install -m 644 files/dnsmasq.conf "${ROOTFS_DIR}/etc/dnsmasq.conf"
# Provide a default hostapd configuration so AP starts even without device.json
install -m 644 files/hostapd.conf "${ROOTFS_DIR}/etc/hostapd/hostapd.conf"
# Ensure hostapd uses our config file on all variants
mkdir -p "${ROOTFS_DIR}/etc/default"
bash -lc 'if [ -f "${ROOTFS_DIR}/etc/default/hostapd" ]; then \
  sed -i "s|^#\?DAEMON_CONF=.*|DAEMON_CONF=\"/etc/hostapd/hostapd.conf\"|" "${ROOTFS_DIR}/etc/default/hostapd" || true; \
else \
  echo "DAEMON_CONF=\"/etc/hostapd/hostapd.conf\"" > "${ROOTFS_DIR}/etc/default/hostapd"; \
fi'
## Hostapd startup ordering: wait for unblock+regdom and dhcpcd
install -d "${ROOTFS_DIR}/etc/systemd/system/hostapd.service.d"
install -m 644 files/hostapd.service.d-override.conf "${ROOTFS_DIR}/etc/systemd/system/hostapd.service.d/override.conf"
install -m 755 files/casanode-firstboot.py "${ROOTFS_DIR}/usr/local/bin/casanode-firstboot.py"
install -m 644 files/casanode-firstboot.service "${ROOTFS_DIR}/etc/systemd/system/casanode-firstboot.service"
echo 'net.ipv4.ip_forward=1' > "${ROOTFS_DIR}/etc/sysctl.d/99-casanode.conf"

# dnsmasq startup ordering: wait for Wi-Fi unblock
install -d "${ROOTFS_DIR}/etc/systemd/system/dnsmasq.service.d"
install -m 644 files/dnsmasq.service.d-override.conf "${ROOTFS_DIR}/etc/systemd/system/dnsmasq.service.d/override.conf"

# Add rfkill unblock oneshot to clear persistent rfkill state and set regdom early
install -m 755 files/casanode-unblock-wifi.sh "${ROOTFS_DIR}/usr/local/sbin/casanode-unblock-wifi.sh"
install -m 644 files/casanode-unblock-wifi.service "${ROOTFS_DIR}/etc/systemd/system/casanode-unblock-wifi.service"

# Install firewall configuration
install -m 755 files/casanode-firewall.sh "${ROOTFS_DIR}/usr/local/sbin/casanode-firewall.sh"
install -m 644 files/casanode-firewall.service "${ROOTFS_DIR}/etc/systemd/system/casanode-firewall.service"

on_chroot <<'EOF'
systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable casanode-firstboot.service
systemctl enable casanode-unblock-wifi.service
systemctl enable casanode-firewall.service
systemctl disable avahi-daemon || true
EOF

echo "End of casanode configuration."
exit 0
