# VM addresses, per-VM monitoring and development additions

## Goal and design

Show available IPv4 addresses for existing VMs without changing DHCP reservations. Preserve VMDetails.ip as the managed reservation, and expose separate observations with their source. Prefer bounded guest-agent reads, valid unexpired DHCP leases, static cloud-init configuration and MAC-bound private-bridge neighbors. Never guess IPs from names, associate another VM's MAC or modify networking. Optional discovery failures should not hide the VM inventory.

Add copy-on-click for the IP table cell and a copy button in details. Use Textual clipboard support with an appropriate WSL/system fallback; report unsupported clipboard delivery honestly. Add a per-VM details screen with CPU, memory, network and disk-I/O sparklines. Data belongs to the selected VM, not the host. Current metrics come from pvesh status/current, initial history from pvesh rrddata. Poll current metrics outside the event loop, only while details are open, with no overlapping calls. Derive B/s from cumulative counters and elapsed host time; discard rates after counter reset, reboot, clock reversal or stopped state. Missing data stays unavailable, never invented zero. Label minute-averaged historical points and host-reported VM memory; no guest process monitor is implied.

Add editable go/python module definitions. Python installs distro Python/pip/venv support plus uv for the configured cloud user. surge-dev includes go and python; full-dev is a real composition. Keep dev installation opt-in and system features separate. Documentation includes reproducible, sanitized TUI screenshots and exact installation/update examples in English and Russian.

## Tasks and ownership

1. Root: shared DTOs/Operations contract, CLI IP/source and stats presentation, docs/screenshots/release integration and final review.
2. Observability backend: owns new network/discovery.py and services/metrics.py, queries.py, ProxmoxClient, local.py/protocol.py/ssh.py/worker.py, and focused backend/transport tests. Use the existing subprocess abstraction, bounded reads and typed allowlist.
3. TUI observability: owns src/vmctl/tui and focused portable TUI tests, plus utils/clipboard.py if needed. Implement IP copy and selected-VM metrics UI over Operations, never call host tools directly. Respect busy/mutation/password guards and both wide/narrow screens.
4. Development modules: owns config/bootstrap/modules/go.toml and python.toml, guest-only installer scripts, profiles.toml and focused bootstrap tests. Verify official distro/upstream installation semantics; do not execute installers against the host.
5. Verification: mocked unit/headless tests, strict types/lint/build, visual inspection, read-only real-host address/metric validation, client/worker package upgrade preserving config, then publish and verify CI. No VM creation/deletion/power actions during this work.

## Shared contract

IPObservation(address: IPv4Address, source: guest-agent|dhcp-lease|cloud-init|neighbor). VMDetails appends addresses and ip_notes, with display_ip and ip_source properties. VMMetricPoint contains timestamp, cpu_percent, memory_bytes/memory_total_bytes, network_in/out_bytes_per_second and disk_read/write_bytes_per_second. VMStats contains vm, timestamp, cpu_percent, memory_bytes/memory_total_bytes, uptime_seconds, pid, cumulative network_in/out_bytes and disk_read/write_bytes, optional history tuple and history_note. Operations.stats(reference, *, history=False) -> VMStats. Missing scalar values are None. Version 0.4.0; protocol remains a backward compatible typed read-only extension.

## Skills and evidence

Python best practices, codebase-memory, subagent task execution and effective README documentation. Code graph refreshed at 2026-10-07T15:35:12Z; exact source coverage for the changed adapters/models is current. Live read-only evidence shows MAC-matched DHCP leases for existing VMs 100/200/201, with .184/.20/.21 respectively; the current table omitted them because it only read generated vmctl reservations. Existing bootstrap scripts are excluded by the graph and must be read directly. User clarified graphs must be for individual VMs.

## Completed verification

- 394 mocked/headless tests passed; Ruff check and format passed for 79 Python files; strict mypy passed for 50 source files. Wheel and sdist built with the new module definitions, guest script, TUI details and CSS included.
- Independent review caught fresh pvesh processes returning the initial CPU zero. Current CPU now uses the selected VM's bounded, identity-checked pvestatd cache from cluster/resources. Memory and cumulative counters still come from status/current; RRD values are already rates. Missing cached CPU is unavailable.
- Four reproducible demo screenshots were visually inspected and added to both READMEs; their 45 code blocks match. Native clipboard writes were mocked, not exercised against the user's clipboard.
- Matching 0.4.0 wheels installed locally and on pxmx. config init added only the two module files and uv script. Server surge-dev was extended atomically with Go/Python and full-dev added, preserving other settings and an atomic backup.
- Read-only installed-tool checks verified DHCP-observed .20/.21, selected VM200 history and nonzero cached CPU, advancing 5-second polls, network/disk rates, close cleanup, and the 80x24 details layout. All 13 VM/storage/dnsmasq file hashes remained unchanged. No VM creation, deletion or lifecycle action was performed; guest installers were not executed.
- Native Windows/macOS/Linux client CI and the Linux full suite are checked after publication; local results above do not substitute for those runs.
