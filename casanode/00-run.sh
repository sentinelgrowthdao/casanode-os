#!/bin/bash -e

echo "Starting casanode configuration..."

# Set environment variables
SENTRY_DSN="<sentry-dsn>"
export SENTRY_DSN

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

# Create logrotate configuration
echo "Creating logrotate configuration..."
install -m 644 files/logrotate "${ROOTFS_DIR}/etc/logrotate.d/casanode"
# Configure wireless access point
echo "Configuring access point..."
mkdir -p "${ROOTFS_DIR}/boot/casanode"
if [ -f files/device.json ]; then
    install -m 600 files/device.json "${ROOTFS_DIR}/boot/casanode/device.json"
fi

cat <<'EOT' >> "${ROOTFS_DIR}/etc/dhcpcd.conf"
interface wlan0
    static ip_address=192.168.50.1/24
    nohook wpa_supplicant
EOT

install -m 644 files/dnsmasq.conf "${ROOTFS_DIR}/etc/dnsmasq.conf"
install -m 755 files/casanode-firstboot.py "${ROOTFS_DIR}/usr/local/bin/casanode-firstboot.py"
install -m 644 files/casanode-firstboot.service "${ROOTFS_DIR}/etc/systemd/system/casanode-firstboot.service"
echo 'net.ipv4.ip_forward=0' > "${ROOTFS_DIR}/etc/sysctl.d/99-casanode.conf"

on_chroot <<'EOF'
systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable casanode-firstboot.service
systemctl mask wpa_supplicant.service wpa_supplicant@wlan0.service
systemctl disable avahi-daemon || true
EOF

echo "End of casanode configuration."
exit 0
