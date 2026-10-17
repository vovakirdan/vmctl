"""Idempotent public-key enrollment through an existing QEMU guest agent."""

import base64
import hashlib
import re
import shlex
import time
from dataclasses import asdict, replace

from vmctl.config import Configuration
from vmctl.errors import UncertainOperationError, VmctlError
from vmctl.models import KeyExportRequest
from vmctl.network.discovery import agent_enabled
from vmctl.operations import KeyExportPreview, KeyExportResult, Progress
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.hardware import confirmation_fingerprint, inspect_vm
from vmctl.utils.atomic import host_lock
from vmctl.utils.ssh_keys import validate_public_key

# Run the merge as the target user, so even a concurrent home-directory change
# cannot turn an authorized_keys update into a privileged arbitrary-file write.
GUEST_MERGE_SCRIPT = r"""set -eu
umask 077
fail() { exit 42; }
uid=$(id -u)
[ "$uid" -ne 0 ] || exit 41
home=$(awk -F: -v user="$(id -un)" '$1 == user {print $6}' /etc/passwd)
case "$home" in /*) ;; *) fail ;; esac
case "$home" in *'/../'*|*'/./'*|*/..|*/.) fail ;; esac
secure_path() {
    path=$1
    while [ "$path" != / ]; do
        [ ! -L "$path" ] || fail
        path=$(dirname "$path")
    done
}
secure_path "$home"
[ -d "$home" ] && [ "$(stat -c %u "$home")" = "$uid" ] || fail
# OpenSSH also requires the home and key directory not to be writable by others.
mode=$(stat -c %a "$home")
[ $((0$mode & 022)) -eq 0 ] || fail
sshdir=$home/.ssh
[ ! -L "$sshdir" ] || fail
if [ ! -e "$sshdir" ]; then mkdir -m 700 "$sshdir"; fi
[ -d "$sshdir" ] && [ "$(stat -c %u "$sshdir")" = "$uid" ] || fail
mode=$(stat -c %a "$sshdir")
[ $((0$mode & 022)) -eq 0 ] || fail
lock=$sshdir/.vmctl-keys.lock
mkdir -m 700 "$lock" 2>/dev/null || exit 43
cleanup() { rm -f "$lock/incoming" "$lock/identities" "$lock/append" "$lock/counts" "$lock/original" "$lock/new"; rmdir "$lock"; }
trap cleanup EXIT
trap 'exit 44' HUP INT TERM
file=$sshdir/authorized_keys
check_file() {
    secure_path "$sshdir"
    [ ! -L "$file" ] || fail
    if [ -e "$file" ]; then
        [ -f "$file" ] && [ "$(stat -c %h "$file")" = 1 ] || fail
        [ "$(stat -c %u "$file")" = "$uid" ] || fail
    fi
}
check_file
printf '%s' VMCTL_PUBLIC_KEYS > "$lock/incoming"
existed=0
if [ -e "$file" ]; then existed=1; cat "$file" > "$lock/original"; else : > "$lock/original"; fi
cat "$lock/original" > "$lock/new"
# Existing authorized_keys may contain options with quoted spaces; preserve every
# byte and compare the actual key type/blob pair, without rewriting old entries.
awk -v append="$lock/append" -v counts="$lock/counts" '
    function identity(line, i,c,quoted,escaped,word,n,parts) {
        if(line ~ /^[ \t]*#/) return ""
        n=0; word=""; quoted=0; escaped=0
        for(i=1;i<=length(line)+1;i++) {
            c=substr(line,i,1)
            if(escaped) {word=word c; escaped=0; continue}
            if(c=="\\") {word=word c; escaped=1; continue}
            if(c=="\"") {quoted=!quoted; word=word c; continue}
            if((c==" " || c=="\t" || c=="") && !quoted) {
                if(length(word)) {
                    parts[++n]=word; word=""
                    if(n>1 && parts[n-1] ~ /^(ssh-|ecdsa-|sk-)/) return parts[n-1] " " parts[n]
                }
            } else word=word c
        }
        if(quoted) return ""
        for(i=1;i<n;i++) if(parts[i] ~ /^(ssh-|ecdsa-|sk-)/) return parts[i] " " parts[i+1]
        return ""
    }
    FILENAME == ARGV[1] {key=identity($0); if(key!="") seen[key]=1; next}
    {key=$1 " " $2; if(seen[key]) unchanged++; else {print $0 >> append; seen[key]=1; added++}}
    END {printf "%d %d\n", added, unchanged > counts}
' "$lock/new" "$lock/incoming"
read -r added unchanged < "$lock/counts"
repair=0
if [ -e "$file" ] && [ "$(stat -c %a "$file")" != 600 ]; then repair=1; fi
if [ "$added" -gt 0 ] || [ "$repair" -eq 1 ]; then
    if [ "$added" -gt 0 ]; then
        if [ -s "$lock/new" ] && [ "$(tail -c 1 "$lock/new" | wc -l)" -eq 0 ]; then
            printf '\n' >> "$lock/new"
        fi
        cat "$lock/append" >> "$lock/new"
    fi
    chmod 600 "$lock/new"
    sync
    check_file
    if [ "$existed" -eq 1 ]; then
        [ -e "$file" ] && cmp -s "$file" "$lock/original" || exit 44
    else
        [ ! -e "$file" ] || exit 44
    fi
    mv -f "$lock/new" "$file"
    sync
fi
printf 'VMCTL_KEYS %s %s\n' "$added" "$unchanged"
"""


