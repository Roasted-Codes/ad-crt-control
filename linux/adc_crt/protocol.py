"""Monitor protocol: constants, control table, DDC/CI packets, HID report parsing, EDID checks.

Pure functions only (no I/O). The values here are a port of the working Mac implementation in
mac/ADCControl/Sources/ADCMonitorCore/ (ADCControl.swift, MacHIDTransport.swift,
MacDDCPowerTransport.swift). linux/tests/test_protocol.py parses ADCControl.swift and fails if
the two drift apart.
"""

from __future__ import annotations

import enum
import operator
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple, Union

# --- USB HID identity ------------------------------------------------------------------------
VENDOR_ID = 0x05AC
PRODUCT_ID = 0x9213
# EDID manufacturer ID 0x0610 is the PNP code "APP"; the EDID product code equals the USB PID.
EDID_VENDOR = 0x0610
EDID_PRODUCT = 0x9213
# Top-level application collection of the monitor-control interface (USB Monitor Control class).
HID_USAGE_PAGE = 0x80
HID_USAGE = 0x01

# --- HID feature report codes (report ID == control code) ------------------------------------
CODE_CONTRAST = 0x12
CODE_DEGAUSS = 0x01
CODE_SAVE = 0xB0      # write 1 = store current settings in the monitor's NVRAM
CODE_POWER = 0xD6     # read-only over HID on this monitor; power is *set* over DDC/CI
CODE_VSYNC = 0xAE     # 16-bit little-endian, Hz x 100

# Physical brightness button, as reported by hiddev events (usage & 0xFFFF).
BUTTON_USAGE = 0x00E4

# Largest feature report we ask for; same buffer size as MacHIDTransport.readFeature.
FEATURE_REPORT_MAX = 132

DEGAUSS_PAYLOAD = b"\x01"
SAVE_PAYLOAD = b"\x01"

# --- DDC/CI ----------------------------------------------------------------------------------
DDC_ADDR = 0x37        # 7-bit I2C address of the DDC/CI command interface
DDC_HOST_ADDR = 0x51   # "source address" byte the host puts first in every DDC/CI message
DDC_DEST_ADDR = 0x6E   # 0x37 << 1 (write); it is part of the checksum but sent by the I2C layer
EDID_ADDR = 0x50
EDID_LENGTH = 128
EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"

_VCP_SET_LENGTH = 0x84  # 0x80 | 4 data bytes that follow (opcode, code, value hi, value lo)
_VCP_SET_OPCODE = 0x03  # "Set VCP Feature"


@dataclass(frozen=True)
class Control:
    code: int
    label: str
    max_value: int
    factory_default: int

    def clamp(self, value: Union[int, float]) -> int:
        """Clamp into 0..max_value (ADCControl.clamped). Floats (GTK adjustments) are rounded."""
        if isinstance(value, float):
            number = int(round(value))
        else:
            number = operator.index(value)  # rejects str and other non-integers
        return min(max(number, 0), self.max_value)

    @property
    def slug(self) -> str:
        """Command-line name: "H Key Balance" -> "h-key-balance"."""
        return "-".join(self.label.lower().split())


# Order matters: it is the slider order in both GUIs and the order factory defaults are written.
CONTROLS: Tuple[Control, ...] = (
    Control(0x12, "Contrast", 96, 95),
    Control(0x20, "H Phase", 96, 64),
    Control(0x22, "H Size", 96, 70),
    Control(0x30, "V Phase", 96, 68),
    Control(0x32, "V Size", 96, 67),
    Control(0x24, "H Pincushion", 96, 63),
    Control(0x42, "H Keystone", 96, 54),
    Control(0x40, "H Key Balance", 96, 77),
    Control(0x44, "Rotation", 255, 152),
    Control(0x28, "H Stat Conv", 255, 127),
    Control(0x38, "V Stat Conv", 255, 127),
)
CONTROLS_BY_CODE: Dict[int, Control] = {c.code: c for c in CONTROLS}

# Extra lookup names. The monitor's front-panel button and the old Pi backend
# (BRIGHTNESS_CODE = 0x12) call this control "brightness"; HID calls it Contrast.
_ALIASES: Dict[str, int] = {"brightness": CODE_CONTRAST}


class PowerState(enum.IntEnum):
    ON = 1
    SLEEP = 2
    OFF = 4
    # Note: str(IntEnum) differs between Python 3.9/3.10 ("PowerState.ON") and 3.11+ ("1").
    # Always print state.name / int(state) explicitly.


def power_state_from_raw(value: Optional[int]) -> Optional[PowerState]:
    """Map the raw HID 0xD6 value to a PowerState; None for missing/unknown values."""
    if value is None:
        return None
    try:
        return PowerState(value)
    except ValueError:
        return None


