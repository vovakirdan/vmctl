#!/bin/sh
# Guest-only official desktop packages. Install without launching or authenticating apps.
set -eu
application=$1
. /etc/os-release
case "$ID:$VERSION_ID" in
    ubuntu:24.04|ubuntu:26.04|debian:13) ;;
    *) echo 'This desktop package requires Ubuntu 24.04/26.04 or Debian 13' >&2; exit 1 ;;
esac
architecture=$(dpkg --print-architecture)
case "$architecture" in
    amd64|arm64) ;;
    *) echo 'Official desktop packages require amd64 or arm64' >&2; exit 1 ;;
esac
case "$application" in
    chatgpt)
        package_name=chatgpt
        download_url="https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_${architecture}.deb"
        ;;
    opencode)
        package_name=opencode
        # The official stable redirect exists for x64. Resolve its version with a one-byte GET;
        # HEAD and an arm64 stable redirect are not supported by the download service.
        stable_url=$(curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
            --connect-timeout 20 --max-time 60 --range 0-0 -o /dev/null -w '%{url_effective}' \
            https://opencode.ai/download/stable/linux-x64-deb)
        case "$stable_url" in
            https://opencode.ai/files/bin/*/opencode-desktop-linux-amd64.deb) ;;
            *) echo 'OpenCode returned an unexpected official release URL' >&2; exit 1 ;;
        esac
        release_version=${stable_url#https://opencode.ai/files/bin/}
        release_version=${release_version%/opencode-desktop-linux-amd64.deb}
        case "$release_version" in
            ''|*[!0-9.]*|.*|*.) echo 'OpenCode returned an invalid release version' >&2; exit 1 ;;
        esac
        download_url="https://opencode.ai/files/bin/$release_version/opencode-desktop-linux-${architecture}.deb"
        ;;
    *) echo 'Unknown desktop package' >&2; exit 1 ;;
esac
download_directory=$(mktemp -d)
trap 'rm -rf "$download_directory"' EXIT HUP INT TERM
archive="$download_directory/${package_name}.deb"
curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
    --connect-timeout 20 --max-time 600 "$download_url" -o "$archive"
[ "$(dpkg-deb --field "$archive" Package)" = "$package_name" ] || {
    echo 'Downloaded desktop package has an unexpected package name' >&2; exit 1;
}
[ "$(dpkg-deb --field "$archive" Architecture)" = "$architecture" ] || {
    echo 'Downloaded desktop package has an unexpected architecture' >&2; exit 1;
}
package_version=$(dpkg-deb --field "$archive" Version)
[ -n "$package_version" ] || { echo 'Downloaded package has no version' >&2; exit 1; }
if [ "$application" = opencode ]; then
    [ "$package_version" = "$release_version" ] || {
        echo 'Downloaded OpenCode package does not match the selected stable release' >&2; exit 1;
    }
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends "$archive"
[ "$(dpkg-query -W -f='${Status}' "$package_name")" = 'install ok installed' ] || {
    echo 'Desktop package was not installed successfully' >&2; exit 1;
}
[ "$(dpkg-query -W -f='${Architecture}' "$package_name")" = "$architecture" ] || {
    echo 'Installed desktop package architecture does not match the guest' >&2; exit 1;
}
[ "$(dpkg-query -W -f='${Version}' "$package_name")" = "$package_version" ] || {
    echo 'Installed desktop package version does not match the download' >&2; exit 1;
}
