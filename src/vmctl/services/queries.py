"""Read-only VM views and metadata lookup."""

from vmctl.models import VMDetails
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata, network_mac


def info_vm(client: ProxmoxClient, reservations: DnsmasqReservations, reference: str) -> VMDetails:
    vm = client.resolve(reference)
    config = client.vm_config(vm.vmid)
    mac = network_mac(config["net0"]) if "net0" in config else None
    reservation = next((entry for entry in reservations.read() if entry.mac == mac), None)
    return VMDetails(
        vm,
        config,
        decode_metadata(config.get("description", "")),
        reservation.ip if reservation else None,
    )


def list_vms(client: ProxmoxClient, reservations: DnsmasqReservations) -> list[VMDetails]:
    entries = reservations.read()
    result: list[VMDetails] = []
    for vm in sorted(client.list_vms(), key=lambda vm: vm.vmid):
        if not vm.template:
            config = client.vm_config(vm.vmid)
            mac = network_mac(config["net0"]) if "net0" in config else None
            reservation = next((entry for entry in entries if entry.mac == mac), None)
            result.append(
                VMDetails(
                    vm,
                    config,
                    decode_metadata(config.get("description", "")),
                    reservation.ip if reservation else None,
                )
            )
    return result