def key_fingerprints(public_key: str) -> tuple[str, ...]:
    """Return one SHA256 fingerprint for each distinct OpenSSH key identity."""
    identities = dict.fromkeys(tuple(line.split()[:2]) for line in public_key.splitlines())
    return tuple(
        "SHA256:"
        + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
        for _, blob in identities
    )


def guest_merge_arguments(username: str, public_key: str) -> tuple[str, ...]:
    """Build an argument array; public data is quoted before entering the guest shell."""
    merge = GUEST_MERGE_SCRIPT.replace("VMCTL_PUBLIC_KEYS", shlex.quote(public_key))
    check = (
        'set -eu; test "$(id -u)" = 0 || exit 41; '
        f"uid=$(awk -F: -v name={shlex.quote(username)} '$1 == name {{print $3}}' /etc/passwd); "
        'test -n "$uid" && test "$uid" -ne 0 || exit 41; '
        f"exec su -s /bin/sh -c {shlex.quote(merge)} {shlex.quote(username)}"
    )
    return "/bin/sh", "-c", check


class KeyExportService:
    def __init__(self, config: Configuration, client: ProxmoxClient) -> None:
        self.config, self.client = config, client

    def plan(self, request: KeyExportRequest) -> KeyExportPreview:
        if not isinstance(request.public_key, str) or len(request.public_key) > 65536:
            raise VmctlError("SSH public keys must fit within 64 KiB")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in request.public_key):
            raise VmctlError("SSH public keys contain unsupported control characters")
        validated = validate_public_key(request.public_key)
        unique: dict[tuple[str, str], str] = {}
        for line in validated.splitlines():
            kind, blob, *_ = line.split()
            unique.setdefault((kind, blob), line)
        public_key = "\n".join(unique.values()) + "\n"
        username = request.username if request.username is not None else self.config.host.username
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
            raise VmctlError("SSH key export requires an existing non-root Linux username")
        normalized = replace(request, public_key=public_key, username=username)
        vm, config = inspect_vm(self.client, request.reference)
        if vm.status != "running":
            raise VmctlError("SSH key export requires a running VM")
        if not agent_enabled(config):
            raise VmctlError("SSH key export requires an enabled and available QEMU guest agent")
        try:
            data = self.client.get_os_info(vm.vmid)
        except VmctlError as exc:
            raise VmctlError("SSH key export requires an available QEMU guest agent") from exc
        result = data.get("result", data) if isinstance(data, dict) else None
        if not isinstance(result, dict) or result.get("id") not in {
            "ubuntu",
            "debian",
            "rocky",
            "rhel",
            "almalinux",
            "centos",
            "fedora",
            "alpine",
        }:
            raise VmctlError(
                "SSH key export requires a supported Linux guest (Debian, RHEL or Alpine family)"
            )
        return KeyExportPreview(
            vm,
            normalized,
            confirmation_fingerprint(vm, config, {"request": asdict(normalized), "os": result}),
            (
                "Existing non-root guest user required; existing keys and restrictions are preserved",
            ),
        )

    def export(self, expected: KeyExportPreview, *, progress: Progress) -> KeyExportResult:
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            current = self.plan(expected.request)
            if current != expected:
                raise VmctlError(
                    "VM or SSH key export settings changed since confirmation; inspect and retry"
                )
            assert current.request.username is not None
            progress("Adding SSH public keys through the QEMU guest agent")
            try:
                pid = self.client.guest_exec(
                    current.vm.vmid,
                    guest_merge_arguments(current.request.username, current.request.public_key),
                )
            except VmctlError as exc:
                # Submission errors may be transport interruptions after guest execution.
                raise UncertainOperationError(current.vm.vmid, "SSH key export") from exc
            added, unchanged = self._wait(current.vm.vmid, pid)
            if added + unchanged != len(key_fingerprints(current.request.public_key)):
                raise UncertainOperationError(current.vm.vmid, "SSH key export")
            return KeyExportResult(
                current.vm,
                current.request.username,
                added,
                unchanged,
                key_fingerprints(current.request.public_key),
            )

    def _wait(self, vmid: int, pid: int) -> tuple[int, int]:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                state = self.client.guest_exec_status(vmid, pid)
            except VmctlError as exc:
                raise UncertainOperationError(vmid, "SSH key export") from exc
            if not isinstance(state, dict) or type(state.get("exited")) not in {bool, int}:
                raise UncertainOperationError(vmid, "SSH key export")
            if state["exited"] not in {0, 1}:
                raise UncertainOperationError(vmid, "SSH key export")
            if not state["exited"]:
                time.sleep(0.2)
                continue
            if state.get("exitcode") != 0 or type(state.get("exitcode")) is not int:
                code = state.get("exitcode")
                detail = (
                    {
                        41: "Guest user must exist and be non-root",
                        42: "Guest SSH path is unsafe (ownership, links or permissions)",
                        43: "Guest key enrollment is locked; inspect an earlier export before retrying",
                        44: "Guest authorized_keys changed during enrollment; retry after other edits complete",
                    }.get(code, "Guest key enrollment failed; guest output was withheld")
                    if isinstance(code, int)
                    else "Guest key enrollment returned no valid exit code"
                )
                raise VmctlError(detail)
            output = state.get("out-data")
            match = (
                re.fullmatch(r"VMCTL_KEYS ([0-9]+) ([0-9]+)\n?", output)
                if isinstance(output, str)
                else None
            )
            if match is None:
                raise UncertainOperationError(vmid, "SSH key export")
            return int(match[1]), int(match[2])
        raise UncertainOperationError(vmid, "SSH key export")
