"""Errors suitable for presentation by any frontend."""


class VmctlError(Exception):
    """An actionable configuration or operation error."""


class CommandError(VmctlError):
    """A failed or timed-out external command."""

    def __init__(self, executable: str, detail: str, *, timed_out: bool = False) -> None:
        self.timed_out = timed_out
        super().__init__(f"{executable}: {detail}")


class CreationError(VmctlError):
    """Creation failed; compensation details are included in the message."""


class PartialDeleteError(VmctlError):
    """The VM was destroyed, but associated resource cleanup failed."""


class RepeatedRequestError(VmctlError):
    def __init__(self, request_id: str, vmid: int) -> None:
        self.request_id, self.vmid = request_id, vmid
        super().__init__(
            f"Request {request_id} already belongs to VM {vmid}; inspect it before continuing"
        )


class ProtocolError(VmctlError):
    """An incompatible or invalid worker message."""


class UnknownOutcomeError(VmctlError):
    def __init__(self, request_id: str, vmid: int | None = None) -> None:
        self.request_id, self.vmid = request_id, vmid
        target = f"VM {vmid}" if vmid is not None else "Proxmox inventory and tasks"
        super().__init__(
            f"Operation outcome is unknown (request {request_id}). Inspect {target} "
            "with vmctl info/list before retrying; no automatic retry was performed"
        )


class UncertainOperationError(VmctlError):
    """A submitted host action may continue after its command process timed out."""

    def __init__(self, vmid: int, operation: str) -> None:
        self.vmid = vmid
        super().__init__(
            f"Outcome of {operation} for VM {vmid} is uncertain after a Proxmox command timeout. "
            "Inspect VM state and tasks before retrying; no automatic retry was performed"
        )
