#!/bin/sh
set -e
# Convenience entry. Canonical implementation: scripts/install.sh
CANONICAL_URL="https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.sh"
# Only a real on-disk wrapper named install.sh may exec the checkout copy.
# Piped `curl | sh` must not pick up a coincidental ./scripts/install.sh.
case "$0" in
    */install.sh | install.sh)
        if [ -f "$0" ]; then
            SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
            if [ -f "$SCRIPT_DIR/scripts/install.sh" ]; then
                exec sh "$SCRIPT_DIR/scripts/install.sh" "$@"
            fi
        fi
        ;;
esac
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
curl -fsSL -A "gjc-install" "$CANONICAL_URL" -o "$TMP"
read first_line < "$TMP" || true
case "$first_line" in
    "#!/bin/sh" | "#!/bin/bash") ;;
    *)
        echo "Refusing to run unexpected installer payload from $CANONICAL_URL" >&2
        exit 1
        ;;
esac
sh "$TMP" "$@"
status=$?
rm -f "$TMP"
trap - EXIT
exit "$status"
