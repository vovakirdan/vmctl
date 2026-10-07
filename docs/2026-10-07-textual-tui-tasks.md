# Textual TUI implementation

## Goal

Provide a workstation TUI for the existing SSH worker. Users browse VM state and create VMs using one form, with described checkboxes separated into system features and opt-in development modules.

## Approach

Reuse the typed Operations contract. Keep worker calls outside the UI thread. Resolve editable catalog dependencies with the existing engine; preserve desktop defaults and password handling. Add guarded lifecycle operations shared by CLI and TUI. No database, daemon, HTTP API or automated deployment.

## Skills

Python best practices, coding, subagent task execution, crafting effective READMEs.

## Tasks

1. Shared frontend contract (root): enrich CatalogItem with dependencies/members and Catalog with per-template system_defaults; share client settings/adapter and key loading independently of Typer; add Textual 8 dependency, CLI launch and lifecycle commands. Preserve portable imports and CLI test injection.
2. Lifecycle backend: implement plan_action(reference, action) -> ActionPreview and action(expected) -> ActionResult for start/shutdown/reboot. Protect templates, locks, changed targets and statuses under the host lock. Shutdown is graceful with a timeout and no forced fallback. Extend the typed SSH protocol/worker allowlist, preserve unknown-outcome semantics and test mocked execution. Backend owns proxmox/client.py, services/lifecycle.py, local.py, protocol.py, ssh.py, worker.py and lifecycle tests.
3. TUI: implement src/vmctl/tui and portable headless tests. Main screen has searchable VM table, details, refresh and actions. Creation form shows template/preset/resources, described system/development checkboxes and collapsible advanced options. Dependencies auto-select; incompatible choices explain why. Preview before creation, masked confirmed desktop password, busy guards, explicit confirmations, progress journal and read-only mode. Preserve VM selection and stale rows on errors. Test wide/narrow screens, independence, dependency selection, credentials, confirmations and refresh. No real host changes in tests.
4. Documentation: update both READMEs with equal commands and translated content, quickstart, upgrade of both client and worker, TUI keys/form/read-only behavior and lifecycle examples. Document limitations honestly.
5. Integration/review: full pytest, Ruff format/check, strict mypy, build; headless visual smoke and read-only live catalog/list when available. Review secret handling and mutation guards. Publish to the existing GitHub repository and report commands for the user to run/test.

## Technical details

LifecycleAction = Literal["start", "shutdown", "reboot"]. ActionPreview(vm: VM, fingerprint: str, action: LifecycleAction). ActionResult(vm: VM, action: LifecycleAction). CatalogItem.dependencies and members are tuples. Catalog.system_defaults maps template names to resolved system-feature names. TUI launch: vmctl tui [--read-only]. TUI construction receives Operations, target label and a public-key loader; it does not depend on Typer contexts. Frontend password hashes remain SecretStr and travel only on SSH stdin. Version: 0.3.0.

## Acceptance

Tests run without Proxmox. Server and desktop defaults remain distinct; development starts empty. All normal UI text/code comments are English. No password appears in logs, output, exception locals or persisted config. Unknown remote outcomes are shown without retrying. Worker upgrade is required for the new lifecycle/catalog fields; CI deployment remains deferred.

## Verification

All five tasks completed. Full suite: 301 tests passed, including 12 headless Textual tests. Ruff check and format passed (66 files); strict mypy passed (44 source files). Wheel and source distribution built; the wheel includes the Textual stylesheet and editable bootstrap defaults. The 44 fenced code blocks match between both READMEs.

The installed 0.3.0 client and Proxmox worker passed live read-only catalog/list and server/desktop creation preflights through `pxmx`. No VM was created, deleted or controlled during these checks. All 29 inspected VM, vmctl and dnsmasq configuration files retained their hashes. Lifecycle behavior was tested with mocks; actual guest boot, new GUI provisioning and RDP login remain user acceptance checks.
