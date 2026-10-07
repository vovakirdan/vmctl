#!/bin/sh
# Rocky 9 uses Docker's RHEL 9-compatible repository; live guest acceptance is separate.
set -eu
[ "$1" = stable ] || { echo 'Only the stable Docker channel is supported by this script' >&2; exit 1; }
. /etc/os-release
[ "$ID" = rocky ] && [ "${VERSION_ID%%.*}" = 9 ] || { echo 'Expected Rocky Linux 9' >&2; exit 1; }
repo_temp=$(mktemp /etc/yum.repos.d/.docker.XXXXXX)
trap 'rm -f "$repo_temp"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL https://download.docker.com/linux/rhel/docker-ce.repo -o "$repo_temp"
chmod 0644 "$repo_temp"
mv "$repo_temp" /etc/yum.repos.d/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version
