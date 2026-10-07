#!/bin/sh
# Download the official native installer and execute it only as the cloud user.
set -eu
username=$1
version=$2
home_directory=$(getent passwd "$username" | cut -d: -f6)
[ -n "$home_directory" ] || { echo 'Cloud user home was not found' >&2; exit 1; }
[ ! -L "$home_directory/.vmctl" ] || { echo 'Refusing a symlink bootstrap directory' >&2; exit 1; }
install -d -m 0700 -o "$username" "$home_directory/.vmctl"
installer_temp=$(mktemp "$home_directory/.vmctl/claude-install.XXXXXX")
trap 'rm -f "$installer_temp"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL https://claude.ai/install.sh -o "$installer_temp"
chmod 0600 "$installer_temp"
chown "$username" "$installer_temp"
mv -f "$installer_temp" "$home_directory/.vmctl/claude-install.sh"
su -l -s /bin/sh -c '
    set -eu
    bash "$HOME/.vmctl/claude-install.sh" "$1"
    test -x "$HOME/.local/bin/claude"
' -- "$username" sh "$version"
