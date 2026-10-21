"""DDC/CI over Linux i2c-dev: bus discovery, EDID read, power command write.

HID can read the power state but not set it; DDC/CI (I2C address 0x37) can set it. On a
Raspberry Pi the bus is GPIO I2C bus 1, hand-wired to the ADC adapter's DVI port. On a desktop
it is the GPU's own DDC/AUX channel for the connector the CRT is plugged into.

All I/O goes through the module-level ``_ioctl`` / ``_os_open`` / ``_os_read`` / ``_os_write``
/ ``_os_close`` / ``_sleep`` names so tests can replace them.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Union

from .protocol import (
    DDC_ADDR,
    EDID_ADDR,
    EDID_LENGTH,
    POWER_OFF_PACKET,
    POWER_ON_PACKET,
    is_studio_display_edid,
)

# --- injectable I/O (tests patch these) -------------------------------------------------------
_ioctl = fcntl.ioctl
_os_open = os.open
_os_read = os.read
_os_write = os.write
_os_close = os.close
_sleep = time.sleep

I2C_SLAVE = 0x0703  # <linux/i2c-dev.h>: set the 7-bit slave address for read()/write()

DEFAULT_REPEATS = 2      # the Mac sends every power packet twice; Set VCP is idempotent
DEFAULT_GAP_S = 0.05     # DDC/CI minimum delay between host messages

RASPBERRY_PI_BUS = 1
SOURCE_CONFIG = "config"
SOURCE_RASPBERRY_PI = "raspberry-pi-gpio"

_EREMOTEIO = getattr(errno, "EREMOTEIO", 121)
# "Nobody answered at that address": NACK (ENXIO from most adapters, EREMOTEIO from DRM/AUX
# and amdgpu), generic bus errors, and AUX/adapter timeouts.
_NO_ACK_ERRNOS = frozenset({errno.ENXIO, _EREMOTEIO, errno.EIO, errno.ETIMEDOUT})


class DdcError(OSError):
    """DDC/I2C failure. ``errno`` is the kernel error code; ``str()`` is an actionable message."""

    def __str__(self) -> str:
        return self.strerror if self.strerror else super().__str__()


def _ddc_error(err: Optional[int], path: str, address: int, action: str) -> DdcError:
    err = err or errno.EIO
    reason = os.strerror(err)
    if err in (errno.EACCES, errno.EPERM):
        message = (f"no permission for {path} — install the udev rule (70-adc-crt-control.rules) "
                   "and log out and back in; on a Raspberry Pi add yourself to the i2c group "
                   "(`sudo usermod -aG i2c $USER`) and log in again; run `adc-crt doctor`")
    elif err in (errno.ENOENT, errno.ENODEV):
        message = (f"{path} does not exist — load the i2c-dev module (`sudo modprobe i2c-dev`); "
                   "on a Raspberry Pi enable I2C with `sudo raspi-config nonint do_i2c 0`; "
                   "run `adc-crt doctor`")
    elif err in _NO_ACK_ERRNOS:
        message = (f"no ACK from the monitor at I2C address 0x{address:02X} on {path} ({reason}) — "
                   "the HDMI/DP-to-VGA adapter or cable may not pass DDC through, the monitor may "
                   "be off at the mains or unplugged, or (Raspberry Pi) check the GPIO-to-DVI "
                   "wiring; run `adc-crt doctor`")
    elif err == errno.EBUSY:
        message = (f"I2C address 0x{address:02X} on {path} is claimed by a kernel driver ({reason}) "
                   "— this bus cannot be used for DDC/CI; choose another with --bus or run "
                   "`adc-crt doctor`")
    else:
        message = f"{action} {path} failed: {reason}; run `adc-crt doctor`"
    return DdcError(err, message, path)


@dataclass
class DdcBus:
    number: int
    source: str          # "config", "raspberry-pi-gpio", "drm:card0-DP-1"
    edid_verified: bool  # the Studio Display's EDID was seen on/for this bus

    @property
    def path(self) -> str:
        return f"/dev/i2c-{self.number}"


@dataclass
class DrmConnector:
    name: str             # "card0-DP-1"
    bus: Optional[int]    # N from the connector's ddc -> i2c-N symlink, if any
    edid: bytes           # sysfs EDID ("" when nothing is connected)
    aux_bus: Optional[int] = None  # the connector's own DP-AUX child adapter, when it differs from bus

    @property
    def is_studio_display(self) -> bool:
        return is_studio_display_edid(self.edid)


def bus_path(bus: int, dev_root: str = "/dev") -> str:
    return os.path.join(dev_root, f"i2c-{bus}")


def _check_bus(bus: object) -> int:
    if isinstance(bus, bool) or not isinstance(bus, int) or bus < 0:
        raise ValueError(f"invalid I2C bus number: {bus!r}")
    return bus


def _open_bus(bus: int, address: int, dev_root: str) -> int:
    path = bus_path(bus, dev_root)
    try:
        fd = _os_open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        raise _ddc_error(exc.errno, path, address, "opening") from exc
    try:
        _ioctl(fd, I2C_SLAVE, address)
    except OSError as exc:
        _os_close(fd)
        raise _ddc_error(exc.errno, path, address, f"selecting address 0x{address:02X} on") from exc
    return fd


def _write_exact(fd: int, data: bytes, path: str, address: int) -> None:
    try:
        written = _os_write(fd, data)
    except OSError as exc:
        raise _ddc_error(exc.errno, path, address, "writing to") from exc
    if written != len(data):
        raise DdcError(errno.EIO, f"short write to {path} at 0x{address:02X}: {written} of "
                                  f"{len(data)} bytes; run `adc-crt doctor`", path)


def read_edid(bus: int, dev_root: str = "/dev") -> bytes:
    """Read the 128-byte EDID base block from the EEPROM at 0x50 (offset 0)."""
    bus = _check_bus(bus)
    path = bus_path(bus, dev_root)
    fd = _open_bus(bus, EDID_ADDR, dev_root)
    try:
        _write_exact(fd, b"\x00", path, EDID_ADDR)  # set the EEPROM word offset to 0
        try:
            data = _os_read(fd, EDID_LENGTH)
        except OSError as exc:
            raise _ddc_error(exc.errno, path, EDID_ADDR, "reading EDID from") from exc
    finally:
        _os_close(fd)
    if len(data) != EDID_LENGTH:
        raise DdcError(errno.EIO, f"short EDID read from {path}: {len(data)} of {EDID_LENGTH} bytes", path)
    return bytes(data)


def write_ddc(bus: int, packet: Union[bytes, bytearray], repeats: int = DEFAULT_REPEATS,
              gap_s: float = DEFAULT_GAP_S, dev_root: str = "/dev") -> None:
    """Write a complete DDC/CI message (starting with 0x51) to address 0x37, ``repeats`` times.

    Each write must be taken whole; there is a ``gap_s`` pause between repeats.
    """
    bus = _check_bus(bus)
    data = bytes(packet)
    if not data:
        raise ValueError("empty DDC/CI packet")
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    if gap_s < 0:
        raise ValueError(f"gap_s must be >= 0, got {gap_s}")
    path = bus_path(bus, dev_root)
    fd = _open_bus(bus, DDC_ADDR, dev_root)
    try:
        for attempt in range(repeats):
            if attempt:
                _sleep(gap_s)
            _write_exact(fd, data, path, DDC_ADDR)
    finally:
        _os_close(fd)


def set_power(bus: int, on: bool, dev_root: str = "/dev") -> None:
    """DDC/CI Set VCP 0xD6 to 1 (on) or 4 (off)."""
    write_ddc(bus, POWER_ON_PACKET if on else POWER_OFF_PACKET, dev_root=dev_root)


# --- discovery --------------------------------------------------------------------------------

def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _natural_key(name: str) -> List[Union[int, str]]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def is_raspberry_pi(proc_root: str = "/proc") -> bool:
    model = _read_bytes(os.path.join(proc_root, "device-tree", "model"))
    return model is not None and b"Raspberry Pi" in model


_CONNECTOR_RE = re.compile(r"card\d+-.+")
_I2C_NAME_RE = re.compile(r"i2c-(\d+)")


def _ddc_link_bus(link: str) -> Optional[int]:
    try:
        target = os.readlink(link)
    except OSError:
        if not os.path.exists(link):
            return None
        target = os.path.realpath(link)
    match = _I2C_NAME_RE.fullmatch(os.path.basename(os.path.normpath(target)))
    return int(match.group(1)) if match else None


def _child_i2c_bus(connector_dir: str) -> Optional[int]:
    """A DisplayPort connector's own AUX-channel I2C adapter ("DPDDC-*"), registered as a child
    i2c-N directory of the connector, when it has no ddc symlink. Exactly one, or None."""
    try:
        names = os.listdir(connector_dir)
    except OSError:
        return None
    numbers = [int(m.group(1)) for m in map(_I2C_NAME_RE.fullmatch, names) if m]
    return numbers[0] if len(numbers) == 1 else None


def iter_drm_connectors(sysfs_root: str = "/sys") -> List[DrmConnector]:
    """Every /sys/class/drm/card*-* connector with its DDC bus number and sysfs EDID.

    The bus is the connector's ``ddc`` symlink (i2c-N); without one, a single child i2c-N
    directory (the DP-AUX adapter) is used. Reads sysfs only; no I2C device is opened (the
    sysfs edid file needs no permission).
    """
    drm_dir = os.path.join(sysfs_root, "class", "drm")
    try:
        names = os.listdir(drm_dir)
    except OSError:
        return []
    connectors = []
    for name in sorted((n for n in names if _CONNECTOR_RE.fullmatch(n)), key=_natural_key):
        entry = os.path.join(drm_dir, name)
        aux = _child_i2c_bus(entry)
        if os.path.lexists(os.path.join(entry, "ddc")):
            bus = _ddc_link_bus(os.path.join(entry, "ddc"))
        else:
            bus, aux = aux, None
        connectors.append(DrmConnector(
            name=name,
            bus=bus,
            edid=_read_bytes(os.path.join(entry, "edid")) or b"",
            aux_bus=aux if aux != bus else None,
        ))
    return connectors


def _bus_has_studio_edid(bus: int, dev_root: str) -> bool:
    try:
        return is_studio_display_edid(read_edid(bus, dev_root))
    except OSError:
        return False


def discover_buses(config_bus: Optional[int] = None, sysfs_root: str = "/sys", dev_root: str = "/dev",
                   proc_root: str = "/proc") -> List[DdcBus]:
    """Candidate DDC buses for the Studio Display, best first.

    1. A configured bus is returned alone (EDID-checked, but used even if unverified).
    2. Raspberry Pi GPIO bus 1, if its EDID is the Studio Display's.
    3. DRM connectors whose ddc symlink (or, failing that, sole child DP-AUX adapter) names
       i2c-N and whose sysfs EDID is the Studio Display's.
    4. Nothing verified on a Pi with /dev/i2c-1: bus 1 unverified (the 2.x behaviour: blind write).

    Safety: the only I2C buses ever *opened* here are the configured bus and the Pi's bus 1.
    Every other /dev/i2c-N is left alone, by design. On a PC those include the SMBus adapters
    (piix4, i801, ...), where addresses 0x50-0x57 are the RAM modules' SPD EEPROMs; an "EDID
    read" there starts by *writing* an offset byte to 0x50, and stray writes to SPD EEPROMs can
    corrupt them (the reason i2cdetect warns and ddcutil restricts itself to display buses).
    DRM connectors are matched via their sysfs EDID file instead, which needs no I2C access.
    """
    if config_bus is not None:
        bus = _check_bus(config_bus)
        return [DdcBus(bus, SOURCE_CONFIG, _bus_has_studio_edid(bus, dev_root))]

    candidates: List[DdcBus] = []
    pi_bus_present = (is_raspberry_pi(proc_root)
                      and os.path.exists(bus_path(RASPBERRY_PI_BUS, dev_root)))
    if pi_bus_present and _bus_has_studio_edid(RASPBERRY_PI_BUS, dev_root):
        candidates.append(DdcBus(RASPBERRY_PI_BUS, SOURCE_RASPBERRY_PI, True))

    for connector in iter_drm_connectors(sysfs_root):
        if not connector.is_studio_display:
            continue
        # A DisplayPort connector can have two buses: the ddc symlink (on amdgpu "AMDGPU DM i2c
        # hw bus", plain I2C on the DP pins, which only a passive DP++ dongle answers) and its own
        # AUX child adapter, which is how the kernel itself reads EDID through an active
        # DP-to-VGA converter. A CRT always needs an active converter, so the AUX bus goes first.
        # Both belong to the connector whose EDID is the Studio Display's, so the safety rule holds.
        options = [(connector.aux_bus, f"drm:{connector.name}:aux"), (connector.bus, f"drm:{connector.name}")]
        for number, source in options:
            if number is None or any(candidate.number == number for candidate in candidates):
                continue
            candidates.append(DdcBus(number, source, True))

    if not candidates and pi_bus_present:
        candidates.append(DdcBus(RASPBERRY_PI_BUS, SOURCE_RASPBERRY_PI, False))
    return candidates
