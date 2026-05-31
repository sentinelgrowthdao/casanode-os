#!/usr/bin/env bash
set -euo pipefail

# Flush existing rules
iptables -F
iptables -t nat -F

# Allow loopback and established connections
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow required local services on wlan0
iptables -A INPUT -i wlan0 -p udp --dport 67 -j ACCEPT
iptables -A INPUT -i wlan0 -p udp --dport 53 -j ACCEPT
iptables -A INPUT -i wlan0 -p tcp --dport 53 -j ACCEPT
iptables -A INPUT -i wlan0 -p tcp --dport 80 -j ACCEPT
if [ -f /boot/enable-ssh-wlan0 ] || [ -f /boot/firmware/enable-ssh-wlan0 ] || [ "${CASANODE_ALLOW_WLAN0_SSH:-0}" = "1" ]; then
    iptables -A INPUT -i wlan0 -p tcp --dport 22 -j ACCEPT
fi
iptables -A INPUT -i wlan0 -j DROP

# Allow AP clients to reach upstream network over Ethernet
iptables -A FORWARD -i wlan0 -o eth0 -m conntrack --ctstate NEW,ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -i eth0 -o wlan0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# Dedicated chain for port openings managed by casanode-natd
iptables -N CASANODE_PORTS 2>/dev/null || true
iptables -F CASANODE_PORTS
iptables -A INPUT -i eth0 -j CASANODE_PORTS

# Optionally allow SSH on eth0 if marker file exists
if [ -f /boot/enable-ssh-eth0 ] || [ -f /boot/firmware/enable-ssh-eth0 ] || [ "${CASANODE_ALLOW_ETH0_SSH:-0}" = "1" ]; then
    iptables -A INPUT -i eth0 -p tcp --dport 22 -j ACCEPT
fi

# Drop all other traffic coming from eth0
iptables -A INPUT -i eth0 -j DROP

exit 0
