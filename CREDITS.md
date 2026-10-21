# Credits

Getting a 25-year-old Apple CRT working with a Raspberry Pi, a Linux desktop and an Apple Silicon Mac depended on people who reverse-engineered this monitor, its protocols and those platforms before us, and then shared what they found. Thank you.

Each entry below says what this project actually took from that work. "Learned from" means ideas or protocol facts. "Adapted from" means code. Licenses are the ones stated in each project's own repository.

## Software

### GUI-for-apple-studio-17-ADC

<https://github.com/mega-calibrator/GUI-for-apple-studio-17-ADC>

- **Who:** mega-calibrator, the repository owner, and agfa. agfa is named as author in the project's `pyproject.toml` and contributed its README hardware notes and the changes for power control, input limits and the refresh-rate label.
- **License:** The repository has no LICENSE file. `pyproject.toml` declares an MIT classifier and points to a LICENSE file that isn't there.
- **What we learned / used:** This Windows GUI is where this project started. Two things came from it:
  - The control tables in `linux/adc_crt/protocol.py` (Linux; `crt_backend.py` in v2.x) and `mac/ADCControl/Sources/ADCMonitorCore/ADCControl.swift` (Mac) list the same 11 geometry controls as its `vcp_codes` table. They are HID usage page 0x82, codes 0x12, 0x20, 0x22, 0x30, 0x32, 0x24, 0x42, 0x40, 0x44, 0x28 and 0x38, in the same order and with nearly the same labels.
  - Our tables also use its degauss (0x01), save-to-NVRAM (0xB0) and vertical-refresh (0xAE, value ÷ 100) usages.

  The Linux GUI (`linux/adc_crt/gui.py`, ported from the v2.x `pi-adc-gui.py`) mirrors its layout: a slider and number box per control, a "V. Rate" readout, Degauss / Read / Write buttons, and power control kept separate over DDC. No code was copied, and our slider limits and factory defaults were not taken from it.

### usbmonctl

<https://github.com/ondrej-zary/usbmonctl>

- **Who:** Ondrej Zary, with README contributions from aschaap.
- **License:** GPL-3.0
- **What we learned / used:** The v2.x Pi version (git tag `pi-v2.1`) ran usbmonctl for all USB HID access: its `crt_backend.py` and `crt-toggle` used it to find the monitor (`usbmonctl -l`) and to read and write feature controls (`-g` / `-s F,0x..`). Its Apple control table documents control 0xD6 as a read-only power state (1 = on, 2 = sleep, 4 = off). That limitation is why this project reads power state over HID and writes it over DDC. usbmonctl implements the USB Monitor Control Class Specification. The current Linux version no longer needs it: it talks to the kernel's hidraw and hiddev interfaces directly.

### monitorcontrol

<https://github.com/newAM/monitorcontrol>

- **Who:** Alex Martens (newAM) and contributors.
- **License:** MIT
- **What we learned / used:** This was our DDC/CI protocol reference. mega-calibrator's GUI also uses it, through a fork, for power control. The power-on and power-off packets in `linux/adc_crt/protocol.py` (and v2.x `crt_backend.py` / `crt-toggle`) and the Mac `DDCPacket` follow its Set VCP Feature layout:
  - host address 0x51
  - a length byte with the 0x80 flag
  - opcode 0x03
  - the VCP code, then a big-endian value
  - an XOR checksum seeded with the display's write address (0x37 << 1 = 0x6E)

  The power control itself is VCP 0xD6 (power mode), where 1 = on and 4 = off.

### m1ddc

<https://github.com/waydabber/m1ddc>

