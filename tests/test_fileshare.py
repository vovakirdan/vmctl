"""Fileshare credentials and lifecycle are isolated from development and dry-run."""

from ipaddress import IPv4Address
from unittest.mock import Mock

import pytest
import yaml
from conftest import FakeRunner
from pydantic import ValidationError
from test_workflows_cli import services

from vmctl.bootstrap.system import select_bootstrap
from vmctl.config import Configuration
from vmctl.errors import CreationError, VmctlError
from vmctl.fileshare.config import FileshareSettings
from vmctl.fileshare.firewall import FileshareFirewall
from vmctl.fileshare.service import FileshareService, generate_access, inject_access
from vmctl.models import CloneRequest, CreateRequest
from vmctl.services.clone_vm import CloneVMService


def definition(config: Configuration) -> FileshareSettings:
    return FileshareSettings(
        endpoint="https://10.210.0.103:8443",
        sftp_host="10.210.0.103",
        host_public_key=config.path(config.host.ssh_key).read_text(),
        ca_file="ca.pem",
        api_key_file="api.key",
        firewall_vmid=105,
    )


def configured(config: Configuration) -> Configuration:
    return config.model_copy(
        update={
            "host": config.host.model_copy(update={"fileshare": definition(config).model_dump()})
        }
    )


def test_feature_opt_in_and_no_development(config: Configuration) -> None:
    request = CreateRequest("example", "debian", "small")
    selection = select_bootstrap(config, request)
    assert "fileshare" not in [feature.name for feature in selection.system_features]
    with pytest.raises(VmctlError, match="unavailable"):
        select_bootstrap(
            config, CreateRequest("example", "debian", "small", system_features=("fileshare",))
        )
    selection = select_bootstrap(
        configured(config),
        CreateRequest("example", "debian", "small", system_features=("fileshare",)),
    )
    assert "fileshare" in [feature.name for feature in selection.system_features]
    assert not selection.modules


def test_credentials_are_distinct_redacted_and_host_key_pinned(config: Configuration) -> None:
    settings = definition(config)
    access = generate_access(settings, "example", 104, "a" * 32)
    another = generate_access(settings, "example", 104, "b" * 32)
    assert access.private_key != another.private_key
    assert "PRIVATE KEY" not in repr(access)
    data = inject_access(
        yaml.safe_dump({"write_files": [], "runcmd": []}), access, settings, "customuser"
    )
    document = yaml.safe_load(data)
    files = document["write_files"]
    guest_files = [item for item in files if item["path"].startswith("/home/customuser/")]
    assert len(guest_files) == 3
    assert all(item["owner"] == "customuser:customuser" for item in guest_files)
    assert all(item["permissions"] == "0600" for item in guest_files)
    wrapper = next(item for item in files if item["path"] == "/usr/local/bin/.vmctl-files-pending")
    assert wrapper["owner"] == "root:root" and wrapper["permissions"] == "0755"
    assert "/home/customuser/.config/vmctl/fileshare/rclone.conf" in wrapper["content"]
    assert not any(item["path"].endswith("/.config/rclone/rclone.conf") for item in files)
    rclone = next(item["content"] for item in files if "rclone.conf" in item["path"])
    assert "known_hosts_file = " in rclone and "disable_hashcheck = true" in rclone
    assert "[files]" in rclone
    assert [
        "chown",
        "customuser:customuser",
        "/home/customuser/.config/vmctl/fileshare",
    ] in document["runcmd"]
    assert ["chmod", "0700", "/home/customuser/.config/vmctl/fileshare"] in document["runcmd"]
    assert [
        "mv",
        "-fT",
        "--",
        "/usr/local/bin/.vmctl-files-pending",
        "/usr/local/bin/vmctl-files",
    ] in document["runcmd"]


def test_api_permissions_and_ownership_guard(config: Configuration) -> None:
    service = object.__new__(FileshareService)
    service.settings = definition(config)
    service.client = Mock()
    service.firewall = Mock()
    access = generate_access(service.settings, "example", 104, "a" * 32)
    service._attempted_users = set()
    service._attempted_folders = set()
    folder = {
        "id": 7,
        "name": access.account,
        "description": access.owner,
        "mapped_path": service.settings.shared_directory + "/" + access.directory,
    }
    service.client.request.side_effect = [None, None, None, folder, None]
    service.provision(access, IPv4Address("10.210.0.104"))
    calls = service.client.request.call_args_list
    assert [call.args[:2] for call in calls[:4]] == [
        ("GET", "users/" + access.account),
        ("GET", "folders/" + access.account),
        ("POST", "folders"),
        ("GET", "folders/" + access.account),
    ]
    payload = calls[4].args[2]
    assert payload["virtual_folders"][0]["id"] == 7
    assert payload["permissions"]["/"] == ["list", "download"]
    assert "create_symlinks" not in payload["permissions"]["/" + access.directory]
    assert "chtimes" in payload["permissions"]["/" + access.directory]
    assert "chtimes" not in payload["permissions"]["/"]
    assert not {"chmod", "chown", "create_symlinks"} & set(
        payload["permissions"]["/" + access.directory]
    )
    assert payload["filters"]["allowed_ip"] == ["10.210.0.104/32"]
    assert access.private_key not in str(payload)
    service.firewall.grant.assert_called_once()
    service.client.request.side_effect = None
    service.client.request.return_value = {"description": "someone else"}
    with pytest.raises(VmctlError, match="ownership"):
        service.revoke(access.account, "a" * 32)
    service.firewall.revoke.assert_not_called()


