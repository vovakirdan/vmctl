"""Official desktop definitions and isolated guest installer command boundaries."""

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.resolver import resolve_modules
from vmctl.config import Configuration
from vmctl.errors import VmctlError

APPLICATIONS = ("codex-desktop", "claude-desktop", "opencode-desktop")
SCRIPTS = Path(__file__).parents[1] / "config/bootstrap/scripts"


@pytest.mark.parametrize("name", APPLICATIONS)
def test_desktop_apps_are_opt_in_and_have_no_node_dependency(
    config: Configuration, name: str
) -> None:
    module = load_modules(config)[name]
    assert module.group == "ai-desktop" and module.desktop_only
    assert module.dependencies == ["ai-tools"] and module.default_version == "latest"
    resolved = resolve_modules([name, name], load_modules(config), config.profiles)
    assert [item.name for item in resolved].count("ai-tools") == 1
    assert [item.name for item in resolved].count(name) == 1
    assert "node" not in [item.name for item in resolved]
    assert not set(APPLICATIONS) & set(config.profiles["surge-dev"])


@pytest.mark.parametrize("name", APPLICATIONS)
@pytest.mark.parametrize(
    "distro,release",
    [
        ("ubuntu", "noble"),
        ("ubuntu", "24.04"),
        ("ubuntu", "resolute"),
        ("ubuntu", "26.04"),
        ("debian", "trixie"),
        ("debian", "13"),
    ],
)
def test_verified_desktop_release_aliases(
    config: Configuration, name: str, distro: str, release: str
) -> None:
    template = config.template("ubuntu-desktop").model_copy(
        update={"distro": distro, "release": release}
    )
    assert load_modules(config)[name].implementation(template).scripts


@pytest.mark.parametrize("name", APPLICATIONS)
@pytest.mark.parametrize("template", ["ubuntu-server", "debian", "rocky", "alpine"])
def test_server_and_unsupported_family_apps_fail_before_provisioning(
    config: Configuration, name: str, template: str
) -> None:
    selected = config.template(template)
    with pytest.raises(VmctlError, match="desktop|implementation"):
        load_modules(config)[name].implementation(selected)


def test_codex_desktop_does_not_claim_ubuntu_2510_support(config: Configuration) -> None:
    template = config.template("ubuntu-desktop").model_copy(update={"release": "questing"})
    modules = load_modules(config)
    with pytest.raises(VmctlError, match="implementation"):
        modules["codex-desktop"].implementation(template)
    assert modules["claude-desktop"].implementation(template).scripts


@pytest.mark.parametrize("name", APPLICATIONS)
def test_desktop_cloud_init_installs_but_never_launches_or_authenticates(
    config: Configuration, name: str
) -> None:
    modules = resolve_modules([name], load_modules(config), config.profiles)
    text = CloudInitRenderer(config).render(
        config.template("ubuntu-desktop"), "desktop-test", "ssh-ed25519 key", modules, {}
    )
    data: dict[str, Any] = yaml.safe_load(text)
    script = next(
        item for item in data["write_files"] if item["path"].endswith(f"development-{name}-0.sh")
    )
    assert script["owner"] == "root:root" and script["permissions"] == "0700"
    assert "dpkg-query" in script["content"] and "apt-get install" in script["content"]
    assert not any(
        value in text
        for value in (
            "--no-sandbox",
            "--ozone-platform",
            "WaylandEnable",
            "API_KEY",
            "auth.json",
        )
    )
    with pytest.raises(VmctlError, match="override"):
        CloudInitRenderer(config).render(
            config.template("ubuntu-desktop"),
            "desktop-test",
            "ssh-ed25519 key",
            modules,
            {name: "1.2.3"},
        )


