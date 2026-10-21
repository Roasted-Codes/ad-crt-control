# CLAUDE.md

Guidance for Claude Code sessions in this repository.

## What this is

These tools control an Apple Studio Display 17" CRT (ADC; M7768, which Apple's spec page calls M7770) through Jason Olshefsky's ADC adapter board. Without an old ADC Mac, the monitor has no power control. Two paths each do half the job:

- **USB HID** (`05ac:9213`) reads state and sets geometry, but power (`0xD6`) is read-only.
- **DDC/CI** (I2C `0x37`) sets power but can't read it back.

Both implementations **read state over HID and write power over DDC**. The protocol reference is `docs/protocol.md`, and the hardware notes are in `docs/hardware.md`.

## Layout

- `linux/`: Python package `adc-crt-control` for Raspberry Pi and desktop Linux (CachyOS/Arch).
  - `adc_crt/protocol.py`: constants, control table, packets, parsing. Pure functions, no I/O.
  - `adc_crt/hid.py`, `adc_crt/ddc.py`: transports.
  - `adc_crt/monitor.py`: the `Monitor` API used by everything else.
  - `adc_crt/cli.py`: the `adc-crt` command.
  - `adc_crt/gui.py`, `adc_crt/buttons.py`, `adc_crt/poweron.py`: GUI, button listener, boot power-on.
  - `adc_crt/config.py`: env > `~/.config/adc-crt-control/config.ini` > `/etc/adc-crt-control.conf`. `adc_crt/keybinding.py`: labwc keybinding helper.
  - `bin/adc-crt`: the only launcher.
  - `data/`: udev, systemd, modules-load and .desktop files.
  - `packaging/debian`, `packaging/arch`: distribution packaging.
  - `tests/`: unittest only, no hardware needed.
  - Python 3.9+. The core is stdlib-only; the GUI needs PyGObject and GTK 3.
- `mac/ADCControl/`: SwiftPM package with three targets: `ADCMonitorCore` (library), `adcctl` (diagnostic CLI) and `ADCControlApp` (SwiftUI). Needs macOS 13+ on Apple Silicon. **It works; don't change its behavior unless asked.**
- `docs/`: `protocol.md`, `hardware.md`, `images/`.
- `README.md`, `CREDITS.md`, `LICENSE` (MIT).

The old Pi-only v2.x layout is at git tag `pi-v2.1`.

## Build and test

```bash
# Linux
make -C linux test            # unit tests (python3 -m unittest discover -s linux/tests)
make -C linux check           # see linux/Makefile
make -C linux deb             # -> linux/dist/adc-crt-control_<version>_all.deb
linux/bin/adc-crt self-test   # offline: constants, packets, parsing, EDID fixture
adc-crt doctor                # on real hardware: read-only checklist with fixes

# Mac
cd mac/ADCControl
make build                    # swift build
make test                     # swift run adcctl self-test
make app                      # builds + ad-hoc signs dist/ADC CRT Control.app
swift run adcctl hid list     # confirm 05ac:9213 is visible
swift run adcctl hid read 0x12
swift run adcctl ddc list     # DDC-capable displays + AV service status
swift run adcctl power read   # 1=on, 2=sleep, 4=off
swift run adcctl display oem  # OEM target readiness table
swift run adcctl display test-oem  # cycle each OEM mode, restore original
```

## The shared-contract rule

The control table, factory defaults, HID codes and DDC power packets are **duplicated by design**. If you change any of them, change all three of these places:

1. `linux/adc_crt/protocol.py`
2. `mac/ADCControl/Sources/ADCMonitorCore/ADCControl.swift` (`ADCControls.all`, `ADCUsage`) and `DDCPacket` in `MacDDCPowerTransport.swift` for packets
3. `docs/protocol.md`

Then run `make -C linux test` (`tests/test_protocol.py` parses the Swift table) and `swift run adcctl self-test`. The Mac self-test only spot-checks literals in `Sources/adcctl/main.swift`, so update those too if needed.

## Linux rules

- Everything runs as the **logged-in user**: no sudo, no sudoers file, no hardcoded UID or Wayland environment. The only root process is the boot-time `adc-crt-poweron.service`. Device access comes from the udev rule (`uaccess`) and, on a Pi, the `i2c` group.
- **Never probe I2C buses other than the ones listed below.** A desktop's SMBus has RAM SPD EEPROMs at `0x50`. The allowed buses are:
  - a configured bus
  - Pi bus 1
  - DRM connector buses whose EDID is the Studio Display
- Only one process may hold the hiddev node. Two button listeners split the events randomly.

## Mac notes

- Apple Silicon DDC uses **private symbols bound with `@_silgen_name`**: `IOAVServiceCreate`, `IOAVServiceCreateWithService`, `IOAVServiceReadI2C`, `IOAVServiceWriteI2C`, `IOAVServiceCopyEDID`, `IOAVServiceSetVirtualEDIDMode` and `CoreDisplay_DisplayCreateInfoDictionary`. They are undocumented and can break across macOS releases, and there is no public equivalent.
- The app is deliberately **not sandboxed and only ad-hoc signed**, because direct HID and IOAVService access require that.
- Parts of `MacDDCPowerTransport.swift` are adapted from m1ddc (MIT). Keep its header comment, and keep the notice in `CREDITS.md`.
- Resolution work: CoreGraphics mode switching against Apple's seven OEM timings (`640x480@153` … `1600x1200@65`). A runtime **virtual EDID** (`ADCEdidProfiles`) advertises all OEM timings so macOS publishes modes it otherwise hides.
- Two behaviors look like bugs but are intentional:
  - **`640x480@153` cannot be applied.** It is listed, but both public CoreGraphics mode-setting routes reject it with error `1001`, so it is exposed only as a legacy non-desktop mode.
  - **Mirroring is preserved, never changed.** `MacDisplayMirrorState` captures and restores the mirror relationship around EDID and mode changes.
- All Mac operations refuse the built-in MacBook panel and require an external `0610:9213` display.

## Pi gotchas

- **Powering off the CRT can make the Pi drop HDMI and go headless**, and recovery needs a graphical-session restart. The GUI's 5 s cancellable power-off countdown exists for this reason. Don't remove it.
- **Never bind Ctrl+Alt+F-keys.** They collide with Linux VT switching and strand the user on an invisible virtual terminal.
- labwc (Wayland) on Raspberry Pi OS has no working `Gtk.StatusIcon` tray, so the button listener is headless.

## Repository hygiene

- This repo sits inside a larger working directory that also has `_reference/` (upstream clones, the service manual, EDID dumps) and `_archive/` folders. **Never commit material from `_reference/` or `_archive/`.**
- Don't run git commands that discard untracked files (`git clean`, `git checkout .`) without asking James first.
