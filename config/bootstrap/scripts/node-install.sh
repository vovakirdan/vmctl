#!/bin/sh
# Install official upstream binaries and verify the published SHA-256 checksum.
set -eu
version=$1
case "$(uname -m)" in
    x86_64) architecture=x64 ;;
    aarch64) architecture=arm64 ;;
    *) echo 'Unsupported Node.js guest architecture' >&2; exit 1 ;;
esac
if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
    echo 'This Node.js module requires glibc; supply an Alpine-specific module instead' >&2
    exit 1
fi
work_directory=$(mktemp -d)
trap 'rm -rf "$work_directory"' EXIT HUP INT TERM
if [ "$version" = lts ]; then
    curl --proto '=https' --tlsv1.2 -fsSL https://nodejs.org/dist/index.tab -o "$work_directory/index.tab"
    version=$(awk 'NR > 1 && $10 != "-" { print $1; exit }' "$work_directory/index.tab")
fi
case "$version" in v*) ;; *) version="v$version" ;; esac
printf '%s\n' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo 'Expected an exact Node.js version or lts' >&2; exit 1; }
archive="node-$version-linux-$architecture.tar.xz"
base_url="https://nodejs.org/dist/$version"
curl --proto '=https' --tlsv1.2 -fsSL "$base_url/$archive" -o "$work_directory/$archive"
curl --proto '=https' --tlsv1.2 -fsSL "$base_url/SHASUMS256.txt" -o "$work_directory/SHASUMS256.txt"
(cd "$work_directory" && grep -F "  $archive" SHASUMS256.txt | sha256sum -c -)
install -d /usr/local/lib/nodejs
tar -xJf "$work_directory/$archive" -C /usr/local/lib/nodejs
for binary in node npm npx; do
    ln -sf "/usr/local/lib/nodejs/node-$version-linux-$architecture/bin/$binary" "/usr/local/bin/$binary"
done
node --version
npm --version
