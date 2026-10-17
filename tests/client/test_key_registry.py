"""Named keys remain public, portable and additive to primary guest access."""

import base64
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vmctl.client_config import initialize_client_config
from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.frontend import FrontendSettings, key_registry, load_public_key
from vmctl.keys import PublicKeyRegistryStore, key_fingerprint, merge_public_keys


def public_key(seed: int = 1, comment: str = "test") -> str:
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment


def setup_client(directory: Path) -> FrontendSettings:
    initialize_client_config(directory)
    path = directory / "client.toml"
    path.write_text(path.read_text().replace('"~/.ssh/id_ed25519.pub"', '"primary.pub"'))
    (directory / "primary.pub").write_text(public_key(1))
    return FrontendSettings(directory)


def test_missing_registry_and_default_selection(tmp_path: Path) -> None:
    settings = setup_client(tmp_path)
    store = key_registry(settings)
    assert store.load().keys == {}
    assert load_public_key(settings) == public_key(1) + "\n"
    store.add("iphone", public_key(2), description="My phone", default=True)
    assert load_public_key(settings) == public_key(1) + "\n" + public_key(2) + "\n"
    assert store.load().keys["iphone"].description == "My phone"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_explicit_names_and_file_preserve_primary(tmp_path: Path) -> None:
    settings = setup_client(tmp_path)
    store = key_registry(settings)
    store.add("iphone", public_key(2), default=True)
    store.add("tablet", public_key(3))
    alternate = tmp_path / "alternate.pub"
    alternate.write_text(public_key(4))
    assert load_public_key(settings, names=["tablet"]) == merge_public_keys(
        public_key(1), public_key(3)
    )
    assert load_public_key(settings, alternate) == merge_public_keys(public_key(4), public_key(2))
    assert load_public_key(settings, names=[]) == public_key(1) + "\n"
    with pytest.raises(VmctlError, match="Unknown SSH key"):
        load_public_key(settings, names=["missing"])


def test_local_defaults_and_deferred_legacy_loading(config: Configuration) -> None:
    settings = FrontendSettings(config.root, local=True)
    assert load_public_key(settings) is None
    store = key_registry(settings)
    store.add("iphone", public_key(2), default=True)
    assert public_key(2) in (load_public_key(settings) or "")
    assert Path(config.host.ssh_key).read_text().strip() in (load_public_key(settings) or "")


def test_deduplicate_comments_and_fingerprint(tmp_path: Path) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    store.add("first", public_key(1, "first"), default=True)
    store.add("alias", public_key(1, "second"), default=True)
    assert store.resolve() == public_key(1, "first") + "\n"
    assert key_fingerprint(public_key(1, "a")) == key_fingerprint(public_key(1, "b"))
    assert key_fingerprint(public_key(1)).startswith("SHA256:")
    with pytest.raises(VmctlError, match="different public key"):
        store.add("first", public_key(2))


@pytest.mark.parametrize(
    "invalid",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret",
        'command="whoami" ' + public_key(),
        "ssh-ed25519 not-base64",
        public_key() + "\n" + public_key(2),
    ],
)
def test_reject_invalid_keys_without_writing(tmp_path: Path, invalid: str) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    with pytest.raises(VmctlError):
        store.add("bad", invalid)
    assert not store.path.exists()


def test_unicode_key_comment_round_trips(tmp_path: Path) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    key = public_key(1) + " phone \U0001f511"
    store.add("phone", key, description="Personal phone")
    assert store.load().keys["phone"].public_key == key


def test_remove_preserves_other_entries(tmp_path: Path) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    store.add("phone", public_key(1), default=True)
    store.add("laptop", public_key(2), description="Preserved", default=True)
    store.remove("phone")
    assert store.load().default_keys == ["laptop"]
    assert store.load().keys["laptop"].description == "Preserved"
    assert store.resolve() == public_key(2) + "\n"


def test_invalid_defaults_and_empty_selection(tmp_path: Path) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    with pytest.raises(VmctlError, match="No SSH keys"):
        store.resolve()
    store.path.write_text('default_keys = ["missing"]\n')
    with pytest.raises(VmctlError, match="unknown named key"):
        store.load()


def test_replace_failure_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    store.add("original", public_key(1))
    original = store.path.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("Injected failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(VmctlError, match="Cannot save"):
        store.add("phone", public_key(2))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".keys.toml.*"))


def test_registry_concurrent_edits_preserve_both_keys(tmp_path: Path) -> None:
    store = PublicKeyRegistryStore(tmp_path / "keys.toml")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda i: store.add(f"key{i}", public_key(i), default=True), [1, 2]))
    assert set(store.load().keys) == {"key1", "key2"}
    assert set(store.load().default_keys) == {"key1", "key2"}


@pytest.mark.parametrize("absolute", [False, True])
def test_registry_configurable_path(tmp_path: Path, absolute: bool) -> None:
    settings = setup_client(tmp_path)
    filename = tmp_path / "nested" / "custom.toml" if absolute else Path("nested/custom.toml")
    path = tmp_path / "client.toml"
    path.write_text(
        path.read_text().replace(
            'ssh_keys_file = "ssh-keys.toml"', f'ssh_keys_file = "{filename.as_posix()}"'
        )
    )
    store = key_registry(settings)
    assert store.path == tmp_path / "nested" / "custom.toml"
    store.add("iphone", public_key(2), default=True)
    assert public_key(2) in (load_public_key(settings) or "")
