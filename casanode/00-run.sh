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

# Add dvpn-node-manager repository
echo "Adding dvpn-node-manager repository..."
echo "deb http://ppa.launchpad.net/foxinou/dvpn-node-manager/ubuntu jammy main" | tee -a "${ROOTFS_DIR}/etc/apt/sources.list"
echo "deb-src http://ppa.launchpad.net/foxinou/dvpn-node-manager/ubuntu jammy main" | tee -a "${ROOTFS_DIR}/etc/apt/sources.list"

# Add GPG key and repository
echo "Adding dvpn-node-manager GPG key..."
install -m 644 files/foxinou_dvpn-node-manager.gpg "${ROOTFS_DIR}/etc/apt/trusted.gpg.d/foxinou_dvpn-node-manager.gpg"

# Install dvpn-node-manager
echo "Installing dvpn-node-manager..."
on_chroot << EOF
apt-get update
# apt-get install -y dvpn-node-manager
EOF

# Git clone
echo "Cloning repository..."
git clone https://github.com/sentinelgrowthdao/casanode-ble "${ROOTFS_DIR}/opt/casanode/casanode-ble/"

# Install dependencies
echo "Installing npm dependencies..."
on_chroot << EOF
npm install --prefix /opt/casanode/casanode-ble/app
EOF

# Install casanode startup.sh
echo "Installing casanode startup.sh..."
install -m 644 files/startup.sh "${ROOTFS_DIR}/opt/casanode/startup.sh"
chmod +x "${ROOTFS_DIR}/opt/casanode/startup.sh"

# Create casanode user
echo "Creating casanode user..."
on_chroot << EOF
adduser --disabled-password --gecos "" --home /opt/casanode casanode
chown -R casanode: /opt/casanode
usermod -aG sudo,adm,docker casanode
sed -i '/%sudo\s\+ALL=(ALL:ALL) ALL/a casanode ALL=(ALL) NOPASSWD:ALL' /etc/sudoers
EOF

# Create casanode configuration
echo "Creating casanode configuration..."
on_chroot << EOF
touch /etc/casanode.conf
chown -R casanode: /etc/casanode.conf
chmod 600 /etc/casanode.conf
EOF

# Create log directory
on_chroot << EOF
mkdir /var/log/casanode/
chown -R casanode: /var/log/casanode/
chmod 755 /var/log/casanode
EOF

# Create systemd service
echo "Creating systemd service..."
install -m 644 files/casanode.service "${ROOTFS_DIR}/etc/systemd/system/casanode.service"
install -m 644 files/casanode-startup.service "${ROOTFS_DIR}/etc/systemd/system/casanode-startup.service"
echo "Enabling systemd service..."
ln -sv "/lib/systemd/system/casanode.service" "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/casanode.service"
ln -sv "/lib/systemd/system/casanode-startup.service" "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/casanode-startup.service"

echo "End of casanode configuration."
exit 0
