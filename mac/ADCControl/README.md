# ADCControl for macOS

Native Apple Silicon macOS controller for the Apple Studio Display 17" CRT (M7768).

This is a sibling implementation to the Raspberry Pi controller. It uses:

- USB HID (`05ac:9213`) for geometry controls, degauss, save, vertical refresh, and power reads.
- Apple Silicon DDC/CI via CoreDisplay/IOAVService for power writes (`VCP 0xD6`, `1` on, `4` off).
- CoreGraphics display modes for resolution switching.
- SwiftUI for the desktop utility window.

## One-click app

```bash
make app
```

Then double-click:

```text
mac/ADCControl/dist/ADC CRT Control.app
```

The app is ad-hoc signed locally and is intentionally not sandboxed because direct HID and Apple Silicon DDC access are required.

## Display targeting

The app and CLI explicitly target the external Apple Studio Display only: EDID/DDC and resolution
operations require an external `0610:9213` display and refuse the built-in MacBook panel.

If macOS Mirror Displays is enabled, macOS still treats the built-in panel and Studio Display as one
mirror set. The app preserves the existing mirrored/unmirrored relationship around EDID and mode
changes; it does not switch mirroring on or off.

## Build

```bash
cd mac/ADCControl
swift build
swift run adcctl self-test
```

## Diagnostics

```bash
swift run adcctl hid list
swift run adcctl hid read 0x12
swift run adcctl hid read 0xAE
swift run adcctl hid set 0x12 95
swift run adcctl ddc list
swift run adcctl display current
swift run adcctl display modes
swift run adcctl display oem
swift run adcctl power read
```

The app's resolution picker intentionally lists only Apple-listed Studio Display targets. The OEM
status table marks each target as ready, profile-backed, fallback-only, blocked, or not exposed, and
blocked/fallback rows include an info button with the reason.

`adcctl display modes` still prints every desktop mode macOS exposes and marks Apple-listed Studio
Display resolutions inline:

- `OEM` means the exposed desktop mode matches the recommended size and refresh closely.
- `OEM size` means the size is exposed, but macOS/the adapter reports a different refresh timing.

The OEM recommendation scan uses Apple's documented list:

```text
640x480 @ 153 Hz
800x600 @ 125 Hz
832x624 @ 120 Hz
1024x768 @ 99 Hz
1056x792 @ 96 Hz
1280x1024 @ 75 Hz
1600x1200 @ 65 Hz
```

Power writes are explicit commands:

```bash
swift run adcctl power on
swift run adcctl power off
```

Resolution changes are explicit:

```bash
swift run adcctl display set <mode-id>
```

Selecting an OEM target in the app can use a tested 2-block runtime EDID profile when macOS is only
exposing a fallback mode or is not exposing that target. The profile advertises all Apple OEM timings
together, so macOS can publish every OEM mode except `640x480 @ 153 Hz`, which still appears only as a
blocked legacy/non-desktop mode on this path.

To try each desktop-usable OEM-sized mode and restore the original mode:

```bash
swift run adcctl display test-oem
```

The Studio Display can expose 640x480 as a legacy, non-desktop mode. The app does not offer that as an
applyable mode because macOS rejects it as a live desktop signal. The matching CLI probes are:

```bash
swift run adcctl display test-legacy-640 5
swift run adcctl display test-legacy-640-config 5
```

On this tested Apple Silicon path, both public CoreGraphics mode-setting routes currently reject the
legacy 640x480 mode with `1001`, even though the mode appears in the mode list.

The override experiment can also be pushed through IOAVService's runtime virtual EDID path:

```bash
swift run adcctl ddc virtual-edid /path/to/edid.bin
swift run adcctl ddc reset-edid
```

That path is experimental and may still require a replug or reboot before CoreGraphics rebuilds its
desktop mode list. In the app, only Apple OEM targets are wired into the selector.

The persistent display override installer has two profiles:

```bash
./install-display-override.sh 640-153
./install-display-override.sh 800-125
```

The `800-125` profile uses Apple's own 800x600 descriptor. macOS may print it as `124 Hz`; that is the
rounded value of the OEM timing, not a separate fallback mode.

## App

Swift Package Manager can also launch the utility executable directly:

```bash
swift run ADCControlApp
```
