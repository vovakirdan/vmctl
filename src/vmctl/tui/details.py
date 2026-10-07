"""A selected VM's metrics, loaded only while its details screen is open."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, cast

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Collapsible, Label, Static

from vmctl.errors import VmctlError
from vmctl.models import VMDetails, VMMetricPoint, VMStats
from vmctl.tui.formatting import details_text, error_text
from vmctl.tui.metrics import GapSparkline, bytes_text, memory_percent, metric_point

if TYPE_CHECKING:
    from vmctl.tui.app import VmctlApp


class MetricCard(Vertical):
    def __init__(self, name: str, title: str, *, percentage: bool = False) -> None:
        super().__init__(classes="metric-card")
        self.metric_name, self.metric_title, self.percentage = name, title, percentage

    def compose(self) -> ComposeResult:
        yield Label(self.metric_title, classes="metric-title")
        yield Static("Waiting for data...", id=f"{self.metric_name}-value", markup=False)
        yield GapSparkline(
            id=f"{self.metric_name}-graph", fixed_maximum=100 if self.percentage else None
        )


class VMDetailsScreen(ModalScreen[None]):
    POLL_SECONDS = 5

    def __init__(self, details: VMDetails) -> None:
        super().__init__()
        self.details = details
        self.previous: VMStats | None = None
        self.snapshot: VMStats | None = None
        self.points: list[VMMetricPoint] = []
        self.in_flight = False
        self.closed = False
        self.last_success: datetime | None = None
        self.timer: Timer | None = None

    @property
    def vmctl(self) -> "VmctlApp":
        return cast("VmctlApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog metrics-dialog"):
            yield Label(
                f"VM {self.details.vm.vmid} · {self.details.vm.name}",
                classes="dialog-title",
                markup=False,
            )
            with Horizontal(id="address-toolbar"):
                yield Static(self.address_text(), id="vm-address", markup=False)
                yield Button("Copy IP", id="copy-ip", disabled=self.details.display_ip is None)
            yield Static("Loading this VM's statistics...", id="metrics-status", markup=False)
            with VerticalScroll(id="metrics-scroll"):
                yield Static(
                    "CPU: cached Proxmox sample, 0–100% of assigned vCPUs. "
                    "Memory: host-reported VM usage. "
                    "Network and disk: bytes per second, not capacity.",
                    classes="metrics-note",
                )
                yield Static(
                    "History: last hour, 1 min averages; live: every 5 s. "
                    "Sample spacing varies; dots mark unavailable values.",
                    id="history-note",
                    classes="metrics-note",
                    markup=False,
                )
                with Grid(id="metrics-grid"):
                    yield MetricCard("cpu", "CPU · fixed 0–100%", percentage=True)
                    yield MetricCard("memory", "Memory · fixed 0–100%", percentage=True)
                    yield MetricCard("network-in", "Network receive · auto scale")
                    yield MetricCard("network-out", "Network transmit · auto scale")
                    yield MetricCard("disk-read", "Disk read · auto scale")
                    yield MetricCard("disk-write", "Disk write · auto scale")
                with Collapsible(title="VM configuration and address sources", collapsed=True):
                    yield Static(details_text(self.details), id="vm-metadata", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Refresh", id="metrics-refresh")
                yield Button("Close", id="metrics-close", variant="primary")

    def address_text(self) -> str:
        return f"IP: {self.details.display_ip or 'unknown'} · {self.details.ip_source}"

    def on_mount(self) -> None:
        self.set_class(self.app.size.width < 110, "narrow")
        self.query_one("#metrics-close", Button).focus()
        self.timer = self.set_interval(self.POLL_SECONDS, self.load)
        self.load(history=True)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 110, "narrow")

    def on_unmount(self) -> None:
        self.closed = True
        if self.timer is not None:
            self.timer.stop()

    def load(self, *, history: bool = False) -> None:
        if self.closed or self.in_flight or self.vmctl.busy:
            return
        self.in_flight = True
        self.query_one("#metrics-refresh", Button).disabled = True
        app = self.vmctl
        reference = str(self.details.vm.vmid)

        def run() -> None:
            try:
                result = app.backend.stats(reference, history=history)
                if result.vm.vmid != self.details.vm.vmid:
                    raise VmctlError("The worker returned statistics for a different VM")
            except Exception as error:
                if not self.closed and app.is_running:
                    app.call_from_thread(self.failed, error)
            else:
                if not self.closed and app.is_running:
                    app.call_from_thread(self.loaded, result)

        self.run_worker(run, thread=True, exit_on_error=False, name=f"VM {reference} statistics")

    def loaded(self, result: VMStats) -> None:
        if self.closed or not self.is_mounted:
            return
        self.in_flight = False
        self.query_one("#metrics-refresh", Button).disabled = False
        if result.history:
            self.points = list(result.history)
        point = metric_point(result, self.previous)
        self.points.append(point)
        self.points = self.points[-120:]
        self.previous = self.snapshot = result
        self.last_success = datetime.now()
        uptime = f" | Uptime {result.uptime_seconds}s" if result.uptime_seconds is not None else ""
        self.query_one("#metrics-status", Static).update(
            f"{result.vm.status.capitalize()} | Updated {self.last_success:%H:%M:%S}{uptime}"
        )
        if result.history_note:
            self.query_one("#history-note", Static).update(
                result.history_note + " | Live every 5 s; dots mark gaps. Sample spacing varies."
            )
        self.render_metrics(point)

    def failed(self, error: Exception) -> None:
        if self.closed or not self.is_mounted:
            return
        self.in_flight = False
        self.query_one("#metrics-refresh", Button).disabled = False
        last = (
            f" | Last update {self.last_success:%H:%M:%S}"
            if self.last_success is not None
            else " | No metrics loaded"
        )
        self.query_one("#metrics-status", Static).update("STALE: " + error_text(error) + last)
        # A failed poll breaks the rate series; the last snapshot remains visible.
        if self.previous is not None:
            self.points.append(VMMetricPoint(self.previous.timestamp))
            self.points = self.points[-120:]
        self.previous = None

    def render_metrics(self, point: VMMetricPoint) -> None:
        cpu_text = f"{point.cpu_percent:.1f}%" if point.cpu_percent is not None else "unavailable"
        memory_text = (
            f"{bytes_text(point.memory_bytes)} / {bytes_text(point.memory_total_bytes)}"
            if point.memory_bytes is not None
            else "unavailable"
        )
        metrics: tuple[tuple[str, str, Callable[[VMMetricPoint], float | None]], ...] = (
            ("cpu", cpu_text, lambda item: item.cpu_percent),
            ("memory", memory_text, memory_percent),
            (
                "network-in",
                bytes_text(point.network_in_bytes_per_second, rate=True),
                lambda item: item.network_in_bytes_per_second,
            ),
            (
                "network-out",
                bytes_text(point.network_out_bytes_per_second, rate=True),
                lambda item: item.network_out_bytes_per_second,
            ),
            (
                "disk-read",
                bytes_text(point.disk_read_bytes_per_second, rate=True),
                lambda item: item.disk_read_bytes_per_second,
            ),
            (
                "disk-write",
                bytes_text(point.disk_write_bytes_per_second, rate=True),
                lambda item: item.disk_write_bytes_per_second,
            ),
        )
        for name, value, extract in metrics:
            self.query_one(f"#{name}-value", Static).update(value)
            self.query_one(f"#{name}-graph", GapSparkline).set_values(
                [extract(item) for item in self.points]
            )

    @on(Button.Pressed, "#copy-ip")
    def copy_ip(self, event: Button.Pressed) -> None:
        event.stop()
        if self.details.display_ip is not None:
            self.vmctl.copy_ip(str(self.details.display_ip))

    @on(Button.Pressed, "#metrics-refresh")
    def refresh_metrics(self, event: Button.Pressed) -> None:
        event.stop()
        self.load(history=self.snapshot is None)

    @on(Button.Pressed, "#metrics-close")
    def close(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)
