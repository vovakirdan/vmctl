#!/bin/sh
# Runs inside the guest only, never on the Proxmox host.
set -eu
username=$1
home_directory=$(getent passwd "$username" | cut -d: -f6)
[ -n "$home_directory" ] || { echo 'Cloud user home was not found' >&2; exit 1; }
[ ! -L "$home_directory/.vmctl" ] || { echo 'Refusing a symlink bootstrap directory' >&2; exit 1; }
install -d -m 0700 -o "$username" "$home_directory/.vmctl"
installer_temp=$(mktemp "$home_directory/.vmctl/uv-install.XXXXXX")
trap 'rm -f "$installer_temp"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/install.sh -o "$installer_temp"
chmod 0600 "$installer_temp"
chown "$username" "$installer_temp"
mv -f "$installer_temp" "$home_directory/.vmctl/uv-install.sh"
# Login su initializes HOME for the account instead of inheriting root's environment.
# The upstream installer runs without privileges. The renderer writes the login PATH fragment atomically.
su -l -s /bin/sh "$username" -c 'UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$HOME/.vmctl/uv-install.sh"'
su -l -s /bin/sh "$username" -c '"$HOME/.local/bin/uv" --version && python3 -m pip --version'
