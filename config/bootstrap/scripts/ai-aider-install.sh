#!/bin/sh
# Runs inside the guest only. uv keeps Aider separate from the system Python environment.
set -eu
username=$1
package=$2
version=$3
python_version=$4
su -l -s /bin/sh -c '
    set -eu
    "$HOME/.local/bin/uv" tool install --force --python "$3" --with pip "$1@$2"
    test -x "$HOME/.local/bin/aider"
' -- "$username" sh "$package" "$version" "$python_version"
