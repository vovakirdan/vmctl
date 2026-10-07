"""Current counter rates and gap-aware rendering for individual VM metrics."""

from collections.abc import Sequence
from math import isfinite

from rich.text import Text
from textual.widgets import Sparkline

from vmctl.models import VMMetricPoint, VMStats


def metric_point(current: VMStats, previous: VMStats | None) -> VMMetricPoint:
    """A first sample, reboot or discontinuity never implies a zero-byte rate."""
    valid = (
        previous is not None
        and current.vm.vmid == previous.vm.vmid
        and current.vm.status == previous.vm.status == "running"
        and isfinite(current.timestamp)
        and isfinite(previous.timestamp)
        and current.timestamp > previous.timestamp
        and not (
            current.pid is not None and previous.pid is not None and current.pid != previous.pid
        )
        and not (
            current.uptime_seconds is not None
            and previous.uptime_seconds is not None
            and current.uptime_seconds < previous.uptime_seconds
        )
    )
    counters = (
        "network_in_bytes",
        "network_out_bytes",
        "disk_read_bytes",
        "disk_write_bytes",
    )
    if valid and previous is not None:
        valid = all(
            getattr(current, name) is None
            or getattr(previous, name) is None
            or getattr(current, name) >= getattr(previous, name)
            for name in counters
        )

    def rate(name: str) -> float | None:
        before: int | None = getattr(previous, name) if previous else None
        after: int | None = getattr(current, name)
        if not valid or before is None or after is None or previous is None:
            return None
        return (after - before) / (current.timestamp - previous.timestamp)

    return VMMetricPoint(
        timestamp=current.timestamp,
        cpu_percent=current.cpu_percent,
        memory_bytes=current.memory_bytes,
        memory_total_bytes=current.memory_total_bytes,
        network_in_bytes_per_second=rate("network_in_bytes"),
        network_out_bytes_per_second=rate("network_out_bytes"),
        disk_read_bytes_per_second=rate("disk_read_bytes"),
        disk_write_bytes_per_second=rate("disk_write_bytes"),
    )


def bytes_text(value: float | int | None, *, rate: bool = False) -> str:
    if value is None:
        return "unavailable"
    quantity = float(value)
    unit = "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if quantity < 1024 or unit == "TiB":
            break
        quantity /= 1024
    return f"{quantity:.1f} {unit}" + ("/s" if rate else "")


def memory_percent(point: VMMetricPoint) -> float | None:
    if point.memory_bytes is None or not point.memory_total_bytes:
        return None
    return min(100.0, max(0.0, point.memory_bytes / point.memory_total_bytes * 100))


class GapSparkline(Sparkline):
    """Sparkline with visible gaps and optional fixed percentage scale."""

    def __init__(self, *, fixed_maximum: float | None = None, id: str) -> None:
        super().__init__(id=id)
        self.values: tuple[float | None, ...] = ()
        self.fixed_maximum = fixed_maximum

    def set_values(self, values: Sequence[float | None]) -> None:
        self.values = tuple(value if value is None or isfinite(value) else None for value in values)
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width)
        values = self.values
        known = [value for value in values if value is not None]
        maximum = self.fixed_maximum or max(known, default=1) or 1
        result = Text()
        if not values or not known:
            return Text("Data unavailable", style="dim")
        bars = "▁▂▃▄▅▆▇█"
        for column in range(width):
            start = column * len(values) // width
            end = max(start + 1, (column + 1) * len(values) // width)
            bucket = values[start:end]
            if any(value is None for value in bucket):
                result.append("·", style="dim")
                continue
            numeric = [value for value in bucket if value is not None]
            ratio = min(1.0, max(0.0, sum(numeric) / len(numeric) / maximum))
            result.append(bars[round(ratio * 7)], style="cyan")
        return result
