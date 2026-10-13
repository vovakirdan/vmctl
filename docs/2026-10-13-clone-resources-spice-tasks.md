# Clone, Resource Editing and SPICE

The 0.6.0 update adds duplication of existing VMs, CPU/RAM edits and optional
SPICE guest integration to the CLI and Textual frontend. Both frontends reuse
typed services through the existing SSH worker. Proxmox remains the VM source
of truth; generated dnsmasq reservations remain the managed IP source of truth.

## Completed tasks

- [x] Add typed requests, previews and results to Operations and the worker
  protocol. Keep previews read-only and classify the three apply operations as
  mutations. Bind confirmation to the exact VM configuration and request.
- [x] Add clone SOURCE NEW_NAME. Allocate a VMID through Proxmox, perform a
  full clone and reserve a new private IP for the generated MAC. Leave the
  duplicate stopped unless explicitly requested otherwise.
- [x] Add update VM --cpu N --memory SIZE. Preserve socket topology and change
  only requested fields. Running VMs require explicit permission for graceful
  shutdown and restart. A stopped VM stays stopped; matching resources do not
  cause a power action.
- [x] Add spice VM --guest-tools auto|skip|install. Preserve SPICE-capable
  VirtIO/QXL displays; use QXL for other display types. Install guest packages
  only through an available QEMU guest agent on a supported Linux OS.
- [x] Add editable spice.toml with OS IDs, command argument arrays and bounded
  execution/polling timeouts. Bundle defaults and preserve existing settings
  when initializing configuration.
- [x] Add Clone, Edit and SPICE forms to Textual. Explain downtime and guest
  limitations, require a server preview before confirmation, and disable Apply
  in read-only mode. Fit the action bar into 80×24 terminals.
- [x] Add mocked service, transport, CLI and headless TUI tests. Update both
  READMEs and reproducible screenshots using a fake worker.

## Safety boundaries

Templates and reserved VMIDs 9000–9099 are protected. Pending configuration,
locks and unsupported hardware layouts are rejected before mutation. Clone
supports one private net0 without VLAN tags/trunks, storage-managed disks,
and no passthrough, host hooks or arbitrary QEMU arguments.

A running clone source requires explicit temporary shutdown permission. The
source is restarted after copying when its original configuration and stopped
state can be verified. Clone compensation destroys only an invocation-owned,
definitely stopped target with target-owned writable disks. It never releases
source-owned snippets or reservations. Interrupted or unverifiable outcomes
are retained for inspection.

Raw clones retain guest accounts, credentials, static network settings and
application identities. Cloud-init sources receive fresh clone-owned data for
hostname/DHCP without replaying source bootstrap, users or password payloads.
Foreign custom cloud-init data requires manual cloning.

Hardware rollback restores only known changed fields while the VM is
definitely stopped and other configuration is unchanged. No forced stop or
automatic retry is used after an uncertain operation.

QEMU guest execution is asynchronous. A polling timeout does not terminate a
guest process; the result reports uncertainty and its PID when known. A lost
submission response is also uncertain, even without a PID. No further package
command, reboot or rollback is submitted while that process may still run.

SPICE display configuration does not prove clipboard integration or a working
viewer connection. Android integration remains manual. This release does not
issue viewer tickets, launch a viewer, or change firewall, public networking or
WireGuard settings. Development bootstrap remains independent.

## Verification

- 661 mocked/headless tests passed; Ruff and strict mypy passed; both package
  formats built successfully.
- Matching 0.6.0 client and worker wheels were installed manually. Configuration
  initialization added spice.toml and preserved 33 existing settings files.
- Read-only CLI previews passed against Android VM 300 and cloud-init VM 101.
  Live worker previews also passed in all three Textual forms at 80×24 with
  Apply disabled.
- Real clone, power changes, resource changes and guest package installation
  were not performed during development. Guest acceptance remains separate
  from mocked tests and read-only host previews.
