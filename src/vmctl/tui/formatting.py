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
        f"IP: {details.display_ip or 'unknown'} (source: {details.ip_source})",
        f"Desktop: {'yes' if details.desktop else 'no' if details.desktop is False else 'unknown'}",
        "SPICE display: " + ("enabled (configuration)" if details.spice_enabled else "disabled"),
        "",
        "System features: " + (", ".join(details.system_features) or "none / unknown"),
        "Optional modules: " + (", ".join(details.modules) or "none / unknown"),
    ]
    if details.addresses:
        lines.append(
            "Observed addresses: "
            + ", ".join(f"{item.address} ({item.source})" for item in details.addresses)
        )
    if details.ip_notes:
        lines += ["Address notes: " + "; ".join(details.ip_notes)]
    if details.display_ip:
        lines += ["", f"SSH: ssh {username}@{details.display_ip}"]
    if details.rdp_enabled:
        lines += ["", f"RDP: {details.rdp_address or 'address unknown'}", f"User: {username}"]
    if "fileshare" in details.system_features:
        lines += [
            "",
            f"Fileshare: {details.metadata.get('fileshare_endpoint', 'unknown')}",
            f"Account: {details.metadata.get('fileshare_account', 'unknown')}",
            "Own directory: files:" + details.metadata.get("fileshare_directory", "unknown"),
            "Access: read shared; write/delete own directory (default; server policy may differ).",
            "Client: vmctl-files (rclone; no mount)",
        ]
    if "agentmemory" in details.system_features:
        lines += [
            "",
            f"Shared memory: {details.metadata.get('agentmemory_endpoint', 'unknown')}",
            f"Client ID: {details.metadata.get('agentmemory_client_id', 'unknown')}",
            "Agent adapters requested: " + (details.metadata.get("agentmemory_adapters") or "none"),
            "Access: shared read/write; guest setup not verified.",
        ]
    return "\n".join(lines)


def preview_text(preview: CreatePreview) -> str:
    lines = [
        f"Template VMID: {preview.template_vmid}   Bridge: {preview.bridge}",
        f"CPU: {preview.resources.cpu} vCPU   RAM: {preview.resources.memory_mib / 1024:g} GiB   Disk: {max(preview.resources.disk_gib, preview.template_disk_gib)} GiB",
        "Disk is never shrunk; larger template disks are retained.",
        "System features: " + (", ".join(preview.system_features) or "none"),
        "Optional modules: " + (", ".join(preview.modules) or "none"),
        f"Cloud-init user: {preview.username}",
        "Desktop password will be requested."
        if preview.requires_desktop_password
        else "Desktop password setup is skipped; GUI/RDP needs another authentication setup."
        if preview.desktop
        else "Desktop password is not required for this server template.",
    ]
    if "fileshare" in preview.system_features:
        lines += [
            "",
            f"Fileshare: {preview.fileshare_endpoint or 'configured SFTP endpoint'}",
            "Access: read shared files; write/delete only in this VM's own directory.",
            "Own directory is allocated using the VM name, VMID and creation ID.",
            "Client: vmctl-files (rclone; no mount). Preview does not create credentials.",
        ]
    if "agentmemory" in preview.system_features:
        lines += [
            "",
            f"Shared memory: {preview.agentmemory_endpoint or 'configured HTTPS endpoint'}",
            "Agent adapters requested: " + (", ".join(preview.agentmemory_adapters) or "none"),
            "Access: shared read/write. Preview creates no credentials or memory records.",
        ]
    return "\n".join(lines)


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
    if result.fileshare_endpoint:
        lines += [
            "",
            f"Fileshare: {result.fileshare_endpoint}",
            f"Account: {result.fileshare_account}",
            f"Own directory: files:{result.fileshare_directory}",
            "Access: read shared files; write/delete only in own directory (default).",
            "Client: vmctl-files (rclone; no mount)",
        ]
    if result.agentmemory_endpoint:
        lines += [
            "",
            f"Shared memory: {result.agentmemory_endpoint}",
            f"Client ID: {result.agentmemory_client_id}",
            "Agent adapters requested: " + (", ".join(result.agentmemory_adapters) or "none"),
            "Access: shared read/write; guest setup not verified.",
        ]
    lines += ["", "Guest bootstrap completion has not been verified."]
    return "\n".join(lines)
