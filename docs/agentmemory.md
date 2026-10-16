# Shared agent memory

agentmemory is a separate private service for approved project VMs. All approved
clients share one memory: project labels organize records, they do not provide
access isolation. For an isolated project, run its own memory server and leave
this integration disabled.

## Service layout

The configured deployment is `memory-vm`, VMID **103**, IP **10.210.0.107**:
2 vCPUs, 4 GiB RAM, 20 GiB OS disk and a separate 100 GiB data disk mounted by
UUID at `/srv/agentmemory`. The service runs as an unprivileged user under systemd;
it must not start without the data disk. Proxmox autostart and VM protection are
enabled. A single host does not provide high availability. Reusable deployment
examples are in [the systemd unit](../ops/agentmemory/agentmemory.service),
[server environment](../ops/agentmemory/server.env.example) and
[model-cache setup](../ops/agentmemory/cache.mjs); generate credentials privately
when deploying, rather than reusing an example value.

The pinned components are [agentmemory 0.9.30](https://www.npmjs.com/package/@agentmemory/agentmemory/v/0.9.30)
and [iii-engine 0.22.1](https://github.com/iii-hq/iii/releases/tag/v0.22.1).
Local embeddings are enabled; the model may need an initial download. External
LLM compression and graph extraction are off by default. Data, indexes and model
cache are persistent on the data disk. No external backup job is configured:
RAID cannot recover accidental deletion or corrupted application data.

| Endpoint | Purpose | Reachability |
| --- | --- | --- |
| `https://10.210.0.107` | Authenticated memory API | Explicitly approved VM IPs |
| `127.0.0.1:3111` | Internal API | Service VM loopback only |
| `127.0.0.1:3112` | Internal streams | Service VM loopback only |
| `127.0.0.1:3113` | Viewer | SSH tunnel only |
| `127.0.0.1:49134` | Internal worker | Service VM loopback only |

Caddy validates the individual client key and source IP, then substitutes the
internal backend secret. The backend secret is never sent to project VMs.
Proxmox Firewall separately limits access to the service VM. There are no public
port forwards, WireGuard changes or public listening services on pxmx.

## Enable access for a new VM

Update both vmctl client and worker to **0.9.0**, then validate configuration:

```sh
vmctl config validate
vmctl create project-vm debian small --with-system agentmemory --dry-run
vmctl create project-vm debian small --with codex,claude,opencode --with-system agentmemory
vmctl info project-vm
```

To include file exchange, use `--with-system fileshare,agentmemory`. In the TUI,
select **agentmemory** under **System features**. It is off by default and stays
disabled with an explanation until the worker has a valid service configuration.
First-release guest support is Debian/Ubuntu; unsupported families fail clearly.

This option installs client components, not a server inside every guest and not
AI agents. It configures detected supported Codex, Claude Code and OpenCode after
bootstrap modules finish. A compatible Node runtime is reused or supplied for
client integration; selecting memory does not select the development Node module.
Other MCP servers and agent settings are preserved. The client key is stored
separately in a user-owned `0600` file, with the private CA pinned. Secrets are
absent from CLI/TUI summaries and Proxmox metadata.

MCP and automatic capture differ by agent adapter. Wait for cloud-init to finish
and check the actual agent's MCP tool availability before relying on capture.
The creation summary reports **requested** adapters, not successful guest setup.
Unavailable service calls fail visibly instead of silently creating local memory.
Supported capture hooks have a bounded retry queue.

## Codex hooks require a separate trust review

Codex MCP configuration is usable without enabling native hooks. Installing or
enabling a plugin does **not** approve its hooks: Codex requires a review of the
current hook definitions. At the first interactive Codex launch, run `/hooks`
and review only the agentmemory definitions you intend to trust. Do not use
`--dangerously-bypass-hook-trust`; vmctl does not change global hook trust. See
OpenAI's [plugin hook trust documentation](https://developers.openai.com/plugins/build/plugins).
The client configuration step prints this review requirement.

If a native agentmemory plugin is already enabled, vmctl avoids installing a
second set of hooks. When its existing native environment cannot be safely
rewritten, configuration reports that its remote endpoint needs review. Confirm
that this plugin uses the shared endpoint and its own private credential file;
do not assume it changed just because MCP was configured. This release leaves
the workstation's existing local agentmemory and its records untouched.

A successful MCP call or direct hook test does not establish that native Codex
automatic capture is trusted and working. Verify it separately in a real trusted
agent session. Deployment acceptance does not perform agent sign-in or a paid
prompt, and does not claim native automatic capture has been verified without
that session.

## Check operation

From a provisioned guest, first check cloud-init and agent startup:

```sh
cloud-init status --wait
codex mcp list
claude mcp list
opencode api mcp.list
```

Run only commands for installed agents. OpenCode **2.0.31** may incorrectly show
`No MCP servers` for `opencode mcp list`, including a server added with its native
command. Use `opencode api mcp.list` to inspect the actual connected status; see
the [OpenCode v2 MCP documentation](https://dev.opencode.ai/v2/docs/mcp-servers/).
A native OpenCode `session.create` event was verified to reach the shared memory
as `session-start` without an LLM request or login (zero tokens). This verifies
that capture path; Codex native hooks still require the trust review above.

Inside an agent, inspect its available
memory tools, save a harmless unique test observation, retrieve it by its exact
text, then retrieve it from a second approved client. This checks the shared
backend rather than merely a configured MCP server name. Validate automatic
capture independently by ending a short supported agent session and searching
for its observation. Never paste the client key into terminal commands or chat.

The viewer can be reached through the trusted Proxmox SSH jump. On your
workstation, in a separate terminal:

```sh
ssh -N -J pxmx -o ExitOnForwardFailure=yes -L 127.0.0.2:3113:127.0.0.1:3113 -L 127.0.0.2:3112:127.0.0.1:3112 vmadmin@10.210.0.107
```

Then open `http://127.0.0.2:3113`. Both forwards are required: agentmemory 0.9.30
selects live-channel port 3112 from its health response, so forwarding the page
alone to a different port does not redirect the live channel. The alternate
loopback address leaves the existing services on `127.0.0.1` untouched. The server
allows the viewer origin `http://127.0.0.2:3113`; its local viewer proxy supplies
backend authentication. Never expose these tunnels on `0.0.0.0`.

Run SSH in the same operating system as the browser. Forwarding WSL's
`127.0.0.2` to a Windows browser has not been verified; native Windows OpenSSH can
bind the tunnels directly when using a Windows browser. HTTP page, live metadata,
snapshot/search and WebSocket transport were checked; visual browser behavior
has not been verified.

## Worker configuration and lifecycle

The optional `[agentmemory]` example is in [`config.toml`](../config/config.toml).
On pxmx, its relative paths resolve against `/etc/vmctl`. The management key is a
root-owned regular `0600` file; `agentmemory-known-hosts` pins the service VM's
verified SSH key. `agentmemory-ca.crt` is the verified public CA certificate.
Management uses a restricted SSH account/helper, not a public administration API.
The helper atomically updates its client registry and gateway configuration,
validates it before reload and rolls back failed changes.

Dry-run creates no token, access record or memory entry. The service is checked
before cloning. A failed create compensates only access/resources created by
that operation. Deleting a VM first revokes its individual access; if revocation
fails, deletion stops. Memory records remain. Cloning a VM with active memory
credentials is rejected to prevent copied keys inheriting access. The shared
memory server itself is a protected infrastructure VM.

A running agentmemory CLI can outlive a failed iii child process. The systemd
health-check timer probes the authenticated local API with bounded checks every
30 seconds, starting 90 seconds after boot, and restarts the service when the API
is unhealthy. It does not expose a health endpoint publicly. This complements
process restart policy; do not infer successful recovery solely from an active
CLI process. Reboot recovery remains a separate live acceptance check. Deployment
sources: [probe](../ops/agentmemory/healthcheck.py),
[oneshot service](../ops/agentmemory/vmctl-agentmemory-healthcheck.service) and
[timer](../ops/agentmemory/vmctl-agentmemory-healthcheck.timer).

For server administration, use the existing trusted SSH route and inspect the
systemd service status and journal. Do not print environment/secret files. Verify
that the data mount is present and the service survives restart. A running
process alone does not verify persistence, client authorization or retrieval.

## Existing local records: separate migration

This release does not migrate or modify the existing local agentmemory 0.9.28,
its files, MCP configuration or running processes. Migration is a separate task.

Use the upstream [export/import interface](https://github.com/rohitg00/agentmemory)
on a copy first. Establish whether it covers every stored record type, IDs,
relationships, projects and paths. Verify embedding compatibility and index
rebuilding. If the new server already has records, test merging both histories
rather than replacing its database. Compare counts and retrieval of known
observations before switching clients, and preserve the original local data as
a rollback copy. Do not copy a live database directory across versions blindly.
