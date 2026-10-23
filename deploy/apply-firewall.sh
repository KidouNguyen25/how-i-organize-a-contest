#!/bin/sh
set -eu

usage() {
    printf '%s\n' "Usage: $0 [check | apply --yes]" >&2
    exit 2
}

mode=${1:-check}
case "$mode" in
    check)
        [ "$#" -le 1 ] || usage
        ;;
    apply)
        [ "$#" -eq 2 ] && [ "$2" = "--yes" ] || usage
        ;;
    *)
        usage
        ;;
esac

[ "$(id -u)" -eq 0 ] || { printf '%s\n' "Run as root." >&2; exit 1; }
command -v nft >/dev/null 2>&1 || { printf '%s\n' "nft is required." >&2; exit 1; }

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rules_file=$script_dir/firewall.nft
[ -r "$rules_file" ] || { printf '%s\n' "Cannot read $rules_file." >&2; exit 1; }

candidate=$(mktemp)
trap 'rm -f "$candidate"' 0
trap 'exit 1' HUP INT TERM
if nft list table inet contest_demo >/dev/null 2>&1; then
    printf '%s\n' 'delete table inet contest_demo' >"$candidate"
else
    : >"$candidate"
fi
cat "$rules_file" >>"$candidate"

nft -c -f "$candidate"
if [ "$mode" = check ]; then
    printf '%s\n' "Firewall rules valid. No rules changed."
    exit 0
fi

nft -f "$candidate"
printf '%s\n' "Firewall table inet contest_demo applied."
