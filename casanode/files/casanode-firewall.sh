#!/usr/bin/env bash
set -euo pipefail

# Flush existing rules
iptables -F
iptables -t nat -F

# Allow loopback and established connections
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow everything on wlan0
iptables -A INPUT -i wlan0 -j ACCEPT
iptables -A FORWARD -i wlan0 -o eth0 -m conntrack --ctstate NEW,ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -i eth0 -o wlan0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# Optionally allow SSH on eth0 if marker file exists
if [ -f /boot/enable-ssh-eth0 ]; then
    iptables -A INPUT -i eth0 -p tcp --dport 22 -j ACCEPT
fi

# Drop all other traffic coming from eth0
iptables -A INPUT -i eth0 -j DROP

exit 0
