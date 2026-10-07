#!/bin/sh
# Runs inside the guest only, never on the Proxmox host.
set -eu
username=$1
version=$2
home_directory=$(getent passwd "$username" | cut -d: -f6)
[ -n "$home_directory" ] || { echo 'Cloud user home was not found' >&2; exit 1; }
install -d -m 0700 -o "$username" "$home_directory/.vmctl"
curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$home_directory/.vmctl/rustup-install.sh"
chown "$username" "$home_directory/.vmctl/rustup-install.sh"
# Renderer validates version syntax before passing it to this guest-side shell.
su -s /bin/sh "$username" -c "sh \"\$HOME/.vmctl/rustup-install.sh\" -y --no-modify-path --default-toolchain '$version'"
profile="$home_directory/.profile"
[ ! -L "$profile" ] || { echo 'Refusing a symlink profile' >&2; exit 1; }
profile_temp=$(mktemp "$home_directory/.profile.vmctl.XXXXXX")
trap 'rm -f "$profile_temp"' EXIT HUP INT TERM
if [ -f "$profile" ]; then
    cp -p "$profile" "$profile_temp"
else
    chmod 0644 "$profile_temp"
fi
if ! grep -Fq '. "$HOME/.cargo/env"' "$profile_temp"; then
    printf '\n. "$HOME/.cargo/env"\n' >> "$profile_temp"
fi
chown "$username" "$profile_temp"
mv "$profile_temp" "$profile"
su -s /bin/sh "$username" -c '"$HOME/.cargo/bin/rustc" --version'
