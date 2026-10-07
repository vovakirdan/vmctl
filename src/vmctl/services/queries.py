"""Read-only VM views and metadata lookup."""

from vmctl.models import VM, VMDetails
from vmctl.network.discovery import (
    AddressSnapshot,
    discover_addresses,
    network_bindings,
    read_address_snapshot,
)
from vmctl.network.dnsmasq import DnsmasqReservations, Reservation
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata


def info_vm(client: ProxmoxClient, reservations: DnsmasqReservations, reference: str) -> VMDetails:
    vm = client.resolve(reference)
    return _details(client, vm, reservations.read(), read_address_snapshot(client))


def list_vms(client: ProxmoxClient, reservations: DnsmasqReservations) -> list[VMDetails]:
    entries = reservations.read()
    snapshot = read_address_snapshot(client)
    result: list[VMDetails] = []
    for vm in sorted(client.list_vms(), key=lambda vm: vm.vmid):
        if not vm.template:
            result.append(_details(client, vm, entries, snapshot))
    return result


def _details(
    client: ProxmoxClient, vm: VM, entries: list[Reservation], snapshot: AddressSnapshot
) -> VMDetails:
    config = client.vm_config(vm.vmid)
    macs = set(network_bindings(config, client.config.host.network.bridge).values())
    reservation = next(
        (entry for entry in entries if entry.mac in macs and entry.vmid in {None, vm.vmid}), None
    )
    addresses, notes = discover_addresses(client, vm, config, snapshot)
    return VMDetails(
        vm,
        config,
        decode_metadata(config.get("description", "")),
        reservation.ip if reservation else None,
        addresses,
        notes,
    )