def _normalize_name(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def parse_int(text: str) -> int:
    """Parse "0x20" (hex) or "32" (decimal), the same convention as usbmonctl's parse_number."""
    return int(text.strip(), 0)


def lookup_control(key: Union[str, int]) -> Control:
    """Find a control by code (32, "0x20", "32") or name ("h-phase", "H Phase", "h_phase").

    Bare digit strings are decimal (usbmonctl convention); hex needs the 0x prefix.
    Raises KeyError with the list of valid names otherwise.
    """
    if isinstance(key, bool):
        raise KeyError(key)
    if isinstance(key, int):
        code = key
    else:
        text = str(key).strip()
        try:
            code = parse_int(text)
        except ValueError:
            wanted = _normalize_name(text)
            for control in CONTROLS:
                if _normalize_name(control.label) == wanted:
                    return control
            if wanted in _ALIASES:
                return CONTROLS_BY_CODE[_ALIASES[wanted]]
            raise KeyError(_unknown_control_message(text)) from None
    try:
        return CONTROLS_BY_CODE[code]
    except KeyError:
        raise KeyError(_unknown_control_message(key)) from None


def _unknown_control_message(key: object) -> str:
    names = ", ".join(c.slug for c in CONTROLS)
    return f"unknown control {key!r}; valid names: {names} (or a code such as 0x12)"


# --- DDC/CI packets --------------------------------------------------------------------------

def ddc_checksum(message: Iterable[int]) -> int:
    """DDC/CI checksum: XOR of the destination address 0x6E and every message byte."""
    checksum = DDC_DEST_ADDR
    for byte in message:
        checksum ^= byte
    return checksum


def ddc_set_vcp_packet(code: int, value: int) -> bytes:
    """Bytes to write to I2C address 0x37 for "Set VCP Feature" (port of DDCPacket.setVCP).

    Layout: [0x51, 0x84, 0x03, code, value_hi, value_lo, checksum]. The leading 0x51 is the
    host source address; the Mac passes it as IOAVServiceWriteI2C's data address, the Linux
    i2c-dev write sends it as the first data byte - the bytes on the wire are identical.
    """
    if not 0 <= code <= 0xFF:
        raise ValueError(f"VCP code out of range: {code}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"VCP value out of range: {value}")
    body = [DDC_HOST_ADDR, _VCP_SET_LENGTH, _VCP_SET_OPCODE, code, (value >> 8) & 0xFF, value & 0xFF]
    return bytes(body + [ddc_checksum(body)])


POWER_ON_PACKET = ddc_set_vcp_packet(CODE_POWER, PowerState.ON)    # 51 84 03 D6 00 01 6F
POWER_OFF_PACKET = ddc_set_vcp_packet(CODE_POWER, PowerState.OFF)  # 51 84 03 D6 00 04 6A


# --- HID feature reports (port of HIDFeatureReport.value / MacHIDTransport.writeFeature) -----

def parse_feature_value(code: int, raw: Union[bytes, bytearray, List[int]]) -> Optional[int]:
    """Decode a GET_REPORT(feature) result exactly like HIDFeatureReport.value on the Mac.

    The report ID may or may not be echoed in byte 0 (Linux hidraw always includes it), so a
    leading byte equal to the code is dropped when more bytes follow. Vertical sync (0xAE) is a
    16-bit little-endian value; everything else is the first payload byte.
    """
    data = bytes(raw)
    if not data:
        return None
    payload = data[1:] if (data[0] == code and len(data) > 1) else data
    if not payload:
        return None
    if code == CODE_VSYNC and len(payload) >= 2:
        return payload[0] | (payload[1] << 8)
    return payload[0]


def feature_write_candidates(code: int, payload: Union[bytes, bytearray, List[int]]) -> List[bytes]:
    """SET_REPORT(feature) buffers to try in order; the first one the device accepts wins.

    First [code] + payload; then, if different, the same with trailing zero bytes removed while
    longer than 2 bytes (the Mac does this because report lengths differ between controls).
    """
    if not 0 <= code <= 0xFF:
        raise ValueError(f"HID report code out of range: {code}")
    full = bytes([code]) + bytes(payload)  # bytes() raises ValueError for values > 255
    trimmed = full
    while len(trimmed) > 2 and trimmed[-1] == 0:
        trimmed = trimmed[:-1]
    return [full] if trimmed == full else [full, trimmed]


def control_write_payload(control: Union[Control, int], value: Union[int, float]) -> bytes:
    """Payload for writing a geometry/contrast control: [clamped value, 0] (MacHIDTransport)."""
    if not isinstance(control, Control):
        control = CONTROLS_BY_CODE[control]
    return bytes([control.clamp(value), 0])


# --- EDID ------------------------------------------------------------------------------------

def is_studio_display_edid(edid: Union[bytes, bytearray]) -> bool:
    """True for the Apple Studio Display 17" CRT's EDID base block.

    Checks the fixed header, manufacturer 0x0610 ("APP", big-endian bytes 8-9) and product code
    0x9213 (little-endian bytes 10-11). The checksum is deliberately not required: a monitor is
    identified by identity bytes, and some adapters corrupt the checksum byte.
    """
    data = bytes(edid)
    if len(data) < EDID_LENGTH or data[:8] != EDID_HEADER:
        return False
    vendor = (data[8] << 8) | data[9]
    product = data[10] | (data[11] << 8)
    return vendor == EDID_VENDOR and product == EDID_PRODUCT


def edid_identity(edid: Union[bytes, bytearray]) -> Tuple[str, int, Optional[str]]:
    """(PNP manufacturer, product code, monitor name or None) for display purposes."""
    data = bytes(edid)
    if len(data) < EDID_LENGTH or data[:8] != EDID_HEADER:
        raise ValueError("not an EDID base block")
    vendor = (data[8] << 8) | data[9]
    pnp = "".join(chr(((vendor >> shift) & 0x1F) + ord("A") - 1) for shift in (10, 5, 0))
    product = data[10] | (data[11] << 8)
    name = None
    for offset in (54, 72, 90, 108):  # 18-byte display descriptors
        block = data[offset:offset + 18]
        if block[0:3] == b"\x00\x00\x00" and block[3] == 0xFC:  # monitor name descriptor
            name = block[5:18].split(b"\x0a", 1)[0].decode("ascii", "replace").rstrip()
            break
    return pnp, product, name
