"""Per-VM dashboard, rate boundaries and workstation clipboard checks."""

import asyncio
import subprocess
from dataclasses import replace
from ipaddress import IPv4Address
from threading import Event
from typing import Any

import pytest
from test_tui import FakeOperations, app_for
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Static

from vmctl.errors import VmctlError
from vmctl.models import IPObservation, VMMetricPoint, VMStats
from vmctl.tui.details import VMDetailsScreen
from vmctl.tui.formatting import details_text
from vmctl.tui.metrics import GapSparkline, bytes_text, metric_point
from vmctl.utils import clipboard


def counters(backend: FakeOperations, *, timestamp: float = 100) -> VMStats:
    return VMStats(
        backend.rows[0].vm,
        timestamp,
        cpu_percent=20,
        memory_bytes=1024**3,
        memory_total_bytes=2 * 1024**3,
        uptime_seconds=1000,
        pid=42,
        network_in_bytes=1000,
        network_out_bytes=2000,
        disk_read_bytes=3000,
        disk_write_bytes=4000,
    )


def test_rates_use_host_elapsed_time_and_correct_binary_units() -> None:
    previous = counters(FakeOperations())
    current = replace(previous, timestamp=105, network_in_bytes=6120, disk_read_bytes=13240)
    point = metric_point(current, previous)
    assert point.network_in_bytes_per_second == 1024
    assert point.disk_read_bytes_per_second == 2048
    assert bytes_text(point.network_in_bytes_per_second, rate=True) == "1.0 KiB/s"
    assert metric_point(current, None).network_in_bytes_per_second is None
    assert bytes_text(None, rate=True) == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"timestamp": 99},
        {"timestamp": 100},
        {"timestamp": float("inf")},
        {"pid": 99},
        {"uptime_seconds": 1},
        {"network_in_bytes": 1},
        {"disk_write_bytes": 1},
    ],
)
def test_reboots_counter_resets_and_clock_reversal_drop_rates(change: dict[str, Any]) -> None:
    previous = counters(FakeOperations())
    current = replace(previous, **({"timestamp": 105, "network_in_bytes": 2000} | change))
    point = metric_point(current, previous)
    assert point.network_in_bytes_per_second is None
    assert point.disk_read_bytes_per_second is None


def test_stopped_different_vm_and_missing_counters_do_not_produce_rates() -> None:
    previous = counters(FakeOperations())
    current = replace(previous, timestamp=105, network_in_bytes=2000)
    assert (
        metric_point(
            replace(current, vm=replace(current.vm, status="stopped")), previous
        ).network_in_bytes_per_second
        is None
    )
    assert (
        metric_point(
            replace(current, vm=replace(current.vm, vmid=999)), previous
        ).network_in_bytes_per_second
        is None
    )
    point = metric_point(replace(current, network_out_bytes=None), previous)
    assert point.network_out_bytes_per_second is None
    assert point.network_in_bytes_per_second == 200


@pytest.mark.parametrize(
    ("system", "release", "environment", "available", "arguments", "encoding"),
    [
        ("Linux", "6.6-microsoft", {}, "clip.exe", ("/bin/clip.exe",), "utf-16-le"),
        ("Linux", "6.6", {"WSL_INTEROP": "yes"}, "clip.exe", ("/bin/clip.exe",), "utf-16-le"),
        ("Windows", "11", {}, "clip", ("/bin/clip",), "utf-16-le"),
        ("Darwin", "23", {}, "pbcopy", ("/bin/pbcopy",), "utf-8"),
        ("Linux", "6", {"WAYLAND_DISPLAY": "wayland-0"}, "wl-copy", ("/bin/wl-copy",), "utf-8"),
        (
            "Linux",
            "6",
            {"DISPLAY": ":0"},
            "xclip",
            ("/bin/xclip", "-selection", "clipboard"),
            "utf-8",
        ),
        ("Linux", "6", {"DISPLAY": ":0"}, "xsel", ("/bin/xsel", "--clipboard", "--input"), "utf-8"),
    ],
)
def test_clipboard_os_selection(
    system: str,
    release: str,
    environment: dict[str, str],
    available: str,
    arguments: tuple[str, ...],
    encoding: str,
) -> None:
    command = clipboard.clipboard_command(
        system=system,
        release=release,
        environment=environment,
        find=lambda name: f"/bin/{name}" if name == available else None,
    )
    assert command == clipboard.ClipboardCommand(arguments, encoding)


