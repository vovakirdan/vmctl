"""Capture demo TUI screens without SSH, clipboard access or VM operations.

Run: uv run --with cairosvg python docs/render_screenshots.py
"""

from __future__ import annotations

import asyncio
import math
import time
from ipaddress import IPv4Address
from pathlib import Path

from textual.widgets import Checkbox, Input, Select

from vmctl.config import Preset, Template, load_config
from vmctl.errors import VmctlError
from vmctl.models import (
    VM,
    CreateRequest,
    CreateResult,
    IPObservation,
    VMDetails,
    VMMetricPoint,
    VMStats,
)
from vmctl.operations import (
    ActionPreview,
    ActionResult,
    Catalog,
    CreatePreview,
    DeletePreview,
    LifecycleAction,
    Progress,
    ValidationSummary,
)
from vmctl.services.catalog import build_catalog
from vmctl.tui.app import VmctlApp
from vmctl.tui.create import CreateScreen
from vmctl.tui.details import VMDetailsScreen
from vmctl.tui.features import FeatureChoices

ROOT = Path(__file__).resolve().parents[1]


class DemoOperations:
    def __init__(self) -> None:
        self.data = build_catalog(load_config(ROOT / "config"))
        self.rows = [
            VMDetails(
                VM(104, "surge-dev", "running", "demo", cpu=8, memory_mib=16384),
                {"cores": "8", "memory": "16384", "ciuser": "vmadmin"},
                {
                    "template": "ubuntu-server",
                    "desktop": "false",
                    "system_features": "qemu-agent",
                    "modules": "base,rust,node,go,python,llvm,cmake",
                },
                IPv4Address("10.210.0.105"),
            ),
            VMDetails(
                VM(105, "workstation", "running", "demo", cpu=4, memory_mib=8192),
                {"cores": "4", "memory": "8192", "ciuser": "vmadmin"},
                {
                    "template": "ubuntu-desktop",
                    "desktop": "true",
                    "system_features": "qemu-agent,desktop-rdp",
                    "username": "vmadmin",
                },
                IPv4Address("10.210.0.106"),
            ),
            VMDetails(
                VM(200, "existing-vm", "running", "demo", cpu=2, memory_mib=4096),
                {"cores": "2", "memory": "4096"},
                {},
                None,
                addresses=(IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),),
            ),
            VMDetails(
                VM(201, "compat-test", "stopped", "demo", cpu=2, memory_mib=2048),
                {"cores": "2", "memory": "2048"},
                {"template": "debian", "desktop": "false"},
                None,
            ),
        ]

    def list(self) -> list[VMDetails]:
        return self.rows

    def info(self, reference: str) -> VMDetails:
        return next(row for row in self.rows if str(row.vm.vmid) == reference)

    def stats(self, reference: str, *, history: bool = False) -> VMStats:
        vm = self.info(reference).vm
        timestamp = time.time()
        points = (
            tuple(
                VMMetricPoint(
                    timestamp - (60 - index) * 60,
                    cpu_percent=28 + 14 * math.sin(index / 3) + 5 * math.cos(index / 7),
                    memory_bytes=(8.5 + 0.8 * math.sin(index / 12)) * 1024**3,
                    memory_total_bytes=16 * 1024**3,
                    network_in_bytes_per_second=(1.8 + math.sin(index / 4)) * 1024**2,
                    network_out_bytes_per_second=(0.8 + 0.4 * math.cos(index / 5)) * 1024**2,
                    disk_read_bytes_per_second=(10 + 8 * math.sin(index / 6)) * 1024**2,
                    disk_write_bytes_per_second=(3 + 2.5 * math.cos(index / 3)) * 1024**2,
                )
                for index in range(61)
            )
            if history
            else ()
        )
        return VMStats(
            vm,
            timestamp,
            cpu_percent=37.2,
            memory_bytes=int(9.3 * 1024**3),
            memory_total_bytes=16 * 1024**3,
            uptime_seconds=86400,
            pid=4000,
            network_in_bytes=10**10,
            network_out_bytes=5 * 10**9,
            disk_read_bytes=2 * 10**10,
            disk_write_bytes=10**10,
            history=points,
            history_note="Demo data: previous hour minute averages; live samples every 5 s",
        )

    def catalog(self) -> Catalog:
        return self.data

    def templates(self) -> dict[str, Template]:
        return self.data.templates

    def presets(self) -> dict[str, Preset]:
        return self.data.presets

    def validate_config(self) -> ValidationSummary:
        return ValidationSummary(
            len(self.data.templates),
            len(self.data.presets),
            len(self.data.system_features),
            len(self.data.modules),
            len(self.data.profiles),
        )

    def plan_create(self, request: CreateRequest) -> CreatePreview:
        raise VmctlError("Screenshot demo does not execute VM requests")

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        raise VmctlError("Screenshot demo does not execute VM requests")

    def plan_delete(self, reference: str) -> DeletePreview:
        raise VmctlError("Screenshot demo does not execute VM requests")

    def delete(self, expected: DeletePreview) -> int:
        raise VmctlError("Screenshot demo does not execute VM requests")

    def plan_action(self, reference: str, action: LifecycleAction) -> ActionPreview:
        raise VmctlError("Screenshot demo does not execute VM requests")

    def action(self, expected: ActionPreview) -> ActionResult:
        raise VmctlError("Screenshot demo does not execute VM requests")


async def capture() -> None:
    import cairosvg

    def save(app: VmctlApp, name: str) -> None:
        svg = app.export_screenshot(title="vmctl · demonstration")
        destination = ROOT / "docs" / "screenshots" / f"{name}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        cairosvg.svg2png(bytestring=svg.encode(), write_to=str(destination))

    backend = DemoOperations()
    app = VmctlApp(backend, target="demo (fake worker)", public_key=lambda _: None, read_only=True)
    async with app.run_test(size=(140, 48)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        save(app, "dashboard")
        app.open_details()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, VMDetailsScreen)
        # Supply a second fake snapshot to demonstrate live B/s next to RRD history.
        from dataclasses import replace

        initial = screen.snapshot
        assert initial is not None
        screen.loaded(
            replace(
                initial,
                timestamp=initial.timestamp + 5,
                network_in_bytes=initial.network_in_bytes + 2 * 1024**2 * 5,
                network_out_bytes=initial.network_out_bytes + 1024**2 * 5,
                disk_read_bytes=initial.disk_read_bytes + 12 * 1024**2 * 5,
                disk_write_bytes=initial.disk_write_bytes + 3 * 1024**2 * 5,
                history=(),
            )
        )
        await pilot.pause()
        save(app, "vm-metrics")
        screen.dismiss(None)
        await pilot.pause()
        app.action_new()
        await pilot.pause()
        create = app.screen
        assert isinstance(create, CreateScreen)
        create.query_one("#vm-name", Input).value = "desktop-dev"
        create.query_one("#template", Select).value = "ubuntu-desktop"
        await pilot.pause()
        save(app, "create")
        await pilot.resize_terminal(140, 60)
        create.query_one("#dev-go", Checkbox).scroll_visible(top=True, animate=False)
        await pilot.pause()
        save(app, "features")
        choices = create.query_one(FeatureChoices)
        choices.selection.toggle("codex", True, system=False)
        choices.update_choices()
        create.query_one("#group-ai-cli").scroll_visible(top=True, animate=False)
        await pilot.pause()
        save(app, "ai-cli")
        create.query_one("#group-ai-desktop").scroll_visible(top=True, animate=False)
        await pilot.pause()
        save(app, "ai-desktop")


if __name__ == "__main__":
    asyncio.run(capture())
