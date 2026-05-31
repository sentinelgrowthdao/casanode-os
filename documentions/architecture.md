# Architecture

The Casanode image is built with `pi-gen` and completed in `casanode/00-run.sh`.

## Main Layers

- `hostapd` provides the local Wi-Fi access point.
- `dnsmasq` provides DHCP and DNS on `wlan0`.
- `nginx` serves the local UI and application fragments.
- `casanode-api` and `casanode-ui` are installed from the Casanode APT repositories.
- `casanode-firewall.service` applies the local network rules.
- `casanode-firstboot.service` prepares Wi-Fi settings on first boot.
- `casanode-unblock-wifi.service` removes rfkill blocks and prepares `wlan0`.
- `casanode-natd.service` manages router port mappings, host firewall openings, and renews them autonomously.

## Boot Flow

The build script installs the system files into the image and then enables the services needed at boot.

The functional order is:

1. `casanode-unblock-wifi` prepares `wlan0`.
2. `hostapd` and `dnsmasq` bring up the local AP.
3. `casanode-firewall` applies the network policy.
4. `casanode-natd` starts and waits for local API commands.

The local API and UI remain reachable over the Wi-Fi AP on `wlan0`. The port-management service does not expose any additional TCP port. Communication with `casanode-api` goes through a local Unix socket. All upstream router traffic and router discovery are bound to `eth0`, while the host firewall opens only the requested ports on that same interface.