def test_clipboard_unavailable_or_failed_requires_terminal_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        clipboard.clipboard_command(
            system="Linux", release="6", environment={}, find=lambda _: "/bin/xclip"
        )
        is None
    )
    monkeypatch.setattr(clipboard.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _: "/bin/pbcopy")
    captured: list[tuple[clipboard.ClipboardCommand, str]] = []

    def writer(command: clipboard.ClipboardCommand, text: str) -> None:
        captured.append((command, text))

    assert clipboard.copy_native("10.210.0.20", writer=writer)
    assert captured == [(clipboard.ClipboardCommand(("/bin/pbcopy",)), "10.210.0.20")]

    def failed(command: clipboard.ClipboardCommand, text: str) -> None:
        raise OSError("No clipboard")

    assert not clipboard.copy_native("10.210.0.20", writer=failed)


def test_clipboard_process_receives_only_stdin_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        returncode = 0

        def __init__(self, arguments: tuple[str, ...], **options: object) -> None:
            captured.append((arguments, options))
            self.input: bytes | None = None
            self.timeout: int | None = None
            self.killed = False

        def __enter__(self) -> "Process":
            return self

        def __exit__(self, *arguments: object) -> None:
            return None

        def communicate(
            self, data: bytes | None = None, *, timeout: int | None = None
        ) -> tuple[None, None]:
            self.input, self.timeout = data, timeout
            return None, None

        def kill(self) -> None:
            self.killed = True

    processes: list[Process] = []

    def popen(arguments: tuple[str, ...], **options: object) -> Process:
        process = Process(arguments, **options)
        processes.append(process)
        return process

    monkeypatch.setattr(clipboard.subprocess, "Popen", popen)
    command = clipboard.ClipboardCommand(("/bin/clip.exe",), "utf-16-le")
    clipboard.write_clipboard(command, "10.210.0.20")
    assert captured[0][0] == ("/bin/clip.exe",)
    assert "shell" not in captured[0][1]
    assert processes[0].input == "10.210.0.20".encode("utf-16-le")
    assert processes[0].timeout == 2

    class SlowProcess(Process):
        def communicate(
            self, data: bytes | None = None, *, timeout: int | None = None
        ) -> tuple[None, None]:
            if timeout is not None:
                raise subprocess.TimeoutExpired("clip.exe", timeout)
            return None, None

    slow = SlowProcess(("/bin/clip.exe",))
    monkeypatch.setattr(clipboard.subprocess, "Popen", lambda *args, **options: slow)
    with pytest.raises(OSError, match="timed out"):
        clipboard.write_clipboard(command, "10.210.0.20")
    assert slow.killed


def test_discovered_ip_source_is_visible_in_table_search_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.rows[0] = replace(
            backend.rows[0],
            ip=None,
            addresses=(IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),),
        )
        app = app_for(backend, read_only=True)
        copied: list[str] = []
        monkeypatch.setattr(app, "copy_ip", copied.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#vm-table", DataTable)
            assert table.get_cell_at(Coordinate(0, 6)) == "10.210.0.20"
            region = table._get_cell_region(Coordinate(0, 6))
            assert await pilot.click(table, offset=(region.x + 1, region.y))
            await pilot.pause()
            assert copied == ["10.210.0.20"]
            assert len(app.screen_stack) == 1
            assert "source: dhcp-lease" in details_text(backend.rows[0])
            region = table._get_cell_region(Coordinate(0, 1))
            assert await pilot.click(table, offset=(region.x + 1, region.y))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, VMDetailsScreen)
            assert "stats:104:True" in backend.calls
            assert "10.210.0.20" in str(app.screen.query_one("#vm-address", Static).render())
            await pilot.click("#copy-ip")
            await pilot.pause()
            assert copied == ["10.210.0.20", "10.210.0.20"]

    asyncio.run(scenario())


