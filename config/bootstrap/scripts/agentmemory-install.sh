#!/bin/sh
# Install only client assets; never start a guest memory server or modify global Node.
set -eu
username=$1
package_version=$2
node_version=$3
home_directory=$(getent passwd "$username" | cut -d: -f6)
[ -n "$home_directory" ] && [ "$username" != root ] || exit 1
printf '%s\n' "$package_version" "$node_version" | grep -Eqv '^[0-9]+\.[0-9]+\.[0-9]+$' && exit 1
root="$home_directory/.local/share/vmctl/agentmemory"
install -d -m 0700 -o "$username" -g "$username" "$root"
node_binary=$(su - "$username" -s /bin/sh -c 'PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; command -v node' || true)
if [ -z "$node_binary" ] || ! "$node_binary" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)'; then
    case "$(uname -m)" in x86_64) architecture=x64 ;; aarch64) architecture=arm64 ;; *) echo 'Unsupported agentmemory runtime architecture' >&2; exit 1 ;; esac
    work_directory=$(mktemp -d)
    trap 'rm -rf "$work_directory"' EXIT HUP INT TERM
    archive="node-v$node_version-linux-$architecture.tar.xz"
    base_url="https://nodejs.org/dist/v$node_version"
    curl --proto '=https' --tlsv1.2 -fsSL "$base_url/$archive" -o "$work_directory/$archive"
    curl --proto '=https' --tlsv1.2 -fsSL "$base_url/SHASUMS256.txt" -o "$work_directory/SHASUMS256.txt"
    (cd "$work_directory" && grep -F "  $archive" SHASUMS256.txt | sha256sum -c -)
    install -d -m 0700 -o "$username" -g "$username" "$root/runtime"
    tar -xJf "$work_directory/$archive" --strip-components=1 -C "$root/runtime"
    chown -R "$username:$username" "$root/runtime"
    node_binary="$root/runtime/bin/node"
fi
# Prefer npm beside Node; distribution packages keep npm under /usr/share/nodejs.
node_binary=$(readlink -f "$node_binary")
npm_script="$(dirname "$(dirname "$node_binary")")/lib/node_modules/npm/bin/npm-cli.js"
if [ ! -f "$npm_script" ]; then
    npm_binary=$(su - "$username" -s /bin/sh -c 'PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; command -v npm' || true)
    [ -n "$npm_binary" ] || { echo 'Selected Node runtime has no npm installation' >&2; exit 1; }
    npm_script=$(readlink -f "$npm_binary")
    case "$npm_script" in */npm-cli.js) ;; *) echo 'Unsupported npm entrypoint' >&2; exit 1 ;; esac
    [ -f "$npm_script" ] || exit 1
fi
# Arguments contain versions and paths, never credentials.
runuser -u "$username" -- env HOME="$home_directory" PATH="$(dirname "$node_binary"):$PATH" "$node_binary" "$npm_script" install --prefix "$root/client" --omit=optional --ignore-scripts --no-audit --no-fund "@agentmemory/agentmemory@$package_version"
printf '%s\n' "$node_binary" > "$root/.node-path.pending"
chown "$username:$username" "$root/.node-path.pending"
chmod 0600 "$root/.node-path.pending"
mv -fT "$root/.node-path.pending" "$root/node-path"
