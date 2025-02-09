#!/bin/bash -e

echo "Starting casanode configuration..."

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
EOF

# Create logrotate configuration
echo "Creating logrotate configuration..."
install -m 644 files/logrotate "${ROOTFS_DIR}/etc/logrotate.d/casanode"

echo "End of casanode configuration."
exit 0