- **Who:** waydabber and contributors. waydabber wrote the original Apple Silicon DDC code (2021), including the lookup of the external `DCPAVServiceProxy`. Ugo Popée restructured it (2023) and added the per-display matching by `IODisplayLocation` that we adapted.
- **License:** MIT, Copyright (c) 2021 waydabber. The full notice is [below](#third-party-license-notices).
- **What we learned / used (code adapted from):** `mac/ADCControl/Sources/ADCMonitorCore/MacDDCPowerTransport.swift` adapts m1ddc's Apple Silicon DDC path to Swift. That covers four parts:
  - the declarations for `IOAVServiceCreate`, `IOAVServiceCreateWithService`, `IOAVServiceReadI2C`, `IOAVServiceWriteI2C` and `CoreDisplay_DisplayCreateInfoDictionary`
  - display enumeration, which reads the UUID, `IODisplayLocation` and `ProductAttributes`
  - finding each display's external `DCPAVServiceProxy` by walking the IORegistry
  - the write routine: the packet `84 03 <code> <hi> <lo> <checksum>` goes to chip address 0x37, data address 0x51, twice, with a 10 ms wait before each write

  "m1ddc-style DDC power writes" in the README refers to this code.

### The Apple Silicon DDC pioneers

m1ddc's README thanks [@tao-j](https://github.com/tao-j) (Tao Jin), [@alin23](https://github.com/alin23) (Alin Panaitiu) and [@ybbond](https://github.com/ybbond). [MonitorControl](https://github.com/MonitorControl/MonitorControl) (MIT) credits @alin23, the developer of [Lunar](https://lunar.fyi), with leading its M1 DDC support. No code came from MonitorControl or Lunar. They are listed because m1ddc and MonitorControl credit these people for the Apple Silicon DDC work that the Mac version depends on.

### ddcutil

<https://github.com/rockowitz/ddcutil> · <https://www.ddcutil.com/>

- **Who:** Sanford Rockowitz
- **License:** GPL-2.0-or-later
- **What we learned / used:** The Linux package needs to let the logged-in user open the graphics card's DDC I2C buses. Its udev rule does this by adding the `uaccess` tag to `i2c-dev` nodes whose parent PCI device is a display controller (class 0x03). That follows ddcutil's [`60-ddcutil-i2c.rules`](https://github.com/rockowitz/ddcutil/blob/3.0.3-dev/data/usr/lib/udev/rules.d/60-ddcutil-i2c.rules). No ddcutil code is used.

### i2c-tools

<https://i2c.wiki.kernel.org/index.php/I2C_Tools>

- **Who:** According to their manual pages, [`i2cdetect`](https://manpages.debian.org/bookworm/i2c-tools/i2cdetect.8.en.html) is by Frodo Looijaard, Mark D. Studebaker and Jean Delvare, and [`i2ctransfer`](https://manpages.debian.org/bookworm/i2c-tools/i2ctransfer.8.en.html) is by Wolfram Sang.
- **License:** See the i2c-tools source distribution. The tools are only run as installed programs (a Debian package dependency), and none of their code is in this repository.
- **What we learned / used:** The v2.x Pi version sent its DDC power packets with `i2ctransfer`; the current Linux version writes to `/dev/i2c-N` itself. The README's wiring check uses `i2cdetect`.

### acdcontrol (prior art)

<https://github.com/johngrimmreaper/acdcontrol> (current) · <https://acdcontrol.sourceforge.net> (original)

- **Who:** These names come from the project's `CREDITS.rst`.
  - Pavel Gurevich, the original developer (SourceForge, 2004–2007).
  - On GitHub, the code passed from [taylor/acdcontrol](https://github.com/taylor/acdcontrol) to [warvariuc/acdcontrol](https://github.com/warvariuc/acdcontrol), maintained by Victor Varvariuc, who also wrote the Python version.
  - It then passed to johngrimmreaper/acdcontrol, maintained by John Grimm (also known as Reaper), the current maintainer.
  - `CREDITS.rst` also lists other helpers and contributors.
- **License:** GPL-2.0-or-later
- **What we learned / used:** acdcontrol is a long-running Linux tool for controlling Apple Cinema and Studio displays over USB HID. We studied it early on, while working out how to talk to this monitor over USB. None of its code is used here.

### AllRez

<https://github.com/joevt/AllRez>

- **Who:** joevt
- **License:** The repository has no license file.
- **What we learned / used:** We used it as a reference while researching macOS display modes for the Mac version. It dumps everything macOS knows about a display, including the `dspc` override timing records that the Mac display-override experiments edit. No AllRez code is included.

## Hardware & documentation

- **Apple Display Connector Adapter Board** by Jason Olshefsky: <https://jasondoesitall.com/adc/>. This is the adapter our README calls the Jason "Does It All" adapter, and the project is built around it. It is open-source hardware with published Gerber files. The board takes the monitor's ADC plug and provides DVI, optional VGA and USB connections, plus a DC power input for the monitor. His page credits earlier ADC adapters by Tony (Look, It's Another Blog) and Alex.
- **Apple, "Apple Studio Display 17 (ADC) - Technical Specifications":** <https://support.apple.com/en-us/112524>. The Mac version takes its seven OEM resolutions and refresh rates (`640x480@153` through `1600x1200@65`) from this page. The Mac display-override profiles start from the override file that macOS itself ships for this monitor (`0610:9213`), which is Apple's data.
- **Apple's service manual for the Studio Display 17" (ADC):** We used it as a hardware reference. It is not included in this repository.
- **VESA DDC/CI and MCCS:** <https://vesa.org/vesa-standards/>. DDC/CI 1.1 and MCCS 2.2a are available free from VESA. The power packets are a DDC/CI Set VCP Feature command for MCCS VCP code 0xD6 (power mode).
- **Linux kernel documentation:** our reference for the HID and I2C interfaces the Linux side uses.
  - [hiddev](https://docs.kernel.org/hid/hiddev.html) describes the 8-byte `hiddev_event` format that the button listener reads.
  - [hidraw](https://docs.kernel.org/hid/hidraw.html) covers HID feature reports.
  - [i2c-dev](https://docs.kernel.org/i2c/dev-interface.html) covers userspace I2C through `/dev/i2c-N`.

This project is not affiliated with or endorsed by Apple.

## If we missed you

If anything here is wrong, or your work helped and isn't listed, please open an issue or a pull request so we can fix it.

## Third-party license notices

### m1ddc

Parts of `mac/ADCControl/Sources/ADCMonitorCore/MacDDCPowerTransport.swift` are adapted from [m1ddc](https://github.com/waydabber/m1ddc) and used under its MIT License:

```
MIT License

Copyright (c) 2021 waydabber

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
