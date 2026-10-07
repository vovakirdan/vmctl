#!/bin/sh
# Guest-only official Anthropic apt repository. No GUI launch, login or credentials.
set -eu
. /etc/os-release
case "$ID:$VERSION_ID" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:25.10|ubuntu:26.04|debian:12|debian:13) ;;
    *) echo 'Claude Desktop requires a supported modern Ubuntu or Debian desktop' >&2; exit 1 ;;
esac
architecture=$(dpkg --print-architecture)
case "$architecture" in
    amd64|arm64) ;;
    *) echo 'Claude Desktop requires amd64 or arm64' >&2; exit 1 ;;
esac
key_directory=/usr/share/keyrings
repository_directory=/etc/apt/sources.list.d
key_target="$key_directory/claude-desktop-archive-keyring.asc"
repository_target="$repository_directory/claude-desktop.list"
[ ! -L "$key_target" ] && [ ! -L "$repository_target" ] || {
    echo 'Refusing symlink Claude Desktop repository configuration' >&2; exit 1;
}
install -d -m 0755 "$key_directory" "$repository_directory"
key_pending=''
repository_pending=''
gpg_directory=''
trap 'rm -f "$key_pending" "$repository_pending"; rm -rf "$gpg_directory"' EXIT HUP INT TERM
key_pending=$(mktemp "$key_directory/.claude-desktop-key.XXXXXX")
repository_pending=$(mktemp "$repository_directory/.claude-desktop-repository.XXXXXX")
gpg_directory=$(mktemp -d)
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
    --connect-timeout 20 --max-time 60 \
    https://downloads.claude.ai/claude-desktop/key.asc -o "$key_pending"
key_metadata=$(gpg --batch --homedir "$gpg_directory" --with-colons --show-keys "$key_pending")
public_keys=$(printf '%s\n' "$key_metadata" | awk -F: '$1 == "pub" { count++ } END { print count+0 }')
fingerprint=$(printf '%s\n' "$key_metadata" | awk -F: '$1 == "fpr" { print $10; exit }')
[ "$public_keys" = 1 ] && [ "$fingerprint" = 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE ] || {
    echo 'Claude Desktop signing key fingerprint does not match the official key' >&2; exit 1;
}
printf '%s\n' "deb [arch=$architecture signed-by=$key_target] https://downloads.claude.ai/claude-desktop/apt/stable stable main" > "$repository_pending"
chmod 0644 "$key_pending" "$repository_pending"
chown root:root "$key_pending" "$repository_pending"
# Each destination is replaced atomically on its own filesystem after key validation.
mv -fT -- "$key_pending" "$key_target"
mv -fT -- "$repository_pending" "$repository_target"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends claude-desktop
[ "$(dpkg-query -W -f='${Status}' claude-desktop)" = 'install ok installed' ] || {
    echo 'Claude Desktop was not installed successfully' >&2; exit 1;
}
[ "$(dpkg-query -W -f='${Architecture}' claude-desktop)" = "$architecture" ] || {
    echo 'Installed Claude Desktop architecture does not match the guest' >&2; exit 1;
}
