"""USB HID access to the Apple Studio Display 17" CRT: hidraw feature reports + hiddev buttons.

The monitor's USB side (05ac:9213) carries the control interface as HID feature reports whose
report ID is the control code (see protocol.py). Feature reports go through /dev/hidrawN; the
physical brightness button arrives as hiddev events on /dev/usb/hiddevN.

All I/O goes through the module-level ``_ioctl`` / ``_os_open`` / ``_os_read`` / ``_os_close``
names so tests can replace them; sysfs and /dev locations are parameters for the same reason.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import struct
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple, Union

from .protocol import FEATURE_REPORT_MAX, HID_USAGE, HID_USAGE_PAGE, PRODUCT_ID, VENDOR_ID

# --- injectable I/O (tests patch these) -------------------------------------------------------
_ioctl = fcntl.ioctl
_os_open = os.open
_os_read = os.read
_os_close = os.close

BUS_USB = 0x0003  # first field of HID_ID in a HID device's uevent

# --- ioctl numbers ----------------------------------------------------------------------------
# asm-generic _IOC encoding, shared by x86, x86-64, ARM, ARM64 and RISC-V:
#   bits 0-7 nr, 8-15 type, 16-29 size, 30-31 direction.
_IOC_WRITE = 1
_IOC_READ = 2
_IOC_SIZEBITS = 14
MAX_REPORT_LENGTH = (1 << _IOC_SIZEBITS) - 1


def _IOC(direction: int, type_: int, nr: int, size: int) -> int:
    if not 0 <= size <= MAX_REPORT_LENGTH:
        raise ValueError(f"ioctl size out of range: {size}")
    return (direction << 30) | (size << 16) | (type_ << 8) | nr


def HIDIOCSFEATURE(length: int) -> int:
    """<linux/hidraw.h>: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)."""
    return _IOC(_IOC_WRITE | _IOC_READ, ord("H"), 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    """<linux/hidraw.h>: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, len)."""
    return _IOC(_IOC_WRITE | _IOC_READ, ord("H"), 0x07, length)


# --- errors -----------------------------------------------------------------------------------

class HidError(OSError):
    """HID failure. ``errno`` is the kernel error code; ``str()`` is an actionable message."""

    def __str__(self) -> str:
        return self.strerror if self.strerror else super().__str__()


def _hid_error(err: Optional[int], path: str, action: str) -> HidError:
    err = err or errno.EIO
    reason = os.strerror(err)
    if err in (errno.EACCES, errno.EPERM):
        message = (f"no permission for {path} — install the udev rule (70-adc-crt-control.rules) "
                   "and log out and back in; run `adc-crt doctor`")
    elif err in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        message = (f"{path} is not available ({reason}) — is the monitor's USB cable plugged in? "
                   "run `adc-crt doctor`")
    elif err in (errno.EPIPE, errno.EIO, errno.EINVAL, errno.ETIMEDOUT):
        message = (f"{action} on {path} failed: {reason} "
                   "(the monitor rejected or did not answer the request)")
    else:
        message = f"{action} on {path} failed: {reason}"
    return HidError(err, message, path)


# --- small sysfs helpers ----------------------------------------------------------------------

def _numbered_nodes(directory: str, prefix: str) -> List[str]:
    """Entries named <prefix><digits>, in numeric order (hidraw2 before hidraw10)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    pattern = re.compile(re.escape(prefix) + r"\d+")
    return sorted((n for n in names if pattern.fullmatch(n)), key=lambda n: int(n[len(prefix):]))


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _parse_uevent(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def parse_hid_id(value: str) -> Optional[Tuple[int, int, int]]:
    """"0003:000005AC:00009213" -> (bus, vendor, product); hex, case-insensitive. None if malformed."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        bus, vendor, product = (int(part, 16) for part in parts)
    except ValueError:
        return None
    return bus, vendor, product


# --- HID report descriptor (short-item parser) ------------------------------------------------

_ITEM_MAIN = 0
_ITEM_GLOBAL = 1
_ITEM_LOCAL = 2
_MAIN_COLLECTION = 0xA
_MAIN_END_COLLECTION = 0xC
_GLOBAL_USAGE_PAGE = 0x0
_GLOBAL_PUSH = 0xA
_GLOBAL_POP = 0xB
_LOCAL_USAGE = 0x0
_COLLECTION_APPLICATION = 0x01
_LONG_ITEM_PREFIX = 0xFE


def iter_descriptor_items(descriptor: Union[bytes, bytearray]) -> Iterator[Tuple[int, int, int, int]]:
    """Yield (item_type, tag, data_size, data) for each short item; long items are skipped.

    Prefix byte: bits 0-1 size code (0, 1, 2 bytes; 3 means 4 bytes), bits 2-3 type
    (0 main, 1 global, 2 local), bits 4-7 tag. Data is little-endian. A truncated item ends
    the walk quietly (a broken descriptor is simply "not the monitor interface").
    """
    data = bytes(descriptor)
    index = 0
    while index < len(data):
        prefix = data[index]
        if prefix == _LONG_ITEM_PREFIX:  # 0xFE, bDataSize, bLongItemTag, data...
            if index + 2 >= len(data):
                return
            index += 3 + data[index + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        end = index + 1 + size
        if end > len(data):
            return
        yield (prefix >> 2) & 0x03, (prefix >> 4) & 0x0F, size, int.from_bytes(data[index + 1:end], "little")
        index = end


def application_collections(descriptor: Union[bytes, bytearray]) -> List[Tuple[int, int]]:
    """(usage_page, usage) of every top-level (depth 0) Application collection.

    Like Linux hid-core, a collection takes the first Usage declared before it, and a 1/2-byte
    Usage is combined with the Usage Page in effect at the main item; a 4-byte (extended) Usage
    carries its own page in the high 16 bits. Every main item clears the local Usage list.
    """
    usage_page = 0
    page_stack: List[int] = []
    usages: List[Tuple[Optional[int], int]] = []
    depth = 0
    found: List[Tuple[int, int]] = []
    for item_type, tag, size, value in iter_descriptor_items(descriptor):
        if item_type == _ITEM_MAIN:
            if tag == _MAIN_COLLECTION:
                if depth == 0 and value == _COLLECTION_APPLICATION and usages:
                    page, usage = usages[0]
                    found.append((usage_page if page is None else page, usage))
                depth += 1
            elif tag == _MAIN_END_COLLECTION:
                depth = max(depth - 1, 0)
            usages = []
        elif item_type == _ITEM_GLOBAL:
            if tag == _GLOBAL_USAGE_PAGE:
                usage_page = value
            elif tag == _GLOBAL_PUSH:
                page_stack.append(usage_page)
            elif tag == _GLOBAL_POP and page_stack:
                usage_page = page_stack.pop()
        elif item_type == _ITEM_LOCAL and tag == _LOCAL_USAGE:
            if size == 4:
                usages.append((value >> 16, value & 0xFFFF))
            else:
                usages.append((None, value))
    return found


def is_monitor_descriptor(descriptor: Union[bytes, bytearray]) -> bool:
    """True if a top-level Application collection is Usage Page 0x80 (Monitor) / Usage 0x01."""
    return (HID_USAGE_PAGE, HID_USAGE) in application_collections(descriptor)


# --- hidraw discovery -------------------------------------------------------------------------

@dataclass
class HidrawInfo:
    path: str                   # /dev/hidrawN
    sysfs: str                  # /sys/class/hidraw/hidrawN
    name: str                   # HID_NAME from uevent
    is_monitor_interface: bool  # top-level collection is Monitor page 0x80 / usage 0x01


def find_hidraw(sysfs_root: str = "/sys", dev_root: str = "/dev") -> List[HidrawInfo]:
    """All hidraw nodes of USB device 05ac:9213, monitor-control interface(s) first.

    A node whose report descriptor cannot be read is still listed, with
    is_monitor_interface False, so callers can report it instead of silently missing it.
    """
    class_dir = os.path.join(sysfs_root, "class", "hidraw")
    found: List[HidrawInfo] = []
    for node in _numbered_nodes(class_dir, "hidraw"):
        sysfs = os.path.join(class_dir, node)
        uevent = _read_bytes(os.path.join(sysfs, "device", "uevent"))
        if uevent is None:
            continue
        props = _parse_uevent(uevent.decode("utf-8", "replace"))
        if parse_hid_id(props.get("HID_ID", "")) != (BUS_USB, VENDOR_ID, PRODUCT_ID):
            continue
        descriptor = _read_bytes(os.path.join(sysfs, "device", "report_descriptor"))
        found.append(HidrawInfo(
            path=os.path.join(dev_root, node),
            sysfs=sysfs,
            name=props.get("HID_NAME", ""),
            is_monitor_interface=descriptor is not None and is_monitor_descriptor(descriptor),
        ))
    found.sort(key=lambda info: not info.is_monitor_interface)  # stable: keeps numeric order
    return found


# --- hidraw feature reports -------------------------------------------------------------------

class HidrawDevice:
    """An open /dev/hidrawN. Use as a context manager or call close()."""

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None
        try:
            self._fd = _os_open(path, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            raise _hid_error(exc.errno, path, "opening") from exc

    @property
    def closed(self) -> bool:
        return self._fd is None

    def fileno(self) -> int:
        return self._require_fd()

    def _require_fd(self) -> int:
        if self._fd is None:
            raise HidError(errno.EBADF, f"{self.path} is closed", self.path)
        return self._fd

    def get_feature(self, code: int, length: int = FEATURE_REPORT_MAX) -> bytes:
        """GET_REPORT(feature, report ID = code). The kernel puts the report ID in byte 0."""
        if not 0 <= code <= 0xFF:
            raise ValueError(f"HID report ID out of range: {code}")
        if not 1 <= length <= MAX_REPORT_LENGTH:
            raise ValueError(f"feature report length out of range: {length}")
        fd = self._require_fd()
        buf = bytearray(length)
        buf[0] = code
        try:
            count = _ioctl(fd, HIDIOCGFEATURE(length), buf, True)
        except OSError as exc:
            raise _hid_error(exc.errno, self.path, f"reading feature report 0x{code:02X}") from exc
        return bytes(buf[:count])

    def set_feature(self, data: Union[bytes, bytearray]) -> int:
        """SET_REPORT(feature); data[0] is the report ID. Returns the byte count the kernel took."""
        # A *mutable* buffer with mutate_flag=True makes fcntl.ioctl return the ioctl's int
        # result; an immutable bytes argument would make it return the buffer instead.
        buf = bytearray(data)
        if not buf:
            raise ValueError("a feature report needs at least the report ID byte")
        if len(buf) > MAX_REPORT_LENGTH:
            raise ValueError(f"feature report too long: {len(buf)} bytes")
        fd = self._require_fd()
        try:
            return _ioctl(fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as exc:
            raise _hid_error(exc.errno, self.path, f"writing feature report 0x{buf[0]:02X}") from exc

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            _os_close(fd)

    def __enter__(self) -> "HidrawDevice":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --- hiddev (button events) -------------------------------------------------------------------

def _owning_usb_device(start: str, sysfs_root: str) -> Optional[str]:
    """Nearest ancestor of ``start`` (inclusive) that has idVendor/idProduct, inside sysfs_root."""
    root = os.path.realpath(sysfs_root)
    prefix = root if root.endswith(os.sep) else root + os.sep
    current = os.path.realpath(start)
    while current.startswith(prefix):
        if (os.path.isfile(os.path.join(current, "idVendor"))
                and os.path.isfile(os.path.join(current, "idProduct"))):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _read_hex(path: str) -> Optional[int]:
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        return int(data.decode("ascii", "replace").strip(), 16)
    except ValueError:
        return None


def find_hiddev(sysfs_root: str = "/sys", dev_root: str = "/dev") -> Optional[str]:
    """/dev/usb/hiddevN of the 05ac:9213 monitor, or None.

    /sys/class/usbmisc/hiddevN/device points at a USB *interface*; the first ancestor with
    idVendor/idProduct is the USB device that owns it. Only that device is compared — walking
    further up would reach hubs (including the monitor's own built-in hub).
    """
    class_dir = os.path.join(sysfs_root, "class", "usbmisc")
    for node in _numbered_nodes(class_dir, "hiddev"):
        device = _owning_usb_device(os.path.join(class_dir, node, "device"), sysfs_root)
        if device is None:
            continue
        if (_read_hex(os.path.join(device, "idVendor")) == VENDOR_ID
                and _read_hex(os.path.join(device, "idProduct")) == PRODUCT_ID):
            usb_path = os.path.join(dev_root, "usb", node)
            legacy_path = os.path.join(dev_root, node)
            if not os.path.exists(usb_path) and os.path.exists(legacy_path):
                return legacy_path
            return usb_path
    return None


HIDDEV_EVENT = struct.Struct("=Ii")  # struct hiddev_event { unsigned hid; signed int value; }


def parse_hiddev_events(data: bytes) -> List[Tuple[int, int]]:
    """Decode whole hiddev events: [(usage & 0xFFFF, value), ...]; a trailing partial is ignored."""
    usable = len(data) - len(data) % HIDDEV_EVENT.size
    return [(hid & 0xFFFF, value) for hid, value in HIDDEV_EVENT.iter_unpack(data[:usable])]


def iter_hiddev_events(path: str) -> Iterator[Tuple[int, int]]:
    """Block on ``path`` and yield (usage, value) per event until EOF; closes the fd on exit.

    The device is opened on the first next(). Raises HidError on open/read failure (e.g.
    ENODEV when the monitor is unplugged).
    """
    try:
        fd = _os_open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise _hid_error(exc.errno, path, "opening") from exc
    try:
        pending = b""
        while True:
            try:
                chunk = _os_read(fd, HIDDEV_EVENT.size * 64)
            except OSError as exc:
                raise _hid_error(exc.errno, path, "reading") from exc
            if not chunk:
                return
            pending += chunk
            usable = len(pending) - len(pending) % HIDDEV_EVENT.size
            for event in parse_hiddev_events(pending[:usable]):
                yield event
            pending = pending[usable:]
    finally:
        _os_close(fd)
