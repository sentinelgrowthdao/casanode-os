#!/bin/bash -e

echo "Starting casanode configuration..."

# Set environment variables
SENTRY_DSN="<sentry-dsn>"
export SENTRY_DSN

# Ensure first-boot user wizard is disabled (first user is created by pi-gen)
on_chroot <<'EOF'
systemctl disable userconf-pi.service userconf.service 2>/dev/null || true
EOF

# Remove apt-listchanges early (avoid network changelog fetch errors in build chroot)
on_chroot <<'EOF'
apt-get purge -y apt-listchanges || true
export APT_LISTCHANGES_FRONTEND=none
echo 'apt-listchanges removed (changelog fetch suppressed).'
EOF

# Install Docker
on_chroot << 'EOF'
set -o pipefail
curl -fsSL get.docker.com | sh
apt-get install -y docker-ce-rootless-extras
EOF

# Install Node.js
echo "Installing Node.js..."
on_chroot << 'EOF'
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get update
apt-get install -y nodejs
EOF

# Install casanode-api & casanode-ui
echo "Installing casanode-api & casanode-ui (version <deb-version>)..."
on_chroot << 'EOF'
echo "deb [trusted=yes] https://sentinelgrowthdao.github.io/casanode-api/ stable main" > /etc/apt/sources.list.d/casanode-api.list
echo "deb [trusted=yes] https://sentinelgrowthdao.github.io/casanode-ui/ stable main" > /etc/apt/sources.list.d/casanode-ui.list
apt-get update
# Safe version handling (placeholder <deb-version> may remain if not replaced)
VER="${CASANODE_VERSION:-<deb-version>}"
if [ "$VER" = "<deb-version>" ]; then
  echo '[casanode] Installing latest versions (placeholder not substituted).'
  apt-get install -y casanode-api casanode-ui || true
else
  if ! apt-get install -y "casanode-api=$VER" "casanode-ui=$VER"; then
    echo "[casanode] Specific version $VER not found, falling back to latest." >&2
    apt-get install -y casanode-api casanode-ui || true
  fi
fi
sed -i "s|^SENTRY_DSN=.*$|SENTRY_DSN=${SENTRY_DSN}|" /etc/casanode.conf || echo "Failed to set SENTRY_DSN in casanode.conf."
EOF

# Configure nginx site for casanode and include fragment directory
echo "Configuring nginx for casanode..."
install -m 644 files/casanode-nginx.conf "${ROOTFS_DIR}/etc/nginx/sites-available/casanode"
ln -sf /etc/nginx/sites-available/casanode "${ROOTFS_DIR}/etc/nginx/sites-enabled/casanode"
mkdir -p "${ROOTFS_DIR}/opt/casanode/nginx"
# Remove default site to avoid port 80/server_name conflicts
rm -f "${ROOTFS_DIR}/etc/nginx/sites-enabled/default" "${ROOTFS_DIR}/etc/nginx/sites-available/default" || true
on_chroot <<'EOF'
systemctl enable nginx
# Remove any invalid upstream blocks inside server-level fragments (should be http-level)
for f in /opt/casanode/nginx/*.conf; do
  [ -f "$f" ] || continue
  if grep -Eq '^[[:space:]]*upstream[[:space:]]+casanode_api' "$f"; then
    echo "[nginx] Stripping upstream block from $f (relocating not yet structured)." >&2
    # Delete the upstream block only
    sed -i '/^[[:space:]]*upstream[[:space:]]\+casanode_api/,/^[[:space:]]*}/d' "$f"
  fi
done
nginx -t || (echo '----- nginx error context -----'; grep -Hn 'upstream' /opt/casanode/nginx/*.conf || true; exit 1)
EOF

# Provide network-online.target using dhcpcd (needed before adding Wants/After elsewhere)
install -m 644 files/dhcpcd-wait-online.service "${ROOTFS_DIR}/etc/systemd/system/dhcpcd-wait-online.service"

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
systemctl enable dhcpcd-wait-online.service || true
# Do not let wpa_supplicant manage wlan0 in AP mode
systemctl mask wpa_supplicant.service wpa_supplicant@wlan0.service || true
EOF

# Radio country/regulatory domain defaults (US) and wpa_supplicant base
echo "Setting Wi-Fi regdom (US) and base wpa_supplicant.conf..."
install -d "${ROOTFS_DIR}/etc/modprobe.d" "${ROOTFS_DIR}/etc/wpa_supplicant"
# install -m 644 files/cfg80211.conf "${ROOTFS_DIR}/etc/modprobe.d/cfg80211.conf"
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

# Include Sentinel docker image tarballs harvested at build time if available
install -d "${ROOTFS_DIR}/opt/casanode/docker"
# Copy ARM64 version
SENTINEL_TAR_ARM64_NAME="sentinel-dvpnx-arm64.tar"
SENTINEL_TAR_ARM64_SRC="files/docker/${SENTINEL_TAR_ARM64_NAME}"
SENTINEL_TAR_ARM64_DST="${ROOTFS_DIR}/opt/casanode/docker/${SENTINEL_TAR_ARM64_NAME}"
if [ -f "${SENTINEL_TAR_ARM64_SRC}" ]; then
  install -m 644 "${SENTINEL_TAR_ARM64_SRC}" "${SENTINEL_TAR_ARM64_DST}"
  rm -f "${SENTINEL_TAR_ARM64_SRC}"
else
  echo "[build] Sentinel docker image tarball (ARM64) not found at ${SENTINEL_TAR_ARM64_SRC}; skipping copy."
fi
# Copy AMD64 version
SENTINEL_TAR_AMD64_NAME="sentinel-dvpnx-amd64.tar"
SENTINEL_TAR_AMD64_SRC="files/docker/${SENTINEL_TAR_AMD64_NAME}"
SENTINEL_TAR_AMD64_DST="${ROOTFS_DIR}/opt/casanode/docker/${SENTINEL_TAR_AMD64_NAME}"
if [ -f "${SENTINEL_TAR_AMD64_SRC}" ]; then
  install -m 644 "${SENTINEL_TAR_AMD64_SRC}" "${SENTINEL_TAR_AMD64_DST}"
  rm -f "${SENTINEL_TAR_AMD64_SRC}"
else
  echo "[build] Sentinel docker image tarball (AMD64) not found at ${SENTINEL_TAR_AMD64_SRC}; skipping copy."
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

# Install NAT port mapping daemon
install -m 755 files/casanode-natd.py "${ROOTFS_DIR}/usr/local/sbin/casanode-natd.py"
install -m 644 files/casanode-natd.service "${ROOTFS_DIR}/etc/systemd/system/casanode-natd.service"

on_chroot <<'EOF'
# Load Sentinel Docker image if available
if [ -f "/opt/casanode/docker/sentinel-dvpnx-arm64.tar" ]; then
  docker load < "/opt/casanode/docker/sentinel-dvpnx-arm64.tar" || echo "Failed to load Sentinel image."
fi
systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable casanode-firstboot.service
systemctl enable casanode-unblock-wifi.service
systemctl enable casanode-firewall.service
systemctl enable casanode-natd.service
systemctl disable avahi-daemon || true
EOF

echo "End of casanode configuration."
exit 0
