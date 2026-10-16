# Shared file exchange

SFTPGo is an optional exchange for moving files between your workstation and
selected VMs. Each project continues to live on its own VM disk. No desktop,
persistent mount or extra daemon inside project VMs is needed.

## Service and access

The deployment uses Debian 13 `files-vm` (VMID 102, `10.210.0.105`) with 2 vCPUs,
2 GiB RAM, a 20 GiB OS disk and a separate 200 GiB data disk mounted at `/srv/files`.
SFTPGo Community runs natively under systemd as an unprivileged user. Its pinned
release is [v2.7.6](https://github.com/drakkan/sftpgo/releases/tag/v2.7.6).

| Endpoint | Purpose | Access |
| --- | --- | --- |
| `10.210.0.105:2022` | SFTP file exchange | WireGuard and explicitly allowed VMs |
| `https://10.210.0.105:8443/web/admin` | Administration | WireGuard and pxmx only |

The service stays on private `vmbr1`. No public port forward or WireGuard change
is needed. The service VM firewall limits network access; individual SFTP keys,
source-IP restrictions and SFTPGo permissions control file access.

Approved VMs can read the common directory but write only within their own
uniquely named directory. The workstation account can manage the whole exchange.
Use WebAdmin for deliberate exceptions, such as denying a folder or granting
another writable directory. SFTPGo stores the effective account permissions;
vmctl does not maintain a separate database or reset permissions on upgrades.

No backup job is configured at this stage. RAID does not protect against deletion
or corruption. Files here have no independent recovery copy until external
backup storage is added.

## Connect from your workstation

Connect over WireGuard using the account `workstation` and your SSH identity:

```sh
sftp -P 2022 workstation@10.210.0.105
```

Verify the SFTP host fingerprint through trusted SSH access before accepting it.
The public host key is also configured as `fileshare.host_public_key` on pxmx.
With a non-default identity, add `-i /path/to/private_key` to the workstation
command; never copy the private key to files-vm. WinSCP is an optional Windows GUI
using the same SFTP address, port and SSH key. OpenSSH SFTP commands include:

```text
ls
put /local/path/archive.tar
get archive.tar /local/destination/archive.tar
put -R /local/path/folder
```

For WebAdmin, trust the deployment's private CA only after obtaining it through
the existing trusted SSH connection. Administrator credentials stay in the
root-only `/etc/vmctl/fileshare-admin.json` on pxmx; they are not guest credentials.
Do not paste them into issue reports, shell arguments or chat.

## Give a new VM access

Update both client and worker to 0.8.0 and run `vmctl config validate` first.
The worker must already have a working `[fileshare]` configuration.

```sh
vmctl create project-vm debian small --with-system fileshare --dry-run
vmctl create project-vm debian small --with-system fileshare
vmctl info project-vm
```

The same opt-in choice appears under **System features** in the TUI. It remains
separate from Rust, Node, Python and other development modules. Existing VMs get
no access automatically. Dry-run creates no account or key.

Provisioning generates a unique account and SSH key, installs rclone, and writes
an isolated guest configuration under `~/.config/vmctl/fileshare/`. Its key and
configuration are readable only by the configured cloud-init user. The wrapper
`vmctl-files` always uses this configuration; it preserves your
existing rclone remotes. SSH host verification uses a pinned public key.

Wait for cloud-init to finish before using the wrapper. An SSH banner or a
successful `vmctl create` does not establish that package installation finished.

Inside the guest, replace `YOUR_DIRECTORY` below with the unique writable
directory shown by `vmctl info`. The root-owned wrapper is installed at
`/usr/local/bin/vmctl-files`, so it also works in noninteractive SSH commands.

```sh
vmctl-files lsf files:
vmctl-files copy ./archive.tar files:YOUR_DIRECTORY/ --progress
vmctl-files copy ./folder files:YOUR_DIRECTORY/folder --progress
vmctl-files copy files:YOUR_DIRECTORY/folder ./downloaded-folder --progress
vmctl-files deletefile files:YOUR_DIRECTORY/archive.tar --interactive
vmctl-files purge files:YOUR_DIRECTORY/folder --interactive
```

`copy` can replace existing files within your writable directory. It does not
delete unrelated destination files. `purge` removes the selected directory and
its contents; keep `--interactive` when deleting by hand. rclone handles transfer
progress; see its [SFTP backend documentation](https://rclone.org/sftp/).

Deleting the VM revokes its account, disconnects active file sessions and removes
its firewall allowance. Its uploaded files remain for workstation access.
Deletion stops if access cannot be revoked safely. Cloning a VM with active
fileshare access is rejected because its disk contains a valid private key.

## Worker configuration and maintenance

Add the following optional section to `/etc/vmctl/config.toml` on pxmx. The example
is intentionally incomplete until the actual public key and trusted CA are set.
The API key file contains only the API key, not the administrator JSON document.

```toml
[fileshare]
endpoint = "https://10.210.0.105:8443"
sftp_host = "10.210.0.105"
sftp_port = 2022
host_public_key = "ssh-ed25519 REPLACE_WITH_VERIFIED_PUBLIC_HOST_KEY"
ca_file = "fileshare-ca.crt"
api_key_file = "fileshare-api-key"
shared_directory = "/srv/files/shared"
firewall_vmid = 102
firewall_ipset = "vmctl-files-clients"
timeout_seconds = 15
```

Relative certificate and key paths resolve against the worker config directory.
Keep the API key owned by root with mode `0600`; it is worker-only and must not
be committed. Keep certificate validation enabled. `config init` preserves
existing settings, so configure this integration explicitly after upgrading.
Provisioning fails clearly if the integration is absent or invalid.

Pin the same Ed25519 key on both sides. In the native SFTPGo JSON configuration,
set the following fields under `sftpd` and retain that key across upgrades:

```json
{
  "host_keys": ["/etc/sftpgo/id_ed25519"],
  "host_key_algorithms": ["ssh-ed25519"]
}
```

The stock Debian 13 rclone package can otherwise negotiate an ECDSA host key
while its known_hosts entry pins Ed25519, producing a key mismatch. Offering
only the verified Ed25519 key keeps strict validation working with the distro
package; do not disable host verification to work around this error.

This deployment combines masquerading with the Proxmox VM firewall. Its scoped
conntrack rule assigns zone 1 to traffic entering `fwbr102i0`, as described in
the [Proxmox masquerading documentation](https://pve.proxmox.com/pve-docs/pve-admin-guide.html#sysadmin_network_masquerading).
The host unit `vmctl-files-nat@102.service` restores that rule before
`pve-guests.service`; its template is committed at
[`ops/files-vm/vmctl-files-nat@.service`](../ops/files-vm/vmctl-files-nat@.service).
If moving the service to another VMID, review the firewall bridge and unit
instance together. This is deployment configuration: ordinary VM creation with
`fileshare` updates only the configured service VM's access IP set, and does
not install general host NAT or raw-table policies.

The data disk must mount by UUID before SFTPGo starts. Its systemd service must
fail closed when `/srv/files` is not mounted. Keep SFTPGo's database and SSH host
keys persistent as well as the file data. Preserve ownership when maintaining
these directories, and stop the service before an offline consistency copy.
Do not delete retained VM directories as part of routine account cleanup.

For checks, use `vmctl config validate`, `vmctl info files-vm`, systemd status and
the SFTPGo journal. Avoid dumping full configuration, cloud-init data or API
responses into shared logs: they can contain credentials. Test permissions with
a fresh disposable VM and small files before transferring project archives.
Files around 50 GB require enough free space for both existing and incoming data;
a small-file smoke test does not establish that a 50 GB transfer was verified.
