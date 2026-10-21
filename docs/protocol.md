# Monitor protocol

This page is the single reference for the protocol that the Linux (Python) and Mac (Swift) implementations share. The code is authoritative. If you change a value, change it in [`linux/adc_crt/protocol.py`](../linux/adc_crt/protocol.py), [`mac/ADCControl/Sources/ADCMonitorCore/ADCControl.swift`](../mac/ADCControl/Sources/ADCMonitorCore/ADCControl.swift) and this page in the same change. See [Keeping the two in sync](#keeping-the-two-in-sync).

## Identity

| What | Value |
|---|---|
| USB HID device | vendor `0x05AC`, product `0x9213` |
| HID interface | top-level Application collection with Usage Page `0x80` (Monitor), Usage `0x01` |
| EDID manufacturer | `0x0610`, the PNP code "APP" (big-endian, bytes 8–9) |
| EDID product | `0x9213` (little-endian, bytes 10–11; same number as the USB product ID) |
| EDID EEPROM | I2C address `0x50`, 128-byte base block |
| DDC/CI | I2C address `0x37` (7-bit; `0x6E` as an 8-bit write address) |

## Two paths, one rule

| | USB HID | DDC/CI |
|---|---|---|
| Read power state (`0xD6`) | yes: 1 = on, 2 = sleep, 4 = off | no (can't read it back) |
| Set power state | **no** (read-only) | yes: Set VCP `0xD6` |
| Geometry, contrast, degauss, save, refresh rate | yes | not used |

**Read state over HID; write power over DDC.** Toggling means: read `0xD6` over HID. If the value is ON, send OFF over DDC. Anything else, including an unreadable value, sends ON.

## USB HID

### Picking the interface

The device can expose more than one HID interface. Use the one whose report descriptor has a top-level Application collection on Usage Page `0x80`, Usage `0x01`.

- **Linux** scans `/sys/class/hidraw/hidraw*` for `HID_ID=0003:000005AC:00009213` and parses each `report_descriptor`.
- **Mac** matches vendor, product and primary usage page and usage through IOHIDManager.

### Feature reports

All controls are HID **feature reports**, and the **report ID is the control code**.

**Read.** Send GET_REPORT(feature, report ID = code) with a buffer of up to 132 bytes. Then decode the result as follows:

1. Empty result → no value.
2. If `raw[0] == code` and more bytes follow, drop that first byte. Linux hidraw always echoes the report ID; the Mac may or may not. Otherwise the payload is the whole buffer.
3. If the code is `0xAE` (vertical sync) and the payload has at least 2 bytes, the value is `payload[0] | payload[1] << 8`. That is 16-bit little-endian, in Hz × 100.
4. Otherwise the value is `payload[0]`.

Examples: `12 5F 00` → 95. `5F 00` → 95. `AE 70 17` → `0x1770` = 6000 → 60.00 Hz.

**Write.** Build these candidate buffers and send SET_REPORT(feature) with each in turn. The first one the device accepts wins.

1. `[code] + payload`
2. If it differs from candidate 1: the same buffer with trailing `0x00` bytes removed, while it is longer than 2 bytes.

The Mac does this because report lengths differ between controls, and Linux copies it. The payloads are:

| Write | Payload | Candidates (example) |
|---|---|---|
| Control | `[clamp(value), 0]`, clamped to `0..max` | Contrast 95 → `12 5F 00`, then `12 5F` |
| Degauss `0x01` | `[1]` | `01 01` |
| Save to NVRAM `0xB0` | `[1]` | `B0 01` |

### Control table

There are 11 controls. **Order matters:** it is the slider order in both GUIs and the order in which factory defaults are written.

| Code | Label | Max | Factory default |
|---|---|---|---|
| `0x12` | Contrast | 96 | 95 |
| `0x20` | H Phase | 96 | 64 |
| `0x22` | H Size | 96 | 70 |
| `0x30` | V Phase | 96 | 68 |
| `0x32` | V Size | 96 | 67 |
| `0x24` | H Pincushion | 96 | 63 |
| `0x42` | H Keystone | 96 | 54 |
| `0x40` | H Key Balance | 96 | 77 |
| `0x44` | Rotation | 255 | 152 |
| `0x28` | H Stat Conv | 255 | 127 |
| `0x38` | V Stat Conv | 255 | 127 |

The old Pi backend called `0x12` "brightness" (`BRIGHTNESS_CODE`), and `adc-crt` accepts `brightness` as an alias for Contrast.

### Special codes

| Code | Direction | Meaning |
|---|---|---|
| `0x01` | write `[1]` | Degauss |
| `0xB0` | write `[1]` | Save current settings to the monitor's NVRAM |
| `0xAE` | read | Vertical refresh: 16-bit little-endian, Hz × 100 |
| `0xD6` | read only | Power state: 1 = on, 2 = sleep, 4 = off. HID **cannot** set it. |
| `0xE4` | input event | Physical brightness button |

**The button.** On Linux the button arrives as a hiddev event (`struct hiddev_event { unsigned hid; signed value; }`, 8 bytes). It matches when `hid & 0xFFFF == 0x00E4`. Ignore `value == 0`, which is the release. Only one process should hold the hiddev node, because two listeners split the events between them. The Mac defines the constant (`ADCUsage.button`) but doesn't act on it.

## DDC/CI: Set VCP Feature (power)

Power is VCP code `0xD6`. Value 1 means on, 4 means off. This message is written to I2C address `0x37`:

| Byte | On | Off | Meaning |
|---|---|---|---|
| 1 | `0x51` | `0x51` | Source address (host) |
| 2 | `0x84` | `0x84` | Length: `0x80` flag + 4 data bytes |
| 3 | `0x03` | `0x03` | Opcode: Set VCP Feature |
| 4 | `0xD6` | `0xD6` | VCP code: power mode |
| 5 | `0x00` | `0x00` | Value, high byte |
| 6 | `0x01` | `0x04` | Value, low byte |
| 7 | `0x6F` | `0x6A` | Checksum |

The checksum is the XOR of the destination write address `0x6E` (`0x37 << 1`) and every message byte. The `0x6E` itself isn't sent as data; the I2C layer sends the address. Here is the power-on example worked through:

```
0x6E ^ 0x51 = 0x3F
0x3F ^ 0x84 = 0xBB
0xBB ^ 0x03 = 0xB8
0xB8 ^ 0xD6 = 0x6E
0x6E ^ 0x00 = 0x6E
0x6E ^ 0x01 = 0x6F   → power on  = 51 84 03 D6 00 01 6F
          (^ 0x04 = 0x6A → power off = 51 84 03 D6 00 04 6A)
```

The two platforms frame the same bytes differently, and the bytes on the wire are identical:

- **Linux** writes all 7 bytes to `/dev/i2c-N` after setting `I2C_SLAVE` to `0x37`.
- **Mac** calls `IOAVServiceWriteI2C(service, 0x37, 0x51, [84 03 D6 00 01 6F], 6)`. The `0x51` travels as the "data address" argument.

Both send the packet **twice**, because the command is idempotent. The Mac waits 10 ms before each write. Linux sends the two writes 50 ms apart, which is the DDC/CI minimum gap between messages.

Nothing is read back over DDC. Power state comes from HID `0xD6`.

## EDID identity check

A 128-byte block is the Studio Display when all three of these hold:

- it starts with `00 FF FF FF FF FF FF 00`
- bytes 8–9 are `06 10` (manufacturer `0x0610`)
- `bytes[10] | bytes[11] << 8 == 0x9213`

The checksum is deliberately not required. On Linux this check decides which I2C bus is the monitor's. The Mac matches the display's vendor and product (or a "StudioDisplay" product name) from CoreDisplay instead.

## Where each piece lives

| Piece | Linux (`linux/adc_crt/`) | Mac (`mac/ADCControl/Sources/ADCMonitorCore/`) |
|---|---|---|
| Constants, control table | `protocol.py` (`CONTROLS`, `CODE_*`, `PowerState`) | `ADCControl.swift` (`ADCControls.all`, `ADCUsage`) |
| Feature-report parsing / write candidates | `protocol.py` (`parse_feature_value`, `feature_write_candidates`, `control_write_payload`) | `MacHIDTransport.swift` (`HIDFeatureReport.value`, `writeFeature`) |
| HID transport | `hid.py` (hidraw `HIDIOCGFEATURE` / `HIDIOCSFEATURE`; hiddev button events) | `MacHIDTransport.swift` (IOHIDManager, `IOHIDDeviceGetReport` / `SetReport`) |
| DDC packet | `protocol.py` (`ddc_set_vcp_packet`, `POWER_ON_PACKET`, `POWER_OFF_PACKET`) | `MacDDCPowerTransport.swift` (`DDCPacket.setVCP`, `DDCPacket.power`) |
| DDC transport | `ddc.py` (`/dev/i2c-N`: Pi GPIO bus 1, DRM connector bus, or a configured bus) | `MacDDCPowerTransport.swift` (IOAVService, private symbols) |
| EDID identity | `protocol.py` (`is_studio_display_edid`), `ddc.py` (`read_edid`) | `MacDDCPowerTransport.swift` (`DDCDisplay.isADCStudioDisplay`) |
| High-level operations | `monitor.py` (`Monitor`) | `adcctl` (`Sources/adcctl/main.swift`) and `ADCControlApp` |

## Keeping the two in sync

The table, defaults and packets are duplicated by design. Two checks catch drift:

- **`linux/tests/test_protocol.py`** parses `ADCControl.swift` and fails if its control table differs from `protocol.py`. Run it with `make -C linux test`.
- **`swift run adcctl self-test`** (Mac) spot-checks the same values against literals in `Sources/adcctl/main.swift`. It checks:
  - the control count (11)
  - the factory defaults of `0x12` (95) and `0x20` (64)
  - the maximum of `0x44` (255)
  - both full power packets
  - HID parsing, including `0xAE`
  - the OEM resolution table

## Provenance

- **Control codes, labels and the special codes** (`0x01`, `0xB0`, `0xAE`) follow the `vcp_codes` table in mega-calibrator's [GUI-for-apple-studio-17-ADC](https://github.com/mega-calibrator/GUI-for-apple-studio-17-ADC). In the USB Monitor Control class, the geometry codes are usages on HID usage page `0x82`.
- **Factory defaults:** the original source is unknown. mega-calibrator has no defaults table, and it uses maxima of 97, 127 and 255, where this project uses 96 and 255.
- **`0xD6` as a read-only HID power state** is documented in [usbmonctl](https://github.com/ondrej-zary/usbmonctl)'s Apple control table.
- **The DDC/CI packet layout** follows VESA DDC/CI Set VCP Feature, as implemented by [monitorcontrol](https://github.com/newAM/monitorcontrol). The Mac write path is adapted from [m1ddc](https://github.com/waydabber/m1ddc).

See [CREDITS.md](../CREDITS.md) for details.
