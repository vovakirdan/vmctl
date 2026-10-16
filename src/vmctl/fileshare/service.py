"""Invocation-owned access provisioning, compensation and revocation."""

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import quote

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.fileshare.client import SFTPGoClient
from vmctl.fileshare.config import FileshareSettings, settings
from vmctl.fileshare.firewall import FileshareFirewall
from vmctl.proxmox.client import ProxmoxClient


@dataclass(frozen=True)
class Access:
    account: str
    directory: str
    endpoint: str
    owner: str
    private_key: str = field(repr=False)
    public_key: str = field(repr=False)


def generate_access(config: FileshareSettings, name: str, vmid: int, token: str) -> Access:
    key = Ed25519PrivateKey.generate()
    account = f"vmctl-{vmid}-{token}"
    return Access(
        account,
        f"{name}-{vmid}-{token[:12]}",
        f"sftp://{config.sftp_host}:{config.sftp_port}",
        f"vmctl:{token}",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode(),
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode(),
    )


class FileshareService:
    def __init__(self, config: Configuration, proxmox: ProxmoxClient) -> None:
        self.config = config
        self.settings = settings(config)
        self.client = SFTPGoClient(config, self.settings)
        self.firewall = FileshareFirewall(proxmox, self.settings)
        self._attempted_users: set[str] = set()
        self._attempted_folders: set[str] = set()

    def preflight(self) -> None:
        self.firewall.entries()
        self.client.request("GET", "users?limit=1")

    def provision(self, access: Access, address: IPv4Address) -> None:
        path = "users/" + quote(access.account, safe="")
        if self.client.request("GET", path, missing_ok=True) is not None:
            raise VmctlError("Fileshare account already exists; refusing to overwrite it")
        folder_path = "folders/" + quote(access.account, safe="")
        if self.client.request("GET", folder_path, missing_ok=True) is not None:
            raise VmctlError("Fileshare folder already exists; refusing to overwrite it")
        self._attempted_folders.add(access.account)
        self.client.request(
            "POST",
            "folders",
            {
                "name": access.account,
                "description": access.owner,
                "mapped_path": self.settings.shared_directory + "/" + access.directory,
            },
        )
        folder = self.client.request("GET", folder_path)
        if (
            not isinstance(folder, dict)
            or type(folder.get("id")) is not int
            or folder["id"] <= 0
            or folder.get("name") != access.account
            or folder.get("description") != access.owner
            or folder.get("mapped_path") != self.settings.shared_directory + "/" + access.directory
        ):
            raise VmctlError("Fileshare folder ownership could not be verified")
        self._attempted_users.add(access.account)
        self.client.request(
            "POST",
            "users",
            {
                "username": access.account,
                "status": 1,
                "description": access.owner,
                "home_dir": self.settings.shared_directory,
                "public_keys": [access.public_key],
                "virtual_folders": [
                    {
                        "id": folder["id"],
                        "name": access.account,
                        "mapped_path": self.settings.shared_directory + "/" + access.directory,
                        "virtual_path": "/" + access.directory,
                        "quota_size": -1,
                        "quota_files": -1,
                    }
                ],
                "permissions": {
                    "/": ["list", "download"],
                    "/" + access.directory: [
                        "list",
                        "download",
                        "upload",
                        "overwrite",
                        "chtimes",
                        "delete",
                        "rename",
                        "create_dirs",
                        "delete_dirs",
                        "rename_dirs",
                        "rename_files",
                    ],
                },
                "filters": {
                    "allowed_ip": [f"{address}/32"],
                    "denied_login_methods": [
                        "password",
                        "keyboard-interactive",
                        "publickey+password",
                        "publickey+keyboard-interactive",
                    ],
                    "denied_protocols": ["FTP", "DAV", "HTTP"],
                },
            },
        )

        self.firewall.grant(address, access.account)

    def revoke(self, account: str, token: str, *, rollback: bool = False) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not re.fullmatch(
            rf"vmctl-[0-9]+-{token}", account
        ):
            raise VmctlError("Fileshare account ownership metadata is invalid")
        path = "users/" + quote(account, safe="")
        if not rollback or account in self._attempted_users:
            user = self.client.request("GET", path, missing_ok=True)
            if user is not None:
                if not isinstance(user, dict) or user.get("description") != f"vmctl:{token}":
                    raise VmctlError("Fileshare account ownership could not be verified")
                # SFTPGo disconnect=1 terminates active connections when disabling a user.
                user["status"] = 0
                self.client.request("PUT", path + "?disconnect=1", user)
                self.firewall.revoke(account)
                self.client.request("DELETE", path)
            else:
                self.firewall.revoke(account)
        if not rollback or account in self._attempted_folders:
            folder_path = "folders/" + quote(account, safe="")
            folder = self.client.request("GET", folder_path, missing_ok=True)
            if folder is not None:
                vmid = account.split("-")[1]
                prefix = self.settings.shared_directory.rstrip("/") + "/"
                target = folder.get("mapped_path", "") if isinstance(folder, dict) else ""
                if (
                    not isinstance(folder, dict)
                    or folder.get("description") != f"vmctl:{token}"
                    or folder.get("name") != account
                    or not isinstance(target, str)
                    or not target.startswith(prefix)
                    or "/" in target[len(prefix) :]
                    or not target.endswith(f"-{vmid}-{token[:12]}")
                ):
                    raise VmctlError("Fileshare folder ownership could not be verified")
                # This removes only the virtual-folder record, never filesystem contents.
                self.client.request("DELETE", folder_path)


def inject_access(data: str, access: Access, config: FileshareSettings, username: str) -> str:
    """Add user-owned secret files without exposing them in a preview or process arguments."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
        raise VmctlError("Fileshare requires a validated non-root cloud-init username")
    document: dict[str, Any] = yaml.safe_load(data)
    root = f"/home/{username}/.config/vmctl/fileshare"
    known_host = (
        config.sftp_host if config.sftp_port == 22 else f"[{config.sftp_host}]:{config.sftp_port}"
    )
    rclone = (
        "[files]\ntype = sftp\n"
        + f"host = {config.sftp_host}\nport = {config.sftp_port}\n"
        + f"user = {access.account}\nkey_file = {root}/id_ed25519\n"
        + f"known_hosts_file = {root}/known_hosts\ndisable_hashcheck = true\n"
    )
    moves: list[list[str]] = [
        ["chown", f"{username}:{username}", root],
        ["chmod", "0700", root],
    ]
    for name, content, mode in (
        ("id_ed25519", access.private_key, "0600"),
        ("known_hosts", f"{known_host} {config.host_public_key.strip()}\n", "0600"),
        ("rclone.conf", rclone, "0600"),
    ):
        document["write_files"].append(
            {
                "path": f"{root}/.{name}.vmctl-pending",
                "content": content,
                "owner": f"{username}:{username}",
                "permissions": mode,
                "defer": True,
            }
        )
        moves.append(["mv", "-fT", "--", f"{root}/.{name}.vmctl-pending", f"{root}/{name}"])
    document["write_files"].append(
        {
            "path": "/usr/local/bin/.vmctl-files-pending",
            "permissions": "0755",
            "owner": "root:root",
            "defer": True,
            "content": f'#!/bin/sh\nexec rclone --config "{root}/rclone.conf" "$@"\n',
        }
    )
    moves.append(
        [
            "mv",
            "-fT",
            "--",
            "/usr/local/bin/.vmctl-files-pending",
            "/usr/local/bin/vmctl-files",
        ]
    )
    document["runcmd"] = moves + document["runcmd"]
    return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)
