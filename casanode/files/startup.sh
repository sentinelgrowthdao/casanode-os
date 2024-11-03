#!/bin/bash

LOGFILE="/var/log/casanode/startup.log"
USER="casanode"

# Clear the log file at the start of each execution
> "$LOGFILE"

# Check if Docker socket file exist
if [ -f /etc/systemd/system/multi-user.target.wants/docker.service ]
then
	echo "Docker rootless is not installed. Installing..." | tee -a "$LOGFILE"
	
	# Check if Docker rootful is running and stop it
	echo "Rootful Docker is running. Stopping Docker rootful..." | tee -a "$LOGFILE"
	systemctl disable --now docker.service docker.socket
	rm /var/run/docker.sock
	echo "Docker rootful stopped." | tee -a "$LOGFILE"
	
	# Enable linger for the casanode user to allow the service to start at boot
	loginctl enable-linger casanode
	echo "Linger enabled for casanode user." | tee -a "$LOGFILE"
	
	# Add subuid and subgid entries for casanode as required by Docker rootless mode
	echo "casanode:100000:65536" >> /etc/subuid
	echo "casanode:100000:65536" >> /etc/subgid
	
	# Install Docker rootless
	echo "Installing Docker rootless..." | tee -a "$LOGFILE"
	su -l "$USER" -c 'export XDG_RUNTIME_DIR=/run/user/$(id -u casanode) && \
	export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus && \
	dockerd-rootless-setuptool.sh install' | tee -a "$LOGFILE"
	echo "Docker rootless installation completed." | tee -a "$LOGFILE"
else
	echo "Docker rootless is already installed." | tee -a "$LOGFILE"
fi

# Check and apply necessary capabilities to node if needed
NODE_PATH=$(eval readlink -f $(which node))
if ! getcap "$NODE_PATH" | grep -q "cap_net_raw+eip"
then
	echo "Applying necessary capabilities to node..." | tee -a "$LOGFILE"
	setcap cap_net_raw+eip "$NODE_PATH" | tee -a "$LOGFILE"
	echo "Capabilities applied." | tee -a "$LOGFILE"
else
	echo "Necessary capabilities for node are already applied." | tee -a "$LOGFILE"
fi

# Configure UFW rules if not already configured
UFW_STATUS=$(ufw status | grep -i "Status: active")
if [ -z "$UFW_STATUS" ]
then
	echo "Configuring UFW rules..." | tee -a "$LOGFILE"
	ufw default deny incoming | tee -a "$LOGFILE"
	ufw default allow outgoing | tee -a "$LOGFILE"
	ufw allow ssh | tee -a "$LOGFILE"
	ufw allow 8080 | tee -a "$LOGFILE"
	ufw allow 8081 | tee -a "$LOGFILE"
	ufw --force enable | tee -a "$LOGFILE"
	echo "UFW rules configured." | tee -a "$LOGFILE"
else
	echo "UFW is already active." | tee -a "$LOGFILE"
fi

exit 0
