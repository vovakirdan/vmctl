"""Render separate system and development definitions into guest-only operations."""

import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, Protocol

import yaml
from pydantic import SecretStr

from vmctl.bootstrap.models import Module, script_path
from vmctl.bootstrap.system import SystemFeature
from vmctl.config import Configuration, Template
from vmctl.errors import VmctlError
from vmctl.utils.passwords import validate_password_hash


class BootstrapExecutor(Protocol):
    """Build initial bootstrap data; future executors may use SSH or a guest agent."""

    def render(
        self,
        template: Template,
        name: str,
        public_key: str,
        modules: Sequence[Module],
        versions: Mapping[str, str],
        *,
        system_features: Sequence[SystemFeature] = (),
        desktop_password_hash: SecretStr | None = None,
    ) -> str: ...


class CloudInitRenderer:
    def __init__(self, config: Configuration) -> None:
        self.config = config

    def render(
        self,
        template: Template,
        name: str,
        public_key: str,
        modules: Sequence[Module],
        versions: Mapping[str, str],
        *,
        system_features: Sequence[SystemFeature] = (),
        desktop_password_hash: SecretStr | None = None,
    ) -> str:
        username = self.config.host.username
        if set(versions) - {module.name for module in modules}:
            raise VmctlError("Version overrides must refer to selected modules")
        operations: list[str] = ["#!/bin/sh", "set -eu"]
        files: list[dict[str, Any]] = []
        installed: set[str] = set()
        targets: set[str] = set()
        for module in (*system_features, *modules):
            implementation = module.implementation(template)
            category = "development" if isinstance(module, Module) else "system"
            version = (
                versions.get(module.name, module.default_version)
                if isinstance(module, Module)
                else ""
            )
            if version and not re.fullmatch(r"[a-zA-Z0-9_.+-]{1,128}", version):
                raise VmctlError(f"Invalid version for module {module.name}")
            if isinstance(module, Module) and module.name in versions:
                version_fields = (
                    *implementation.packages,
                    *(argument for command in implementation.commands for argument in command),
                    *(argument for script in implementation.scripts for argument in script.args),
                    *(
                        value
                        for item in implementation.files
                        for value in (item.path, item.content, item.owner)
                    ),
                )
                if version != module.default_version and not any(
                    "{version}" in value for value in version_fields
                ):
                    raise VmctlError(
                        f"Module {module.name!r} does not support version overrides for this template"
                    )

            def expand(value: str, version: str = version) -> str:
                return value.replace("{version}", version).replace("{username}", username)

            if implementation.packages:
                packages = list(
                    dict.fromkeys(expand(package) for package in implementation.packages)
                )
                if any(
                    not re.fullmatch(r"[a-zA-Z0-9_.+:=@-]+", package) or package.startswith("-")
                    for package in packages
                ):
                    raise VmctlError(f"Invalid package name in module {module.name}")
                packages = [package for package in packages if package not in installed]
                installed.update(packages)
                if packages and template.family == "debian":
                    operations.append("export DEBIAN_FRONTEND=noninteractive")
                    operations.append("apt-get update")
                    command = ["apt-get", "install", "-y", "--no-install-recommends", *packages]
                elif packages and template.family == "rhel":
                    command = ["dnf", "install", "-y", *packages]
                elif packages:
                    command = ["apk", "add", "--no-cache", *packages]
                if packages:
                    operations.append(shlex.join(command))
            for index, guest_file in enumerate(implementation.files):
                destination = expand(guest_file.path)
                owner = expand(guest_file.owner)
                path = PurePosixPath(destination)
                if not path.is_absolute() or ".." in path.parts or not path.name:
                    raise VmctlError(f"Invalid guest file path in {module.name}")
                if destination in targets:
                    raise VmctlError(f"Duplicate guest file target: {destination}")
                if not re.fullmatch(
                    r"[a-z_][a-z0-9_-]*:[a-z_][a-z0-9_-]*", owner
                ) or not re.fullmatch(r"0[0-7]{3}", guest_file.permissions):
                    raise VmctlError(f"Invalid guest file owner or permissions in {module.name}")
                targets.add(destination)
                pending = str(
                    path.with_name(
                        f".{path.name.lstrip('.')}.vmctl-{category}-{module.name}-{index}"
                    )
                )
                files.append(
                    {
                        "path": pending,
                        "owner": owner,
                        "permissions": guest_file.permissions,
                        "content": expand(guest_file.content),
                        # Account/group creation must precede user-owned file writes.
                        "defer": True,
                    }
                )
                operations.append(shlex.join(["mv", "-fT", "--", pending, destination]))
            operations.extend(
                shlex.join([expand(arg) for arg in command]) for command in implementation.commands
            )
            for index, script in enumerate(implementation.scripts):
                destination = f"/var/lib/vmctl/bootstrap/{category}-{module.name}-{index}.sh"
                files.append(
                    {
                        "path": destination,
                        "permissions": "0700",
                        "owner": "root:root",
                        "content": script_path(self.config, script.file).read_text(),
                    }
                )
                operations.append(
                    shlex.join(["sh", destination, *[expand(arg) for arg in script.args]])
                )
        files.append(
            {
                "path": "/var/lib/vmctl/bootstrap/run.sh",
                "permissions": "0700",
                "owner": "root:root",
                "content": "\n".join(operations) + "\n",
            }
        )
        data: dict[str, Any] = {
            "hostname": name,
            "preserve_hostname": False,
            "manage_etc_hosts": True,
            "ssh_pwauth": False,
            "disable_root": True,
            "package_upgrade": False,
            "users": [
                {
                    "name": username,
                    "homedir": f"/home/{username}",
                    "primary_group": username,
                    "shell": "/bin/sh" if template.family == "alpine" else "/bin/bash",
                    "lock_passwd": True,
                    "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                    "ssh_authorized_keys": public_key.strip().splitlines(),
                }
            ],
            "groups": [username],
            "write_files": files,
            "runcmd": [["sh", "/var/lib/vmctl/bootstrap/run.sh"]],
        }
        if desktop_password_hash is not None:
            if not template.desktop:
                raise VmctlError("Desktop passwords require a desktop template")
            data["users"][0]["hashed_passwd"] = validate_password_hash(desktop_password_hash)
            data["users"][0]["lock_passwd"] = False
            data["chpasswd"] = {"expire": False}
        return "#cloud-config\n" + yaml.safe_dump(data, sort_keys=False)