def test_revocation_disables_disconnects_then_deletes(config: Configuration) -> None:
    service = object.__new__(FileshareService)
    service.settings = definition(config)
    service.client = Mock()
    service.firewall = Mock()
    account = "vmctl-104-" + "a" * 32
    service.client.request.side_effect = [{"description": "vmctl:" + "a" * 32}, None, None, None]
    service.revoke(account, "a" * 32)
    calls = service.client.request.call_args_list
    assert calls[1].args[0] == "PUT" and calls[1].args[1].endswith("?disconnect=1")
    assert calls[1].args[2]["status"] == 0
    assert calls[2].args[0] == "DELETE"
    service.firewall.revoke.assert_called_once_with(account)


def test_tls_and_endpoint_reject_insecure_config(config: Configuration) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        FileshareSettings.model_validate(
            {**definition(config).model_dump(), "endpoint": "http://example"}
        )


def test_create_dryrun_generates_no_credentials(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured(config)
    creator, _, _, _ = services(config, runner)
    preflight = Mock()
    monkeypatch.setattr("vmctl.services.create_vm.FileshareService", preflight)
    generator = Mock(side_effect=AssertionError("must not generate secrets"))
    monkeypatch.setattr("vmctl.services.create_vm.generate_access", generator)
    plan = creator.plan(CreateRequest("example", "debian", "small", system_features=("fileshare",)))
    assert "PRIVATE KEY" not in plan.user_data
    assert "fileshare" in plan.system_features
    generator.assert_not_called()
    assert not any(call[0:2] == ("qm", "clone") for call in runner.calls)


def test_failure_revokes_owned_account_before_vm_cleanup(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured(config)
    creator, _, _, _ = services(config, runner)
    service = Mock()
    service.settings = definition(config)
    service.provision.side_effect = VmctlError("injected provisioning failure")
    monkeypatch.setattr("vmctl.services.create_vm.FileshareService", Mock(return_value=service))
    with pytest.raises(CreationError, match="provisioning failure"):
        creator.create(CreateRequest("example", "debian", "small", system_features=("fileshare",)))
    service.revoke.assert_called_once()
    assert 104 not in runner.vms


def test_delete_stops_if_revocation_fails(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, delete, _, _ = services(config, runner)
    result = creator.create(CreateRequest("example", "debian", "small"))
    from vmctl.proxmox.parser import decode_metadata, encode_metadata

    metadata = decode_metadata(runner.vms[result.vmid]["description"])
    metadata["fileshare_account"] = "vmctl-104-" + metadata["invocation"]
    runner.vms[result.vmid]["description"] = encode_metadata("", metadata)
    service = Mock()
    service.revoke.side_effect = VmctlError("API unavailable")
    monkeypatch.setattr("vmctl.services.delete_vm.FileshareService", Mock(return_value=service))
    with pytest.raises(VmctlError, match="API unavailable"):
        delete.delete(str(result.vmid), expected=delete.plan(str(result.vmid)))
    assert result.vmid in runner.vms
    assert runner.status[result.vmid] == "running"


def test_clone_rejects_fileshare_credentials(config: Configuration, runner: FakeRunner) -> None:
    creator, _, client, reservations = services(config, runner)
    result = creator.create(CreateRequest("example", "debian", "small", start=False))
    from vmctl.proxmox.parser import decode_metadata, encode_metadata

    metadata = decode_metadata(runner.vms[result.vmid]["description"])
    metadata["system_features"] += ",fileshare"
    runner.vms[result.vmid]["description"] = encode_metadata("", metadata)
    clone = CloneVMService(config, client, reservations, creator.allocator)
    with pytest.raises(VmctlError, match="private access key"):
        clone.plan(CloneRequest(str(result.vmid), "copy"))


def test_firewall_never_replaces_unowned_entry(config: Configuration) -> None:
    client = Mock()
    firewall = FileshareFirewall(client, definition(config))
    client.command.return_value.stdout = '[{"cidr":"10.210.0.104/32","comment":"manual"}]'
    with pytest.raises(VmctlError, match="overlaps"):
        firewall.grant(IPv4Address("10.210.0.104"), "vmctl-owned")
    assert client.command.call_count == 1


def test_firewall_revoke_only_owned_host_entries(config: Configuration) -> None:
    client = Mock()
    firewall = FileshareFirewall(client, definition(config))
    client.command.return_value.stdout = (
        '[{"cidr":"10.210.0.104/32","comment":"mine"},'
        '{"cidr":"10.210.0.105/32","comment":"manual"}]'
    )
    firewall.revoke("mine")
    assert client.command.call_args.args == ("pvesh", "delete", firewall.path + "/10.210.0.104")


def test_rocky_feature_fails_clearly_without_enabling_external_repos(config: Configuration) -> None:
    with pytest.raises(VmctlError, match="does not support family"):
        select_bootstrap(
            configured(config),
            CreateRequest("example", "rocky", "small", system_features=("fileshare",)),
        )


def test_invalid_config_does_not_echo_inputs(config: Configuration) -> None:
    from vmctl.fileshare.config import settings

    config = config.model_copy(
        update={
            "host": config.host.model_copy(update={"fileshare": {"unexpected": "SUPER-SECRET"}})
        }
    )
    with pytest.raises(VmctlError) as exc:
        settings(config)
    assert "SUPER-SECRET" not in str(exc.value)


def test_host_pin_accepts_one_key_only(config: Configuration) -> None:
    value = definition(config)
    with pytest.raises(ValidationError, match="exactly one"):
        FileshareSettings.model_validate(
            {**value.model_dump(), "host_public_key": value.host_public_key * 2}
        )


@pytest.mark.parametrize("username", ["root", "bad/name", "user\ncommand", "-user"])
def test_guest_wrapper_rejects_unsafe_usernames(config: Configuration, username: str) -> None:
    value = definition(config)
    access = generate_access(value, "example", 104, "a" * 32)
    with pytest.raises(VmctlError, match="validated non-root"):
        inject_access(yaml.safe_dump({"write_files": [], "runcmd": []}), access, value, username)


def test_delete_protected_vm_rejects_before_stop_or_destroy(
    config: Configuration, runner: FakeRunner
) -> None:
    creator, delete, _, _ = services(config, runner)
    result = creator.create(CreateRequest("files-vm", "debian", "small"))
    runner.vms[result.vmid]["protection"] = "1"
    runner.calls.clear()
    with pytest.raises(VmctlError, match="protected"):
        delete.plan(str(result.vmid))
    assert runner.status[result.vmid] == "running"
    assert not any(call[:2] in {("qm", "stop"), ("qm", "destroy")} for call in runner.calls)


def test_preflight_authenticates_api_without_account_or_key_mutations(
    config: Configuration,
) -> None:
    service = object.__new__(FileshareService)
    service.client = Mock()
    service.firewall = Mock()
    service.preflight()
    service.client.request.assert_called_once_with("GET", "users?limit=1")
    service.firewall.entries.assert_called_once_with()
    service.firewall.grant.assert_not_called()
    service.firewall.revoke.assert_not_called()


def test_unavailable_api_fails_before_clone_or_secret_generation(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured(config)
    creator, _, _, _ = services(config, runner)
    service = Mock()
    service.preflight.side_effect = VmctlError("Fileshare API unavailable")
    monkeypatch.setattr("vmctl.services.create_vm.FileshareService", Mock(return_value=service))
    generator = Mock(side_effect=AssertionError("must not generate secrets"))
    monkeypatch.setattr("vmctl.services.create_vm.generate_access", generator)
    with pytest.raises(VmctlError, match="API unavailable"):
        creator.create(CreateRequest("example", "debian", "small", system_features=("fileshare",)))
    generator.assert_not_called()
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)
    service.provision.assert_not_called()
    service.revoke.assert_not_called()


def test_rollback_never_deletes_preexisting_account_or_folder(config: Configuration) -> None:
    service = object.__new__(FileshareService)
    service.settings = definition(config)
    service.client = Mock()
    service.firewall = Mock()
    service._attempted_users = set()
    service._attempted_folders = set()
    access = generate_access(service.settings, "example", 104, "a" * 32)
    service.client.request.side_effect = [None, {"description": access.owner}]
    with pytest.raises(VmctlError, match="folder already exists"):
        service.provision(access, IPv4Address("10.210.0.104"))
    service.revoke(access.account, "a" * 32, rollback=True)
    assert all(call.args[0] == "GET" for call in service.client.request.call_args_list)
    service.firewall.revoke.assert_not_called()


def test_owned_folder_record_removed_without_filesystem_deletion(config: Configuration) -> None:
    service = object.__new__(FileshareService)
    service.settings = definition(config)
    service.client = Mock()
    service.firewall = Mock()
    access = generate_access(service.settings, "example", 104, "a" * 32)
    folder = {
        "id": 7,
        "name": access.account,
        "description": access.owner,
        "mapped_path": service.settings.shared_directory + "/" + access.directory,
    }
    service.client.request.side_effect = [None, folder, None]
    service.revoke(access.account, "a" * 32)
    assert service.client.request.call_args.args == ("DELETE", "folders/" + access.account)


def test_unowned_folder_is_retained(config: Configuration) -> None:
    service = object.__new__(FileshareService)
    service.settings = definition(config)
    service.client = Mock()
    service.firewall = Mock()
    access = generate_access(service.settings, "example", 104, "a" * 32)
    service.client.request.side_effect = [None, {"name": access.account, "description": "manual"}]
    with pytest.raises(VmctlError, match="folder ownership"):
        service.revoke(access.account, "a" * 32)
    assert not any(call.args[0] == "DELETE" for call in service.client.request.call_args_list)
