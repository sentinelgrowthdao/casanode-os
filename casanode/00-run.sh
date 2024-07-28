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

# Git clone
echo "Cloning repository..."
git clone https://github.com/sentinelgrowthdao/casanode-ble "${ROOTFS_DIR}/opt/casanode/sources/"

# Fetch specific commit hash
git -C "${ROOTFS_DIR}/opt/casanode/sources/" fetch origin <commit-hash>

# Check if the commit hash exists in the repository
if git -C "${ROOTFS_DIR}/opt/casanode/sources/" rev-parse --verify "<commit-hash>" >/dev/null 2>&1; then
	# Checkout the specified commit hash
	git -C "${ROOTFS_DIR}/opt/casanode/sources/" checkout "<commit-hash>" || error_exit "Failed to checkout specified commit hash."
fi

# Move app directory
mv "${ROOTFS_DIR}/opt/casanode/sources/app/" "${ROOTFS_DIR}/opt/casanode/app/"
rm -rf "${ROOTFS_DIR}/opt/casanode/sources/"

# Install dependencies and build
echo "Install npm dependencies and build the application..."
on_chroot << EOF
npm install --prefix /opt/casanode/app
npm run build --prefix /opt/casanode/app
EOF

# Install casanode startup.sh
echo "Installing casanode startup.sh..."
install -m 644 files/startup.sh "${ROOTFS_DIR}/opt/casanode/startup.sh"
chmod +x "${ROOTFS_DIR}/opt/casanode/startup.sh"

# Create casanode user
echo "Creating casanode user..."
on_chroot << EOF
adduser --disabled-password --gecos "" --home /opt/casanode --uid 150 casanode
chown -R casanode: /opt/casanode
usermod -aG sudo,adm,docker casanode
sed -i '/%sudo\s\+ALL=(ALL:ALL) ALL/a casanode ALL=(ALL) NOPASSWD:ALL' /etc/sudoers
EOF

# If the "insecure" parameter is passed to the build script
if [ -f "bluetooth.insecure" ]
then
	echo "Setting the default BLE UUID..."
	sed -i 's/BLE_UUID=.*$/BLE_UUID=0000180d-0000-1000-8000/' "files/casanode.conf"
else
	echo "Remove the default BLE UUID..."
	sed -i 's/BLE_UUID=*/BLE_UUID=/' "files/casanode.conf"
fi

# Create casanode configuration
echo "Creating casanode configuration..."
install -m 644 files/casanode.conf "${ROOTFS_DIR}/etc/casanode.conf"
on_chroot << EOF
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

# Create logrotate configuration
echo "Creating logrotate configuration..."
install -m 644 files/logrotate "${ROOTFS_DIR}/etc/logrotate.d/casanode"

echo "End of casanode configuration."
exit 0
