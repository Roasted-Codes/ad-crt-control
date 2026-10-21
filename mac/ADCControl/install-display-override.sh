#!/bin/sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="${1:-640-153}"

case "$PROFILE" in
	640|640-153)
		SOURCE="$ROOT_DIR/Overrides/DisplayVendorID-610/DisplayProductID-9213"
		PROFILE_LABEL="640-153"
		;;
	800|800-125)
		SOURCE="$ROOT_DIR/Overrides/Profiles/800-125/DisplayVendorID-610/DisplayProductID-9213"
		PROFILE_LABEL="800-125"
		;;
	*)
		echo "usage: $0 [640-153|800-125]" >&2
		exit 2
		;;
esac

TARGET_DIR="/Library/Displays/Contents/Resources/Overrides/DisplayVendorID-610"
TARGET="$TARGET_DIR/DisplayProductID-9213"

install -d -m 0755 "$TARGET_DIR"
install -m 0644 "$SOURCE" "$TARGET"
plutil -lint "$TARGET" >/dev/null
echo "Installed $PROFILE_LABEL profile to $TARGET"
