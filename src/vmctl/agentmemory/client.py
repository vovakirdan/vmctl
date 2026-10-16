"""Restricted SSH JSON protocol; credentials travel only over encrypted stdin."""

import json
from typing import Any

import paramiko

from vmctl.agentmemory.config import AgentmemorySettings, private_file
from vmctl.config import Configuration
from vmctl.errors import VmctlError


class GatewayClient:
    def __init__(self, config: Configuration, settings: AgentmemorySettings) -> None:
        self.config, self.settings = config, settings

    def request(self, operation: str, **fields: str) -> None:
        value = self.settings
        private_file(self.config.path(value.management_key_file))
        client = paramiko.SSHClient()
        try:
            client.load_host_keys(str(self.config.path(value.management_known_hosts_file)))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                value.management_host,
                port=value.management_port,
                username=value.management_user,
                key_filename=str(self.config.path(value.management_key_file)),
                allow_agent=False,
                look_for_keys=False,
                timeout=value.timeout_seconds,
                banner_timeout=value.timeout_seconds,
                auth_timeout=value.timeout_seconds,
            )
            stdin, stdout, stderr = client.exec_command(
                "vmctl-memory-gateway", timeout=value.timeout_seconds
            )
            stdin.write(json.dumps({"operation": operation, **fields}))
            stdin.flush()
            stdin.channel.shutdown_write()
            # The helper emits a bounded, non-secret response and never echoes request values.
            raw = stdout.read(8193)
            if len(raw) > 8192 or stdout.channel.recv_exit_status() != 0:
                raise VmctlError(
                    "Agentmemory gateway rejected the operation; inspect server health"
                )
            result: Any = json.loads(raw)
            if not isinstance(result, dict) or result != {"ok": True}:
                raise VmctlError("Invalid agentmemory gateway response")
        except (OSError, ValueError, paramiko.SSHException):
            raise VmctlError(
                "Cannot contact agentmemory gateway with the pinned SSH identity"
            ) from None
        finally:
            client.close()
