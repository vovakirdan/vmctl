"""Plain-text presentation without exposing credentials or cloud-init data."""

from vmctl.errors import VmctlError
from vmctl.models import CreateResult, VMDetails
from vmctl.operations import CreatePreview


def error_text(error: Exception) -> str:
    return (
        str(error)
        if isinstance(error, VmctlError)
        else "Unexpected operation error. Check the client/worker installation and configuration."
    )


def details_text(details: VMDetails) -> str:
    vm = details.vm
    username = details.metadata.get("username") or details.config.get("ciuser", "vmadmin")
    lines = [
        f"VMID: {vm.vmid}   Name: {vm.name}",
        f"Status: {vm.status}   Node: {vm.node}",
        f"Template: {details.metadata.get('template', 'unknown')}",
        f"CPU: {vm.cpu} vCPU   RAM: {vm.memory_mib / 1024:g} GiB",
        f"IP: {details.ip or 'unmanaged'}",
        f"Desktop: {'yes' if details.desktop else 'no' if details.desktop is False else 'unknown'}",
        "",
        "System features: " + (", ".join(details.system_features) or "none / unknown"),
        "Development modules: " + (", ".join(details.modules) or "none / unknown"),
    ]
    if details.ip:
        lines += ["", f"SSH: ssh {username}@{details.ip}"]
    if details.rdp_enabled:
        lines += ["", f"RDP: {details.rdp_address or 'address unknown'}", f"User: {username}"]
    return "\n".join(lines)


def preview_text(preview: CreatePreview) -> str:
    return "\n".join(
        [
            f"Template VMID: {preview.template_vmid}   Bridge: {preview.bridge}",
            f"CPU: {preview.resources.cpu} vCPU   RAM: {preview.resources.memory_mib / 1024:g} GiB   Disk: {max(preview.resources.disk_gib, preview.template_disk_gib)} GiB",
            "Disk is never shrunk; larger template disks are retained.",
            "System features: " + (", ".join(preview.system_features) or "none"),
            "Development modules: " + (", ".join(preview.modules) or "none"),
            f"Cloud-init user: {preview.username}",
            "Desktop password will be requested."
            if preview.requires_desktop_password
            else "Desktop password setup is skipped; GUI/RDP needs another authentication setup."
            if preview.desktop
            else "Desktop password is not required for this server template.",
        ]
    )


def result_text(result: CreateResult) -> str:
    lines = [
        "VM created successfully",
        "",
        f"VMID: {result.vmid}",
        f"Name: {result.name}",
        f"IP: {result.ip}",
        f"Status: {'started' if result.started else 'stopped'}",
        f"Guest readiness: {result.readiness}",
        "",
        f"SSH: ssh {result.username}@{result.ip}",
    ]
    if result.rdp_enabled:
        lines += ["", f"RDP: {result.ip}:3389", f"User: {result.username}"]
    lines += ["", "Guest bootstrap completion has not been verified."]
    return "\n".join(lines)
