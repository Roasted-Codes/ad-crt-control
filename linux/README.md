# adc-crt-control for Linux

`adc-crt` controls an Apple Studio Display 17" CRT (ADC) connected through the Jason "Does It All" ADC adapter. It handles power on/off, the eleven geometry controls, degauss and save-to-NVRAM. It ships a command-line tool, a GTK 3 window, a listener for the monitor's front-panel button, and a boot service that powers the monitor on.

It runs on:

- **Raspberry Pi OS Bookworm** (tested on a Pi 2B with labwc/Wayland). Packaged as a `.deb`.
- **CachyOS / Arch Linux** (any desktop, e.g. KDE Plasma). Packaged with a `PKGBUILD`.

Everything runs as your normal desktop user. There is no sudo and no sudoers file. A udev rule gives the logged-in session access to the monitor. The only root process is the optional boot-time power-on service.

## How it works

The monitor needs two paths, because each one only does half the job:

- **USB HID** (`05ac:9213`, the adapter's USB port) reads the monitor's state and sets geometry. It can read the power state but cannot change it.
- **DDC/CI** (I2C address `0x37`) can switch power on and off, but cannot read anything back.

So `adc-crt` reads state over HID and writes power over DDC. On a Raspberry Pi, DDC goes over the Pi's GPIO I2C bus 1, wired to the adapter's DVI port. On a desktop, it goes over the graphics card's own display connector. That is the same idea the Mac version uses, sending DDC over its video cable. The full protocol is in [`../docs/protocol.md`](../docs/protocol.md).

## Install on Raspberry Pi OS

1. **Wire the Pi to the adapter's DVI port.** Use four jumper wires. Full details and photos are in [`../docs/hardware.md`](../docs/hardware.md#raspberry-pi).

   | Pi pin | Function | DVI pin |
   |--------|----------|---------|
   | 2 | 5V | 14 |
   | 3 | SDA1 | 7 |
   | 5 | SCL1 | 6 |
   | 9 | GND | 15 |

   With the DVI port taken by the I2C wires, video goes out the adapter's **VGA** port, fed from the Pi's HDMI output through an HDMI-to-VGA adapter. The adapter's USB port goes to a Pi USB port.

2. **Enable I2C** and join the `i2c` group:

   ```bash
   sudo raspi-config nonint do_i2c 0
   sudo usermod -aG i2c $USER
   ```

3. **Build and install the package.** Run this from the repository root:

   ```bash
   sudo apt install make             # if missing; dpkg-deb comes with dpkg
   make -C linux deb
   sudo apt install ./linux/dist/adc-crt-control_3.0.0_all.deb
   ```

   If you run `make deb` from inside `linux/`, the package is `./dist/adc-crt-control_3.0.0_all.deb`.

   The package automatically replaces the old `pi-adc-control` 2.x. The old package's own removal script deletes its sudoers file, its `crt-daemon` service and its `crt-toggle` keybinding. It leaves an empty `<keybind key="C-A-p">` block behind in `/etc/xdg/labwc/rc.xml`. The new package removes exactly that block and keeps a backup at `/etc/xdg/labwc/rc.xml.adc-crt-control.bak`. Nothing else in the file is touched.

4. **Reboot** (or log out and back in). The boot service powers the monitor on, and the button listener starts with your desktop session.

5. **Check everything:**

   ```bash
   adc-crt doctor
   ```

6. **Optional: add a keyboard shortcut.** This binds Ctrl+Alt+P to `adc-crt power toggle` in your own `~/.config/labwc/rc.xml`, keeps a backup, and reloads labwc:

   ```bash
   adc-crt keybinding labwc            # show what would be added
   adc-crt keybinding labwc --install
   ```

The `.deb` enables `adc-crt-poweron.service` on first install. To turn it off: `sudo systemctl disable adc-crt-poweron.service`. Upgrades keep that choice.

## Install on CachyOS / Arch Linux

1. **Connect the monitor so DDC reaches it.** On a desktop there are no jumper wires. DDC travels over the graphics card's display connector, so **the DDC lines of the connector you use must reach the monitor through the adapter.** The usual setup is a DisplayPort- or HDMI-to-VGA adapter into the Jason board's VGA input. Many such adapters pass DDC through, but some cheap ones do not. Connect the adapter's USB port to the PC as well.

   If your video path cannot carry DDC, use a **USB-to-I2C adapter** wired to the Jason board's DVI pins exactly like the Pi (SDA → DVI 7, SCL → DVI 6, GND → DVI 15, and 5V → DVI 14 if the adapter provides it). Then tell `adc-crt` which bus it is with `--bus N` or the config file (see [Configuration](#configuration)). The tool never guesses or probes other buses. See [`../docs/hardware.md`](../docs/hardware.md#desktop-linux).

2. **Build and install the package.** Run this from the repository root:

   ```bash
   sudo pacman -S --needed base-devel
   cd linux/packaging/arch
   makepkg -si
   ```

   The `PKGBUILD` builds from your checked-out copy and downloads nothing.

3. **Load the I2C driver and refresh your session.** The package loads `i2c-dev` at every boot, but not now. Either reboot, or:

   ```bash
   sudo modprobe i2c-dev
   ```

   Then log out and back in. Your session gets device access through udev's `uaccess` tag, and the button listener starts.

4. **Optional: power on at boot.** Arch packages don't enable services on their own, so turn it on yourself:

   ```bash
   sudo systemctl enable adc-crt-poweron.service
   ```

5. **Check everything:**

   ```bash
   adc-crt doctor
   ```

   A working DDC path shows up as a bus with a source like `drm:card0-DP-1` and "Studio Display EDID verified".

6. **Optional: add a keyboard shortcut in KDE Plasma.** Go to System Settings → Keyboard → Shortcuts → Add New → Command or Script. Enter `adc-crt power toggle` and pick a key. Don't use Ctrl+Alt+F1…F12, because those switch virtual terminals.

## Usage

Start the GUI from the application menu (**ADC CRT Control**, under Settings), with `adc-crt gui`, or by pressing the monitor's front-panel brightness button. The button opens the window and a second press closes it. The GUI has a slider for each control, Degauss, Read, Write (save to NVRAM), Defaults, and a power switch. Switching power off starts a 5-second countdown you can cancel, because powering off the CRT can blank your only screen.

### Command reference

Global options, accepted before or after the subcommand: `--bus N` (I2C bus for DDC/CI) and `--hidraw PATH` (hidraw node). Both default to the config file, then auto-detection. Exit status is 0 for success, 1 for an error and 2 for a usage error.

| Command | What it does |
|---|---|
| `adc-crt doctor` | Read-only checklist of devices, permissions and the DDC bus, with a fix for every failure |
| `adc-crt self-test` | Offline check of constants, DDC packets, report parsing and the EDID fixture |
| `adc-crt hid list` | List the monitor's hidraw nodes and its hiddev node |
| `adc-crt hid read CODE` | Read one HID feature report, e.g. `adc-crt hid read 0x12` |
| `adc-crt hid write CODE BYTE...` | Write one raw feature report (no clamping), e.g. `adc-crt hid write 0x12 80 0` |
| `adc-crt ddc buses` | List DDC/CI bus candidates; `*` marks the one used |
| `adc-crt ddc edid [--bus N]` | Read the EDID over I2C (hex on stdout, identity on stderr) |
| `adc-crt power [read\|on\|off\|toggle]` | Read power over HID (default), or set it over DDC/CI. `toggle`: ON → off, anything else → on |
| `adc-crt get [NAME\|CODE\|all]` | Read one control, or all of them (default) |
| `adc-crt set NAME\|CODE VALUE` | Write a control (clamped to its range). Not kept across power cycles until `adc-crt save` |
| `adc-crt defaults` | Write all 11 factory defaults (not saved until `adc-crt save`) |
| `adc-crt degauss` | Degauss the CRT |
| `adc-crt save` | Store the current settings in the monitor's NVRAM |
| `adc-crt vsync` | Read the vertical refresh rate |
| `adc-crt gui` | Open the GTK calibration window |
| `adc-crt buttons` | Listen for the front-panel button and toggle the GUI (started at login by autostart) |
| `adc-crt poweron [--wait SECONDS]` | Boot-time power-on, used by `adc-crt-poweron.service` (default wait 30 s) |
| `adc-crt keybinding labwc [--install]` | Print, or add to your labwc `rc.xml`, a Ctrl+Alt+P power-toggle binding |
| `adc-crt --version` | Print the version |

Control names for `get` and `set` ignore case, spaces, dashes and underscores, so `"H Phase"`, `h-phase` and `h_phase` all work. Codes work as `0x20` or decimal `32`. `brightness` is an alias for `contrast`.

| Name | Code | Range | Factory default |
|---|---|---|---|
| `contrast` | `0x12` | 0–96 | 95 |
| `h-phase` | `0x20` | 0–96 | 64 |
| `h-size` | `0x22` | 0–96 | 70 |
| `v-phase` | `0x30` | 0–96 | 68 |
| `v-size` | `0x32` | 0–96 | 67 |
| `h-pincushion` | `0x24` | 0–96 | 63 |
| `h-keystone` | `0x42` | 0–96 | 54 |
| `h-key-balance` | `0x40` | 0–96 | 77 |
| `rotation` | `0x44` | 0–255 | 152 |
| `h-stat-conv` | `0x28` | 0–255 | 127 |
| `v-stat-conv` | `0x38` | 0–255 | 127 |

Examples:

```bash
adc-crt power toggle
adc-crt get all
adc-crt set h-size 72 && adc-crt save
adc-crt --bus 5 power on        # use I2C bus 5 for this one command
```

## Configuration

You only need this when auto-detection picks the wrong device, or with a USB-to-I2C adapter. Sources, highest priority first:

1. The command-line options `--bus N` and `--hidraw PATH`.
2. The environment variables `ADC_CRT_I2C_BUS` and `ADC_CRT_HIDRAW`.
3. `$XDG_CONFIG_HOME/adc-crt-control/config.ini` (normally `~/.config/adc-crt-control/config.ini`).
4. `/etc/adc-crt-control.conf`. This is the file the boot service reads, because it runs as root.

```ini
# ~/.config/adc-crt-control/config.ini  (or /etc/adc-crt-control.conf)
[ddc]
bus = 5

[hid]
hidraw = /dev/hidraw3
```

Without a configured bus, DDC uses only these: on a Pi, bus 1; otherwise, a graphics-card connector bus whose EDID identifies the Studio Display. No other I2C bus is ever probed, because a desktop's SMBus has the RAM's SPD EEPROMs at the same address as an EDID.

## Troubleshooting

Start with `adc-crt doctor`. Each failed line comes with its fix. The common ones:

| `doctor` says | Fix |
|---|---|
| USB HID device 05ac:9213 not found | Plug the adapter's USB port into this computer. Check `lsusb \| grep 05ac:9213`. |
| No node reports the Monitor Control interface | The adapter is visible but not its monitor interface. Replug it, or pass `--hidraw /dev/hidrawN` from `adc-crt hid list`. |
| You cannot read and write `/dev/hidrawN` (also hiddev, or a graphics-card `/dev/i2c-N`) | Access goes to the **active local desktop session**, not SSH. Log out and back in, or replug the USB cable. Check the rule is installed at `/usr/lib/udev/rules.d/70-adc-crt-control.rules`, then run `sudo udevadm control --reload-rules && sudo udevadm trigger`. |
| i2c-dev kernel module not loaded | `sudo modprobe i2c-dev`. The package loads it at boot through `/usr/lib/modules-load.d/adc-crt-control.conf`. |
| Raspberry Pi I2C is not enabled | `sudo raspi-config nonint do_i2c 0`, then reboot. |
| Raspberry Pi: user is not in group i2c, or the session predates it | `sudo usermod -aG i2c $USER`, then log out and back in. |
| No DDC/CI bus found for the Apple Studio Display | **Pi:** check the wiring (pin 3 → DVI 7, pin 5 → DVI 6, pin 9 → DVI 15). `sudo i2cdetect -y 1` must show `37` and `50`. **Desktop:** the video adapter isn't passing DDC. Try another adapter or connector, or use a USB-to-I2C adapter with `--bus N`. |
| DDC bus found, but the EDID could not be read | Same as the line above. Also check that no other program is using that bus. |
| Power state could not be read over HID | HID is reachable but the monitor didn't answer. Check that the monitor is plugged in and the adapter has power. |

More problems:

- **Power on/off does nothing, but HID works.** DDC writes reach no device. Run `adc-crt ddc buses` and see the DDC rows above.
- **The Pi went headless after powering the CRT off.** The Pi can drop HDMI when the CRT turns off. Run `adc-crt power on` over SSH, then restart the graphical session. This is why the GUI asks for a 5-second countdown first.
- **The button does nothing, or works only every other press.** Only one process may listen to the button. Check that no old `crt-buttons` is still running (`pgrep -af crt-buttons`). `adc-crt buttons` itself refuses to start twice.
- **The boot service.** See its log with `journalctl -b -u adc-crt-poweron.service`.

## Uninstall

Raspberry Pi OS:

```bash
sudo apt remove adc-crt-control
```

This disables the boot service and removes the udev rule. The labwc keybinding in your own `~/.config/labwc/rc.xml` is yours to keep or delete. A backup of the file from before `--install` sits next to it.

CachyOS / Arch:

```bash
sudo pacman -R adc-crt-control
```

If you installed without a package (`sudo make -C linux install`), remove it with `sudo make -C linux uninstall`.

## For developers

```bash
make -C linux test       # unit tests, no hardware needed
make -C linux check      # syntax, maintainer scripts, .desktop files, version consistency
make -C linux deb        # -> linux/dist/adc-crt-control_3.0.0_all.deb
linux/bin/adc-crt self-test    # runs straight from the source tree
make -C linux install DESTDIR=/tmp/stage PREFIX=/usr   # staged install
```

The version is defined once, as `VERSION` in `linux/Makefile`. `make check` fails if `packaging/debian/control`, `packaging/arch/PKGBUILD` or `adc_crt/__init__.py` disagree with it.