# Every installer subprocess is substituted. All writable paths stay inside the fixture.
FAKE_COMMAND = r"""
import json, os, pathlib, sys, tempfile
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['FAKE_GUEST_ROOT'])
with (root / 'calls.jsonl').open('a') as output:
    output.write(json.dumps([name, *args]) + '\n')
architecture = os.environ.get('FAKE_ARCH', 'amd64')
package = os.environ.get('FAKE_PACKAGE', 'chatgpt')
version = os.environ.get('FAKE_VERSION', '2.0.24')
def path(value: str) -> pathlib.Path:
    result = pathlib.Path(value)
    if not result.is_relative_to(root):
        raise RuntimeError('A mocked command attempted to write outside its guest fixture')
    return result
if name == 'dpkg':
    print(architecture)
elif name == 'curl':
    if os.environ.get('FAKE_DOWNLOAD_FAIL'):
        sys.exit(23)
    if '-w' in args:
        print(os.environ.get('FAKE_STABLE_URL', 'https://opencode.ai/files/bin/2.0.24/opencode-desktop-linux-amd64.deb'), end='')
    else:
        path(args[args.index('-o') + 1]).write_bytes(b'official package or key fixture')
elif name == 'dpkg-deb':
    values = {'Package': os.environ.get('FAKE_DEB_PACKAGE', package),
              'Architecture': os.environ.get('FAKE_DEB_ARCH', architecture), 'Version': version}
    print(values[args[-1]])
elif name == 'dpkg-query':
    values = {'-f=${Status}': 'install ok installed', '-f=${Architecture}': architecture, '-f=${Version}': version}
    print(values[args[1]])
elif name == 'gpg':
    print('pub:-:4096:1:key:0::::::')
    print('fpr:::::::::' + os.environ.get('FAKE_FINGERPRINT', '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE') + ':')
    if os.environ.get('FAKE_EXTRA_KEY'):
        print('pub:-:4096:1:additional:0::::::')
elif name == 'mktemp':
    if '-d' in args:
        print(tempfile.mkdtemp(dir=root))
    else:
        parent = path(args[-1]).parent
        descriptor, filename = tempfile.mkstemp(dir=parent)
        os.close(descriptor)
        print(filename)
elif name == 'install':
    for target in args[3:]:
        path(target).mkdir(parents=True, exist_ok=True)
elif name == 'mv':
    os.replace(path(args[-2]), path(args[-1]))
elif name == 'chmod':
    for target in args[1:]:
        path(target).chmod(int(args[0], 8))
elif name in ('apt-get', 'chown', 'rm'):
    pass
else:
    raise RuntimeError('Unexpected mocked command ' + name)
"""

GuestRun = Callable[
    [str, list[str], dict[str, str]], tuple[subprocess.CompletedProcess[str], list[list[str]]]
]


