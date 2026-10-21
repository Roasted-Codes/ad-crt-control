# Display Override Experiments

`DisplayVendorID-610/DisplayProductID-9213` is a reversible user-level override for the Apple Studio Display 17" CRT.

It starts from Apple's built-in `0610:9213` override, then:

- embeds a patched 128-byte EDID with `640x480 @ 153.62 Hz` as detailed timing slot 1,
  making it the preferred timing macOS sees first;
- demotes the original `1600x1200 @ 65 Hz` timing to detailed timing slot 2;
- repeats those timing edits as `edid-patches` entries;
- moves the Apple-provided `640x480 @ 153.62 Hz` `dspc` descriptor to the front of the list.

Install path:

```text
/Library/Displays/Contents/Resources/Overrides/DisplayVendorID-610/DisplayProductID-9213
```

Rollback path:

```text
/Library/Displays/Contents/Resources/Overrides/DisplayVendorID-610/DisplayProductID-9213
```

After installing or removing it, unplug/replug the display chain or reboot before checking `adcctl display modes`.

## Profiles

The default profile is `640-153`:

```bash
./install-display-override.sh 640-153
```

The `800-125` profile embeds Apple's built-in `800x600 @ 125 Hz` descriptor as the preferred detailed
timing and keeps `1600x1200 @ 65 Hz` as the second detailed timing:

```bash
./install-display-override.sh 800-125
```

macOS reports the Apple `800x600 @ 125 Hz` descriptor as roughly `124 Hz` because the underlying timing
is about 124.48 Hz. The app treats that as the documented OEM `800x600 @ 125 Hz` mode.
