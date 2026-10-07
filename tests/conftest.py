"""Isolated configuration and a stateful fake of the host command boundary."""

import base64
import json
import shutil
import struct
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote

import pytest

from vmctl.config import Configuration, load_config
from vmctl.errors import CommandError
from vmctl.utils.subprocess import CommandResult


@pytest.fixture
def config(tmp_path: Path) -> Configuration:
    root = tmp_path / "config"
    shutil.copytree(Path(__file__).parents[1] / "config", root)
    network = tmp_path / "dnsmasq.d"
    network.mkdir()
    dnsmasq = tmp_path / "dnsmasq.conf"
    dnsmasq.write_text(f"conf-dir={network}\n")
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    key = tmp_path / "test.pub"
    key.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " test\n")
    path = root / "config.toml"
    text = path.read_text().replace('"/root/.ssh/id_ed25519.pub"', json.dumps(str(key)))
    text = text.replace('"/run/lock/vmctl.lock"', json.dumps(str(tmp_path / "vmctl.lock")))
    text = text.replace(
        '"/etc/dnsmasq.d/vmctl-hosts.conf"', json.dumps(str(network / "vmctl-hosts.conf"))
    )
    text = text.replace('"/var/lib/misc/dnsmasq.leases"', json.dumps(str(tmp_path / "leases")))
    text = text.replace('"/etc/dnsmasq.conf"', json.dumps(str(dnsmasq)))
    text = text.replace("probe = true", "probe = false")
    text = text.replace("[proxmox]", '[proxmox]\nnode = "test-node"')
    path.write_text(text)
    return load_config(root)


class FakeRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, ...]] = []
        self.failure: Callable[[Sequence[str]], bool] | None = None
        self.fail_once = True
        self.clone_timeout = False
        self.mac = "BC:24:11:AA:BB:CC"
        self.vms: dict[int, dict[str, str]] = {
            vmid: {
                "name": name,
                "template": "1",
                "scsi0": "local-lvm:template-disk,size=10G",
                "ide2": "local-lvm:cloudinit,media=cdrom",
                "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
                "cores": "1",
                "sockets": "2",
                "memory": "1024",
                "boot": "order=scsi0;ide2",
                "ciupgrade": "1",
            }
            for vmid, name in [
                (9000, "ubuntu-server"),
                (9001, "ubuntu-desktop"),
                (9010, "debian"),
                (9020, "alpine"),
                (9030, "rocky"),
            ]
        }
        self.status: dict[int, str] = {vmid: "stopped" for vmid in self.vms}
        self.nextid = 104
        self.arp_codes: dict[str, int] = {}

    def inventory(self) -> list[dict[str, object]]:
        return [
            {
                "type": "qemu",
                "vmid": vmid,
                "name": c["name"],
                "node": "test-node",
                "template": int(c.get("template", "0")),
                "status": self.status.get(vmid, "stopped"),
                "maxcpu": int(c.get("cores", "1")),
                "maxmem": int(c.get("memory", "1024")) * 1048576,
            }
            for vmid, c in self.vms.items()
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        if self.failure and self.failure(args):
            if self.fail_once:
                self.failure = None
            raise CommandError(args[0], "Injected failure")
        if args[0] == "pvesh":
            endpoint = args[2]
            if endpoint == "/cluster/resources":
                return CommandResult(json.dumps(self.inventory()))
            if endpoint == "/cluster/nextid":
                return CommandResult(
                    json.dumps(
                        int(args[args.index("--vmid") + 1]) if "--vmid" in args else self.nextid
                    )
                )
            if endpoint.startswith("/storage/"):
                return CommandResult(
                    json.dumps(
                        {"type": "dir", "content": "iso,backup,snippets", "path": str(self.root)}
                    )
                )
            if endpoint.endswith("/status"):
                return CommandResult('{"active": 1}')
        if args[0] == "pvesm":
            return CommandResult(str(self.root / "snippets" / args[2].split("/")[-1]) + "\n")
        if args[0] == "qm":
            verb = args[1]
            vmid = int(args[3]) if verb in {"disk", "cloudinit"} else int(args[2])
            if verb == "clone":
                clone_id = int(args[3])
                clone = dict(self.vms[vmid])
                clone.pop("template")
                clone["name"] = args[args.index("--name") + 1]
                clone["description"] = args[args.index("--description") + 1]
                self.vms[clone_id] = clone
                self.status[clone_id] = "stopped"
                if self.clone_timeout:
                    raise CommandError("qm", "clone timeout", timed_out=True)
                if stream:
                    stream("Clone complete")
                return CommandResult("")
            if verb == "config":
                config = self.vms[vmid]
                return CommandResult(
                    "\n".join(
                        f"{key}: {quote(value, safe='') if key == 'description' else value}"
                        for key, value in config.items()
                    )
                )
            if verb == "set":
                iterator = iter(args[3:])
                for option, value in zip(iterator, iterator, strict=True):
                    if option == "--delete":
                        for key in value.split(","):
                            self.vms[vmid].pop(key, None)
                    elif option == "--net0":
                        self.vms[vmid]["net0"] = value.replace("virtio,", f"virtio={self.mac},")
                    elif option == "--sshkeys":
                        self.vms[vmid]["sshkeys"] = Path(value).read_text().strip()
                    else:
                        self.vms[vmid][option.removeprefix("--")] = value
                return CommandResult("")
            if verb == "disk":
                device, size = args[4], args[5]
                self.vms[vmid][device] = "local-lvm:vm-disk,size=" + size
                return CommandResult("")
            if verb == "start":
                self.status[vmid] = "running"
                return CommandResult("")
            if verb == "stop":
                self.status[vmid] = "stopped"
                return CommandResult("")
            if verb == "destroy":
                del self.vms[vmid]
                self.status.pop(vmid, None)
                return CommandResult("")
            if verb == "cloudinit":
                return CommandResult("")
        if args[0] in {"dnsmasq", "systemctl"}:
            return CommandResult("")
        if args[0] == "ip":
            return CommandResult("[]")
        if args[0] == "arping":
            return CommandResult("", returncode=self.arp_codes.get(args[-1], 0))
        raise AssertionError(f"Unexpected fake command: {args}")


@pytest.fixture
def runner(tmp_path: Path) -> FakeRunner:
    return FakeRunner(tmp_path / "storage")
