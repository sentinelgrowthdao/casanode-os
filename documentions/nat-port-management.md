# Port Management

The goal of this service is to keep port mappings on the router without requiring `casanode-api` to manage renewals, firewall rules, or network recovery cases.

## Goal

- `casanode-api` only sends the desired state.
- The local daemon handles:
  - router discovery,
  - router backend selection,
  - firewall port openings and closures on the OS,
  - lease renewals,
  - cleanup of stale mappings,
  - retries when the router or network is unavailable.

## Local Transport

The transport between `casanode-api` and the daemon is a Unix socket:

- default path: `/run/casanode-natd/control.sock`
- format: one JSON object per line
- no local TCP
- no network exposure
- the daemon writes logs to `/var/log/casanode/natd.log`

## Commands

### `sync`

Replaces the full desired mapping set.

Example:

```json
{"action":"sync","mappings":[{"id":"web","protocol":"TCP","external_port":8443,"internal_port":8443,"lease_seconds":3600}]}
```

Each entry can contain:

- `id` or `name`: stable mapping identifier
- `protocol`: `TCP` or `UDP`
- `external_port`: port exposed on the router
- `internal_port`: local destination port
- `internal_client`: target local IPv4, optional
- `lease_seconds`: lease duration, optional
- `description`: UPnP label, optional
- `enabled`: boolean, optional

If `internal_client` is omitted, the daemon uses the IPv4 address of `eth0`.

Example:

```bash
printf '%s\n' '{"action":"sync","mappings":[{"id":"web","protocol":"TCP","external_port":8443,"internal_port":8443,"lease_seconds":3600}]}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `upsert`

Adds or updates a single mapping.

Example:

```bash
printf '%s\n' '{"action":"upsert","mapping":{"id":"web","protocol":"TCP","external_port":8443,"internal_port":8443,"lease_seconds":3600}}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `delete`

Deletes a mapping by `id` or `name`.

Example:

```bash
printf '%s\n' '{"action":"delete","id":"web"}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `clear`

Deletes all desired mappings.

Example:

```bash
printf '%s\n' '{"action":"clear"}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `status`

Returns the current state:

- desired mappings
- applied mappings
- detected router
- external address
- next renewal time
- last error

Example:

```bash
printf '%s\n' '{"action":"status"}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `refresh`

Forces an immediate resync attempt.

Example:

```bash
printf '%s\n' '{"action":"refresh"}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

### `ping`

Simple liveness check.

Example:

```bash
printf '%s\n' '{"action":"ping"}' | sudo socat - UNIX-CONNECT:/run/casanode-natd/control.sock
```

## Renewal

The daemon does not treat mappings as static rules:

- leases are renewed before expiration,
- the polling frequency matches the lease duration,
- if the router changes or disappears, the daemon retries automatically.

By default, the lease duration is 3600 seconds and renewal happens 120 seconds before expiration.

## Firewall Sync

The daemon also maintains a dedicated firewall chain on the host:

- chain name: `CASANODE_PORTS`
- target interface: `eth0`
- rule type: `ACCEPT`
- scope: only the requested ports are opened

The firewall bootstrap script creates the jump from `INPUT` to `CASANODE_PORTS`, and the daemon only manages the content of that dedicated chain.

## Lifecycle

1. `casanode-api` pushes the desired state.
2. The daemon discovers a supported router backend and applies the mappings.
3. The daemon persists the desired state in `/var/lib/casanode-natd/state.json`.
4. Mappings are refreshed periodically to keep leases valid.
5. On clean shutdown, the daemon removes applied mappings when the router allows it.

## Response Example

```json
{"ok":true,"action":"sync","count":2}
```

## Implementation Notes

- The daemon tries multiple router backends: UPnP IGD via `WANIPConnection` or `WANPPPConnection`, PCP, and NAT-PMP.
- All upstream discovery and SOAP traffic is bound to `eth0`.
- Local access to `casanode-api` and the UI remains available over the Wi-Fi AP on `wlan0`.
- The service works without depending on `casanode-api` once the state is synchronized.
- Renewals are autonomous, so the API side does not need to manage timers.