@pytest.fixture
def guest_run(tmp_path: Path) -> GuestRun:
    root = tmp_path / "guest"
    root.mkdir()
    binary_directory = root / "bin"
    binary_directory.mkdir()
    for name in (
        "curl",
        "dpkg",
        "dpkg-deb",
        "dpkg-query",
        "apt-get",
        "gpg",
        "mktemp",
        "install",
        "mv",
        "chmod",
        "chown",
        "rm",
    ):
        command = binary_directory / name
        command.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        command.chmod(0o755)

    def run(
        script: str, arguments: list[str], extra: dict[str, str]
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        extra = extra.copy()
        os_release = root / "os-release"
        os_release.write_text(extra.pop("OS_RELEASE", 'ID=ubuntu\nVERSION_ID="24.04"\n'))
        source = (
            (SCRIPTS / script)
            .read_text()
            .replace(". /etc/os-release", ". " + shlex.quote(str(os_release)))
        )
        source = source.replace(
            "key_directory=/usr/share/keyrings",
            "key_directory=" + shlex.quote(str(root / "keyrings")),
        )
        source = source.replace(
            "repository_directory=/etc/apt/sources.list.d",
            "repository_directory=" + shlex.quote(str(root / "repositories")),
        )
        executable = root / "installer.sh"
        executable.write_text(source)
        environment = (
            dict(os.environ)
            | {"FAKE_GUEST_ROOT": str(root), "PATH": f"{binary_directory}:{os.environ['PATH']}"}
            | extra
        )
        result = subprocess.run(
            ["sh", str(executable), *arguments],
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )
        log = root / "calls.jsonl"
        calls: list[list[str]] = (
            [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        )
        return result, calls

    return run


@pytest.mark.parametrize("application", ["chatgpt", "opencode"])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_official_deb_download_metadata_and_install_are_mocked(
    guest_run: GuestRun, application: str, architecture: str
) -> None:
    result, calls = guest_run(
        "desktop-deb-install.sh",
        [application],
        {"FAKE_PACKAGE": application, "FAKE_ARCH": architecture},
    )
    assert result.returncode == 0, result.stderr
    download = next(call for call in calls if call[0] == "curl" and "-w" not in call)
    assert "--proto-redir" in download and "=https" in download
    assert any(f"{architecture}.deb" in argument for argument in download)
    assert [call[0] for call in calls].index("dpkg-deb") < [call[0] for call in calls].index(
        "apt-get"
    )
    assert any(call[:3] == ["apt-get", "install", "-y"] for call in calls)
    assert any(call[0] == "dpkg-query" and "-f=${Version}" in call for call in calls)


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"FAKE_DEB_PACKAGE": "other-app"}, "package name"),
        ({"FAKE_DEB_ARCH": "i386"}, "architecture"),
        ({"FAKE_DOWNLOAD_FAIL": "1"}, ""),
        ({"FAKE_ARCH": "i386"}, "amd64 or arm64"),
        ({"OS_RELEASE": 'ID=ubuntu\nVERSION_ID="25.10"\n'}, "requires Ubuntu"),
    ],
)
def test_invalid_deb_or_guest_fails_before_package_install(
    guest_run: GuestRun, extra: dict[str, str], message: str
) -> None:
    result, calls = guest_run("desktop-deb-install.sh", ["chatgpt"], extra)
    assert result.returncode != 0 and message in result.stderr
    assert not any(call[0] == "apt-get" for call in calls)


def test_opencode_requires_official_stable_release_identity(guest_run: GuestRun) -> None:
    result, calls = guest_run(
        "desktop-deb-install.sh",
        ["opencode"],
        {"FAKE_STABLE_URL": "https://untrusted.example/package.deb"},
    )
    assert result.returncode != 0 and "unexpected official release URL" in result.stderr
    assert not any(call[0] == "apt-get" for call in calls)


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_claude_key_and_repository_replace_atomically_after_verification(
    guest_run: GuestRun, architecture: str
) -> None:
    result, calls = guest_run("claude-desktop-install.sh", [], {"FAKE_ARCH": architecture})
    assert result.returncode == 0, result.stderr
    moves = [call for call in calls if call[0] == "mv"]
    assert len(moves) == 2 and all(call[1:3] == ["-fT", "--"] for call in moves)
    assert all(Path(call[-2]).parent == Path(call[-1]).parent for call in moves)
    repository = Path(moves[1][-1]).read_text()
    assert f"arch={architecture}" in repository and "signed-by=" in repository
    assert "https://downloads.claude.ai/claude-desktop/apt/stable stable main" in repository
    assert Path(moves[0][-1]).stat().st_mode & 0o777 == 0o644
    names = [call[0] for call in calls]
    assert names.index("gpg") < names.index("mv") < names.index("apt-get")


@pytest.mark.parametrize("extra", [{"FAKE_FINGERPRINT": "0" * 40}, {"FAKE_EXTRA_KEY": "1"}])
def test_claude_untrusted_keys_never_replace_configuration_or_install(
    guest_run: GuestRun, extra: dict[str, str]
) -> None:
    result, calls = guest_run("claude-desktop-install.sh", [], extra)
    assert result.returncode != 0 and "fingerprint does not match" in result.stderr
    assert not any(call[0] in {"mv", "apt-get"} for call in calls)


@pytest.mark.parametrize("script", ["desktop-deb-install.sh", "claude-desktop-install.sh"])
def test_desktop_installer_shell_syntax(script: str) -> None:
    subprocess.run(["sh", "-n", str(SCRIPTS / script)], check=True, capture_output=True)
