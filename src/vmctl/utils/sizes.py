"""Binary size parsing; resource units remain explicit."""

import re
from decimal import Decimal

from vmctl.errors import VmctlError


def parse_size(value: str, *, unit: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)\s*(M|G|MiB|GiB)?", value, re.IGNORECASE)
    if match is None:
        raise VmctlError(f"Invalid size: {value!r}; use a positive integer with M/G or MiB/GiB")
    amount = int(match[1])
    suffix = (match[2] or unit).lower()
    mib = amount * (1024 if suffix in {"g", "gib"} else 1)
    if unit == "MiB":
        return mib
    if mib % 1024:
        raise VmctlError("Disk size must be a whole number of GiB")
    return mib // 1024


def proxmox_disk_gib(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT])", value, re.IGNORECASE)
    if match is None:
        raise VmctlError(f"Cannot parse Proxmox disk size: {value!r}")
    multiplier = {
        "K": Decimal(1) / 1048576,
        "M": Decimal(1) / 1024,
        "G": Decimal(1),
        "T": Decimal(1024),
    }
    gib = Decimal(match[1]) * multiplier[match[2].upper()]
    # Round up for presentation; compare requested sizes against the original bytes before resize.
    return int(gib.to_integral_value(rounding="ROUND_CEILING"))
