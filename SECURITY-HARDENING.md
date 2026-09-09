# Security hardening — 0.3.2

This change addresses the review of 2026-09-09. It has not been tested on a physical gateway and should be reviewed and installed as a test update before release.

## Implemented

- Ingress handlers reject actual socket peers other than 172.30.32.2, including requests with forged forwarded headers. HTTP workers and request timeouts are bounded.
- MQTT discovery parser validates JSON shapes and exact publisher topic/schema. Repair writes are scoped to the associated mower and exclude gen2 mappings. Both `status` and `mower_status` resource names are accepted. Invalid commands do not tear down the connection.
- MQTT/WebSocket frames are limited to 256 KiB and a packet deadline; unsupported WebSocket fragmentation fails closed. The WebSocket handshake verifies Upgrade/Connection/Accept and preserves data after the handshake.
- Unexpected control errors retry without logging their raw content. A separate supervisor restarts a terminated worker and removes leftover SSH tunnel descendants. Pending commands are limited to 32.
- Add-on SSH paths use accept-new and a shared persistent `/data/ssh/known_hosts_gardena`. Key changes are rejected. Existing container-local stores are copied if still available at first startup. Container recreation may have removed those old stores already; no independently verified first key is claimed. Standalone uses the user's persistent `.ssh/known_hosts_gardena`.
- The old MQTT installer inside the hash-pinned upstream bundle is no longer executed by the add-on. A local audited installer transfers a serialized EnvironmentFile via stdin, uses quoted SSH arrays and legacy SCP, writes secrets atomically with 0600, and replaces the stopped binary atomically. Shell control characters in configuration are rejected. Passwords are absent from installation argv.
- Release hash verification and safe extraction happen before gateway mutations. Each add-on deployment has a private temporary workspace removed afterwards. Older Python without tar data filtering fails closed. Standalone also verifies before mutation and uses fresh extraction directories.
- A configured SSH-disable policy is honored after installation failure if that call enabled SSH. Pre-existing reachable SSH is preserved on failure. Exact previous state cannot be inferred from failed reachability. If local control is enabled, disabling SSH is rejected.
- Runtime source copies at repository root are checked against the actual add-on sources in CI to prevent drift. `python3 -m unittest discover -s tests -v` is the canonical suite.

## Deliberately still open

- MQTT transport TLS, verification/pinning of the gateway HTTPS certificate and removal of HTTP from gateway web controls require broker/gateway configuration and a hardware test.
- SSH first contact remains trust-on-first-use. `accept-new` is not protection against an attacker present at the initial connection. Verify the first fingerprint independently before treating the connection as authenticated. Do not delete a saved host key merely to silence a mismatch.
- The gateway publisher binary still uses `--broker-pass` in its systemd unit. Removing that runtime argv exposure requires its source and a new MIPS build. Secrets can also be read by privileged processes from configuration/memory.
- Discovery association retains the existing single-mower/single-publisher behavior for compatibility. Schema checks do not cryptographically bind an MQTT identity to a gateway. A malicious client allowed to publish convincing GARDENA discovery records can still spoof the association. Enforce broker topic ACLs; a stronger device mapping needs trusted device metadata and a gateway test. Multi-gateway brokers may keep control separate if association is ambiguous.
- The existing toggle API's firewall exposure and compiled authentication implementation are unchanged. This is not a complete security certification of the binary release or the gateway OS.
- Standalone installation is not the HA runtime; Windows packaging and full failure rollback there need separate platform validation.

## Validation before merge/release

1. Open the add-on panel through HA Ingress and confirm status/deploy access. Direct container-port access must return 403.
2. Confirm the first SSH fingerprint over a trusted route; restart/update the add-on and verify it reuses the persistent store. An unexpected changed key must fail.
3. Redeploy MQTT with a password containing spaces, quotes, dollar signs and backslashes. Confirm publisher authentication, permissions and command-line limitation noted above. No real mower start is needed for installation validation.
4. Verify docked/mowing states and existing entity association for the actual publisher schema. Test commands only with the mower in a safe situation and explicit operator control.
5. Restart the broker/add-on, stop the worker and verify recovery. Test malformed dummy discovery in an isolated topic namespace.
6. TLS/certificate and the gateway-web-API changes are a follow-up, not implied to be completed by this PR.
