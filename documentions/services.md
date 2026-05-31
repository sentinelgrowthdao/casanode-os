# Services

This page lists the systemd services present in the Casanode image.

## Base Services

- `casanode-firstboot.service`
  - Configures AP credentials on first boot.
  - Runs before `hostapd` and `dnsmasq`.

- `casanode-unblock-wifi.service`
  - Removes rfkill blocks.
  - Sets the Wi-Fi regulatory domain.

- `casanode-firewall.service`
  - Configures `iptables` rules.
  - Allows the required local services and NAT to `eth0`.

- `dnsmasq.service`
  - Provides DHCP and DNS for the local AP network.

- `hostapd.service`
  - Provides the Wi-Fi access point.

- `nginx.service`
  - Serves the local web UI and local API endpoints on the Wi-Fi AP network.

## NAT Service

- `casanode-natd.service`
  - Starts a local daemon that talks to the router through multiple port-mapping backends.
  - Exposes a local Unix socket for the API side.
  - Renews leases automatically.
  - Removes mappings on shutdown when possible.
  - Binds all upstream traffic to `eth0`.
  - Synchronizes the host firewall through a dedicated `CASANODE_PORTS` chain.
  - Writes runtime logs under `/var/log/casanode/`.

## Notes

- Background network services are designed to tolerate partial startup sequences.
- The NAT daemon does not depend on a local TCP port: it listens only on a Unix socket.
