#!/bin/sh
set -eu

TARGET="/Library/Displays/Contents/Resources/Overrides/DisplayVendorID-610/DisplayProductID-9213"

if [ -e "$TARGET" ]; then
	rm -f "$TARGET"
	echo "Removed $TARGET"
else
	echo "No override installed at $TARGET"
fi