def test_clicking_second_row_uses_its_target_and_enter_opens_details() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.rows.append(
            replace(backend.rows[0], vm=replace(backend.rows[0].vm, vmid=200, name="second"))
        )
        app = app_for(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#vm-table", DataTable)
            region = table._get_cell_region(Coordinate(1, 1))
            await pilot.click(table, offset=(region.x + 1, region.y))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, VMDetailsScreen)
            assert app.screen.details.vm.vmid == app.selected_id == 200
            assert "stats:200:True" in backend.calls
            assert "stats:104:True" not in backend.calls
            await pilot.press("escape")
            await pilot.pause()
            table.focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, VMDetailsScreen)
            assert app.screen.details.vm.vmid == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 44)])
def test_details_history_live_rates_offline_and_close_cleanup(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        original = counters(backend)
        requested: list[tuple[str, bool]] = []

        def stats(reference: str, *, history: bool = False) -> VMStats:
            requested.append((reference, history))
            return (
                replace(
                    original,
                    history=(
                        VMMetricPoint(60, cpu_percent=10),
                        VMMetricPoint(80, cpu_percent=None),
                    ),
                )
                if history
                else replace(original, timestamp=105, network_in_bytes=6120)
            )

        backend.stats = stats
        app = app_for(backend)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_details()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, VMDetailsScreen)
            assert screen.has_class("narrow") == (size[0] < 110)
            assert requested == [("104", True)]
            assert screen.query_one("#cpu-graph", GapSparkline).values == (10, None, 20)
            assert screen.query_one("#cpu-graph", GapSparkline).fixed_maximum == 100
            screen.load()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "1.0 KiB/s" in str(screen.query_one("#network-in-value", Static).render())
            snapshot = screen.snapshot
            screen.failed(VmctlError("Connection unavailable"))
            assert screen.snapshot is snapshot
            assert screen.previous is None
            assert "STALE" in str(screen.query_one("#metrics-status", Static).render())
            assert not screen.query_one("#copy-ip", Button).disabled
            assert screen.query_one("#metrics-close", Button).region.bottom <= size[1]
            await pilot.click("#metrics-close")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert screen.closed
            before = len(requested)
            screen.load()
            await pilot.pause()
            assert len(requested) == before

    asyncio.run(scenario())


def test_pending_metrics_do_not_overlap_or_update_a_closed_screen() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        entered, release = Event(), Event()
        requests: list[str] = []

        def slow(reference: str, *, history: bool = False) -> VMStats:
            requests.append(reference)
            entered.set()
            assert release.wait(5)
            return counters(backend)

        backend.stats = slow
        app = app_for(backend)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_details()
            await pilot.pause()
            assert await asyncio.to_thread(entered.wait, 2)
            screen = app.screen
            assert isinstance(screen, VMDetailsScreen)
            try:
                screen.load()
                screen.load()
                assert requests == ["104"]
                assert not app.busy
                await pilot.click("#metrics-close")
                await pilot.pause()
                assert screen.closed
            finally:
                release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.snapshot is None
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_unknown_ip_copy_disabled_and_clipboard_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.rows[0] = replace(backend.rows[0], ip=None)
        app = app_for(backend)
        monkeypatch.setattr("vmctl.tui.app.copy_native", lambda _: False)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_details()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, VMDetailsScreen)
            assert app.screen.query_one("#copy-ip", Button).disabled
            app.copy_ip("10.210.0.20")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.clipboard == "10.210.0.20"

    asyncio.run(scenario())


def test_worker_statistics_for_another_vm_are_rejected() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        wrong = counters(backend)
        backend.stats = lambda reference, history=False: replace(
            wrong, vm=replace(wrong.vm, vmid=999)
        )
        app = app_for(backend)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_details()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, VMDetailsScreen)
            assert app.screen.snapshot is None
            status = str(app.screen.query_one("#metrics-status", Static).render())
            assert "STALE" in status and "different VM" in status

    asyncio.run(scenario())
