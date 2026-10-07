"""Parse per-VM Proxmox gauges, cumulative counters and averaged RRD history."""

import math
import re
import time
from dataclasses import replace

from vmctl.errors import VmctlError
from vmctl.models import VM, VMMetricPoint, VMStats
from vmctl.proxmox.client import ProxmoxClient


def nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def counter(value: object) -> int | None:
    number = nonnegative_number(value)
    if number is None or not number.is_integer():
        return None
    # Preserve exact integer counters instead of converting them through a float.
    return value if isinstance(value, int) else int(number)


def cpu_percent(value: object) -> float | None:
    number = nonnegative_number(value)
    return number * 100 if number is not None and number <= 1 else None


def parse_current(vm: VM, data: object, *, timestamp: float) -> VMStats:
    if not isinstance(data, dict):
        raise VmctlError("Proxmox returned an invalid VM status object")
    status = data.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", status):
        raise VmctlError("Proxmox returned an invalid VM status")
    reported_id = data.get("vmid")
    if reported_id is not None and (type(reported_id) is not int or reported_id != vm.vmid):
        raise VmctlError("Proxmox returned metrics for another VM")
    current_vm = replace(vm, status=status)
    total = counter(data.get("maxmem"))
    if not total:
        total = None
    if status != "running":
        return VMStats(current_vm, timestamp, memory_total_bytes=total)
    pid = counter(data.get("pid"))
    return VMStats(
        vm=current_vm,
        timestamp=timestamp,
        cpu_percent=cpu_percent(data.get("cpu")),
        memory_bytes=counter(data.get("mem")),
        memory_total_bytes=total,
        uptime_seconds=counter(data.get("uptime")),
        pid=pid if pid else None,
        network_in_bytes=counter(data.get("netin")),
        network_out_bytes=counter(data.get("netout")),
        disk_read_bytes=counter(data.get("diskread")),
        disk_write_bytes=counter(data.get("diskwrite")),
    )


def parse_history(data: object) -> tuple[VMMetricPoint, ...]:
    if not isinstance(data, list):
        raise VmctlError("Proxmox returned invalid VM history")
    points: dict[float, VMMetricPoint] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        timestamp = nonnegative_number(row.get("time"))
        if not timestamp:
            continue
        total = nonnegative_number(row.get("maxmem"))
        # RRD network/disk values are already rates; never differentiate them again.
        points[timestamp] = VMMetricPoint(
            timestamp=timestamp,
            cpu_percent=cpu_percent(row.get("cpu")),
            memory_bytes=nonnegative_number(row.get("mem")),
            memory_total_bytes=total if total else None,
            network_in_bytes_per_second=nonnegative_number(row.get("netin")),
            network_out_bytes_per_second=nonnegative_number(row.get("netout")),
            disk_read_bytes_per_second=nonnegative_number(row.get("diskread")),
            disk_write_bytes_per_second=nonnegative_number(row.get("diskwrite")),
        )
    return tuple(points[timestamp] for timestamp in sorted(points)[-120:])


def vm_stats(client: ProxmoxClient, reference: str, *, history: bool = False) -> VMStats:
    """Resolve a local VM and read only that VM's metrics; history failure is optional."""
    vm = client.resolve(reference)
    current = parse_current(vm, client.current_metrics(vm.vmid), timestamp=time.time())
    if current.vm.status == "running":
        try:
            cached_cpu = client.cached_cpu(vm.vmid)
        except (VmctlError, OSError):
            cached_cpu = None
        # Fresh pvesh processes initialize status/current CPU to zero, not a measured sample.
        current = replace(current, cpu_percent=cpu_percent(cached_cpu))
    if not history:
        return current
    try:
        points = parse_history(client.metric_history(vm.vmid))
    except (VmctlError, OSError):
        return replace(current, history_note="VM history is unavailable; showing current samples")
    if points and not any(
        any(
            value is not None
            for value in (
                point.cpu_percent,
                point.memory_bytes,
                point.network_in_bytes_per_second,
                point.network_out_bytes_per_second,
                point.disk_read_bytes_per_second,
                point.disk_write_bytes_per_second,
            )
        )
        for point in points
    ):
        return replace(
            current,
            history_note="VM history has no supported metric values; collecting current samples",
        )
    note = (
        "Previous hour: Proxmox minute averages; memory is reported by the host"
        if points
        else "VM history has no samples; collecting current samples"
    )
    return replace(current, history=points, history_note=note)
