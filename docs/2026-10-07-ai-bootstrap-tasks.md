# AI Bootstrap Task Breakdown

**Goal:** Install optional AI CLI agents and supported Linux desktop apps through editable bootstrap definitions, with automatic runtime dependencies and grouped frontend choices.

**Approach:** Reuse Module, the dependency resolver, cloud-init renderer and CatalogItem. Add optional group metadata (development/ai-cli/ai-desktop) and desktop_only validation to Module; defaults preserve old TOML and wire DTOs. CLI --with remains the single composable opt-in selection; system features stay separate. AI selections never become automatic template or surge-dev defaults.

**Skills:** brainstorming, python-best-practices, codebase-memory, ponytail, task-breakdown, subagent-task-execution, openai-docs, crafting-effective-readmes.

**Tech Details:** Python3.12+, Pydantic/dataclasses, Typer/Textual, editable TOML and guest-only scripts, npm per-user prefix with engine checks, native Claude installer, official desktop packages, pytest/mypy/Ruff, existing SSH worker. No installer, authentication, VM mutation or provider API call runs on the workstation or Proxmox host.

---

### Task 1: Shared domain and catalog metadata (root)

Modify bootstrap/models.py to add group metadata and desktop_only validation in Module. Append a defaulted group to CatalogItem in operations.py; populate it for modules in services/catalog.py. Keep desktop filtering in Module.implementation so create planning, rendering, catalogs, completion and TUI share it. Add tests for legacy defaults, editable group, unsupported combinations and server rejection before cloning. Root owns these files and CLI grouping, protocol compatibility tests, package version/lock, integration and publication.

### Task 2: Editable AI CLI definitions and installers (CLI worker)

Own config/bootstrap/modules/{ai-tools,codex,claude,opencode,gemini,aider}.toml and new CLI guest scripts only; tests/test_ai_cli.py. ai-tools provides minimal download/login-PATH prerequisites and one atomic GuestFile, reused as a dependency. Codex, OpenCode and Gemini npm installations require node explicitly; native Claude requires no Node; Aider uses python/uv. Install as configured non-root cloud user; no sudo npm, API keys, login, permissive agent modes or root HOME changes. Prefer official current stable defaults, state version channels and package names in TOML/script arguments. Read primary current docs/metadata; syntax/test installers with fake commands only. Do not change profiles.toml (root owns). Root adds ai-cli profile without changing surge-dev/full-dev.

### Task 3: Official desktop modules (desktop worker)

Own config/bootstrap/modules/{codex-desktop,claude-desktop,opencode-desktop}.toml, their new guest-only scripts and tests/test_ai_desktop.py. All require desktop_only=true, group=ai-desktop. Use official packages for verified distro/release/architecture combinations, without --no-sandbox or global display changes. Codex-desktop is the official ChatGPT app with Codex; documented Ubuntu24.04/noble, Ubuntu26.04/resolute and Debian13/trixie aliases only, not Rocky/Alpine. Claude Desktop official apt for modern Ubuntu/Debian. OpenCode official Linux desktop deb/rpm only where dependencies support the existing guest; do not assume Rocky9 compatibility. Package/key/repo configurations created by scripts must use same-filesystem temporary files and atomic replacement. Install but do not launch/authenticate GUI apps. Unsupported versions fail clearly. Do not edit shared domain or profiles.toml.

### Task 4: TUI grouping and portable tests (TUI worker)

Own tui/features.py and new tests/client/test_ai_tui.py, plus existing TUI tests only when directly needed. Separate Development, AI CLI agents, AI desktop apps and Profiles using CatalogItem.group. Preserve shared FeatureSelection.development/resolver, descriptions, dependencies, --with behavior and checkbox IDs. Desktop options unavailable on server/unsupported templates; switching templates clears invalid choices. Node becomes selected and locked only while required by a chosen module. No business rules hardcoded in widgets. Root owns CLI help/table grouping and screenshots.

### Task 5: Documentation, verification and delivery (root)

Update both READMEs, examples, screenshot fixture/captures and docs, version0.5.0/uv.lock. Include actual supported distro matrix and manual guest login commands; never transfer existing auth to clones. Run full mocked/headless suite, Ruff/type/build and meaningful independent review. Install matching local/pxmx worker wheels, config init preserves existing edits; add optional ai-cli/ai-desktop profiles atomically with backup. Read-only installed-tool catalog/preflight/TUI checks only. Publish to authorized repo and verify native CI. No live VM create/delete/lifecycle or agent installers during development.

## Discovery evidence

Graph project vmctl, generation2026-10-07T15:35:12Z: Module/Definition.implementation is shared by select_bootstrap, renderer and catalog support checks; FeatureSelection resolves CatalogItem.dependencies and profiles. Exact relevant paths checked; renderer.py/operations.py metadata changed since graph and were read directly. Guest scripts are excluded from graph and read directly. Current official pages: learn.chatgpt.com/docs/codex/cli and docs/linux/linux-app; code.claude.com/docs/en/setup; support.claude.com/en/articles/10065433-install-claude-desktop; opencode.ai/docs and /v2/docs; geminicli.com/docs/get-started/installation. OpenCode v2/latest package choice must be checked against official download page and registry; do not assume an old npm name is current.

## Verification results

- 484 full mocked/headless tests passed. Ruff check/format passed for 85 Python files; strict mypy passed for 50 source files. Wheel and sdist include all nine AI definitions and five guest-only scripts.
- Independent review found option-looking version arguments could be interpreted by su. The shared renderer now rejects leading '-' versions; all three CLI scripts use options, an explicit '--', then user and positional shell arguments. GNU/BusyBox source and harmless shell probes verified binding. No privileged su or installer was executed.
- Official npm metadata confirms OpenCode @opencode/cli latest2.0.24 (separate beta/dev tags). Official DEB metadata was checked for architecture/package/version; the Claude signing key fingerprint was checked in an isolated temporary GPG home. The actual OpenCode desktop Exec is absolute /opt/OpenCode/ai.opencode.desktop, so the npm CLI does not shadow menu/URI launches. Read-only downloads do not prove a completed guest installation.
- Six sanitized TUI screenshots are reproducible; the AI CLI/desktop groups are visible, and all 48 README code blocks match between English and Russian.
- Matching 0.5.0 wheel SHA256 af7bad1fd261910996b5be3ce741b6bfc40644e2cc6e4af97eec9fb95f4db1b5 installed on this WSL client and pxmx worker. config init added14files and kept19existingfiles. Only missing ai-cli/ai-desktop aliases were added atomically; existing profiles and settings preserved with /etc/vmctl/.profiles.toml.before-0.5.0.bak backup.
- Installed-tool config validate, server CLI-agent dry-run and desktop-profile dry-run succeeded over SSH. Live read-only headless TUI verified 17modules/4profiles, initially empty AI selections, required Node checkbox locking and desktop/server gating at wide and80x24sizes. All13VM/storage/dnsmasq file hashes remained unchanged. No VM creation/deletion/lifecycle, guest installer, GUI launch, authentication or provider request was performed.
- Native Windows/macOS/Linux client CI and the Linux full suite are checked after publication; local tests and read-only package metadata do not replace live guest/GUI acceptance.
