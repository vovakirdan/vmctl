#!/bin/sh
# Runs inside the guest only. Packages and versions come from editable module arguments.
set -eu
username=$1
package=$2
version=$3
executable=$4
# Login su supplies the cloud user's HOME. Arguments are positional, never shell interpolated.
# Engine checks turn an unsupported Node.js pin into an installation error.
su -l -s /bin/sh -c '
    set -eu
    npm install --global --prefix "$HOME/.local" --engine-strict --no-audit --no-fund --userconfig=/dev/null --registry=https://registry.npmjs.org/ "$1@$2"
    test -x "$HOME/.local/bin/$3"
' -- "$username" sh "$package" "$version" "$executable"
