#!/bin/sh
# Official Docker CE repository, configured only inside a disposable guest.
set -eu
[ "$1" = stable ] || { echo 'Only the stable Docker channel is supported by this script' >&2; exit 1; }
. /etc/os-release
case "$ID:$VERSION_CODENAME" in
    ubuntu:noble|debian:trixie) ;;
    *) echo 'Unsupported distribution for this Docker module' >&2; exit 1 ;;
esac
install -m 0755 -d /etc/apt/keyrings
key_temp=$(mktemp /etc/apt/keyrings/.docker.XXXXXX)
sources_temp=$(mktemp /etc/apt/sources.list.d/.docker.XXXXXX)
trap 'rm -f "$key_temp" "$sources_temp"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$key_temp"
chmod 0644 "$key_temp"
mv "$key_temp" /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "$VERSION_CODENAME" "$(dpkg --print-architecture)" > "$sources_temp"
chmod 0644 "$sources_temp"
mv "$sources_temp" /etc/apt/sources.list.d/docker.sources
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version
