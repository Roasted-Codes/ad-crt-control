"""High-level monitor API shared by the CLI, the GUI, the button listener and boot power-on.

Two transports are combined, exactly like the Mac version and the old Pi version:

* USB HID (hidraw): reads every control, the power state and vertical sync; writes contrast,
  geometry, degauss and save-to-NVRAM. HID can NOT set power (0xD6 is read-only).
* DDC/CI over I2C: sets power, but cannot read it back.

So toggling power means: read the state over HID, then write the opposite over DDC.

hid.py, ddc.py and config.py are imported lazily (only when a Monitor actually needs them),
and every hardware entry point can be replaced with a keyword-only hook for tests.
"""

from __future__ import annotations

import errno as _errno
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from .protocol import (
    CODE_DEGAUSS,
    CODE_POWER,
    CODE_SAVE,
    CODE_VSYNC,
    CONTROLS,
    CONTROLS_BY_CODE,
    DEGAUSS_PAYLOAD,
    FEATURE_REPORT_MAX,
    SAVE_PAYLOAD,
    PowerState,
    control_write_payload,
    feature_write_candidates,
    parse_feature_value,
    power_state_from_raw,
)

__all__ = ["DOCTOR_HINT", "Monitor", "MonitorError", "pick_bus", "pick_hidraw"]

DOCTOR_HINT = "Run `adc-crt doctor` for a full diagnosis."

# errno values meaning "the device node or the device itself went away" (unplug, USB reset).
_GONE_ERRNOS = frozenset({_errno.ENODEV, _errno.ENOENT, _errno.ENXIO, _errno.EBADF, _errno.ESHUTDOWN})
# errno values meaning "the device is there but refused or did not answer this report".
_REJECTED_ERRNOS = frozenset({_errno.EPIPE, _errno.EINVAL, _errno.EIO, _errno.ETIMEDOUT, _errno.EPROTO})
_PERMISSION_ERRNOS = frozenset({_errno.EACCES, _errno.EPERM})

_UDEV_FIX = (
    "Install the udev rule 70-adc-crt-control.rules (shipped with adc-crt-control), run "
    "`sudo udevadm control --reload-rules && sudo udevadm trigger`, then replug the monitor's "
    "USB cable or log out and back in."
)

_NO_HID_MESSAGE = (
    "The Apple Studio Display's USB HID device (05ac:9213) was not found. Check that the ADC "
    "adapter's USB cable is connected to this computer and the adapter is powered (`lsusb` should "
    "list 05ac:9213), or name the device with --hidraw /dev/hidrawN or ADC_CRT_HIDRAW."
)

_NO_BUS_MESSAGE = (
    "No DDC/CI (I2C) bus for the Apple Studio Display was found. On a Raspberry Pi enable I2C "
    "(`sudo raspi-config nonint do_i2c 0`) and check the GPIO wiring to the adapter's DVI port; on "
    "a desktop load i2c-dev (`sudo modprobe i2c-dev`) and use a video cable/adapter that passes "
    "DDC. You can also choose the bus with --bus N, ADC_CRT_I2C_BUS, or `[ddc] bus = N` in "
    "~/.config/adc-crt-control/config.ini."
)

_DDC_FIXES = {
    _errno.EACCES: "Permission denied on the I2C device: on a Raspberry Pi run `sudo usermod -aG i2c $USER` "
                   "and log out and back in; on a desktop install the adc-crt-control udev rule and log out "
                   "and back in.",
    _errno.ENOENT: "The I2C device node is missing: load i2c-dev (`sudo modprobe i2c-dev`); on a Raspberry Pi "
                   "enable I2C with `sudo raspi-config nonint do_i2c 0`.",
    _errno.ENXIO: "Nothing acknowledged at DDC/CI address 0x37: the cable or adapter does not pass DDC, the "
                  "wiring is wrong, or this is the wrong bus (choose one with --bus N).",
    _errno.EIO: "Nothing acknowledged at DDC/CI address 0x37: the cable or adapter does not pass DDC, the "
                "wiring is wrong, or this is the wrong bus (choose one with --bus N).",
    _errno.EBUSY: "A kernel driver already owns that I2C address on this bus; choose another bus with --bus N.",
}
_DDC_FIXES[_errno.EPERM] = _DDC_FIXES[_errno.EACCES]
_DDC_FIXES[_errno.ENODEV] = _DDC_FIXES[_errno.ENOENT]
_DDC_FIXES[_errno.EREMOTEIO] = _DDC_FIXES[_errno.ENXIO]

_UNSET: Any = object()


class MonitorError(Exception):
    """A user-facing, actionable error: what failed, how to fix it, and a pointer to `adc-crt doctor`.

    `errno` carries the underlying OS error number when there is one (None otherwise).
    """

    def __init__(self, message: str, errno: Optional[int] = None) -> None:
        super().__init__(message)
        self.errno = errno


def _os_detail(exc: BaseException) -> str:
    code = getattr(exc, "errno", None)
    strerror = getattr(exc, "strerror", None)
    if code and strerror:
        return f"{_errno.errorcode.get(code, str(code))}: {strerror}"
    return str(exc) or exc.__class__.__name__


def _with_hint(message: str) -> str:
    message = message.rstrip()
    if "adc-crt doctor" in message:
        return message
    if not message.endswith("."):
        message += "."
    return f"{message} {DOCTOR_HINT}"


def _has_own_message(exc: BaseException) -> bool:
    """True for transport errors (hid.HidError, ddc.DdcError) whose str() is already actionable."""
    return isinstance(exc, OSError) and type(exc).__str__ is not OSError.__str__


def _hid_failure(action: str, path: str, exc: BaseException) -> MonitorError:
    code = getattr(exc, "errno", None)
    if _has_own_message(exc):
        return MonitorError(_with_hint(f"{action}: {exc}"), code)
    detail = _os_detail(exc)
    if code in _PERMISSION_ERRNOS:
        message = f"{action}: permission denied on {path} ({detail}). {_UDEV_FIX}"
    elif code in _GONE_ERRNOS:
        message = (f"{action}: {path} is not available ({detail}); the monitor's USB cable may be "
                   "unplugged or the device was reset. Reconnect it and try again.")
    elif code in _REJECTED_ERRNOS:
        message = (f"{action} on {path} failed ({detail}): the monitor did not accept the request. Check "
                   "that the monitor is on and that this is its monitor-control interface (`adc-crt hid list`).")
    else:
        message = f"{action} on {path} failed ({detail})."
    return MonitorError(_with_hint(message), code)


def _ddc_failure(on: bool, bus: int, exc: BaseException) -> MonitorError:
    code = getattr(exc, "errno", None)
    message = f"Could not send power {'on' if on else 'off'} over DDC/CI on /dev/i2c-{bus}"
    if _has_own_message(exc):
        message += f": {exc}"
    else:
        message += f" ({_os_detail(exc)})."
        if code in _DDC_FIXES:
            message += " " + _DDC_FIXES[code]
    return MonitorError(_with_hint(message), code)


def pick_hidraw(infos: Iterable[Any]) -> Optional[Any]:
    """The hidraw node to use: the first monitor-control interface, else the first node, else None."""
    infos = list(infos)
    for info in infos:
        if getattr(info, "is_monitor_interface", False):
            return info
    return infos[0] if infos else None


def pick_bus(buses: Iterable[Any]) -> Optional[Any]:
    """The DDC bus to use: the first discovery result (discovery already orders by preference)."""
    for bus in buses:
        return bus
    return None


# --- default hardware hooks (lazy imports so this module works without hid/ddc/config) ---------

def _default_hid_factory(path: str) -> Any:
    from . import hid
    return hid.HidrawDevice(path)


def _default_find_hidraw() -> List[Any]:
    from . import hid
    return hid.find_hidraw()


def _default_discover_buses() -> List[Any]:
    from . import ddc
    return ddc.discover_buses()


def _default_ddc_set_power(bus: int, on: bool) -> None:
    from . import ddc
    ddc.set_power(bus, on)


def _default_load_config() -> Any:
    from . import config
    return config.load_config()


class Monitor:
    """The Apple Studio Display 17" CRT behind a Jason "Does It All" ADC adapter.

    `hidraw_path` / `i2c_bus` of None mean "resolve on first use": explicit argument, then the
    config (env / user file / system file), then discovery. One HidrawDevice is opened lazily and
    reused; every hardware access is serialized by a threading.Lock (the GUI uses worker threads).
    """

    def __init__(
        self,
        hidraw_path: Optional[str] = None,
        i2c_bus: Optional[int] = None,
        *,
        _hid_factory: Optional[Callable[[str], Any]] = None,
        _find_hidraw: Optional[Callable[[], Iterable[Any]]] = None,
        _discover_buses: Optional[Callable[[], Iterable[Any]]] = None,
        _ddc_set_power: Optional[Callable[[int, bool], None]] = None,
        _load_config: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._hid_factory = _hid_factory or _default_hid_factory
        self._find_hidraw = _find_hidraw or _default_find_hidraw
        self._discover_buses = _discover_buses or _default_discover_buses
        self._ddc_set_power = _ddc_set_power or _default_ddc_set_power
        self._load_config = _load_config or _default_load_config
        self._config: Any = _UNSET
        self._device: Any = None

        self._hidraw_path: Optional[str] = hidraw_path or None
        self._hidraw_auto = False
        self.hidraw_source: Optional[str] = "argument" if self._hidraw_path else None

        self._i2c_bus: Optional[int] = i2c_bus
        self._bus_auto = False
        self.i2c_bus_source: Optional[str] = "argument" if i2c_bus is not None else None
        self.ddc_bus_info: Any = None  # the DdcBus chosen by discovery, if discovery was used

    # --- context manager ----------------------------------------------------------------------
    def __enter__(self) -> "Monitor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- lazy resolution ----------------------------------------------------------------------
    @property
    def hidraw_path(self) -> str:
        with self._lock:
            return self._resolve_hidraw_locked()

    @property
    def i2c_bus(self) -> int:
        with self._lock:
            return self._resolve_bus_locked()

    def _config_locked(self) -> Any:
        if self._config is _UNSET:
            try:
                self._config = self._load_config()
            except Exception as exc:  # bad INI, bad env value, unreadable file ...
                raise MonitorError(_with_hint(
                    f"Could not read the adc-crt-control configuration ({exc}). Fix or remove "
                    "~/.config/adc-crt-control/config.ini or /etc/adc-crt-control.conf, and check the "
                    "ADC_CRT_I2C_BUS / ADC_CRT_HIDRAW environment variables")) from exc
        return self._config

    def _resolve_hidraw_locked(self) -> str:
        if self._hidraw_path:
            return self._hidraw_path
        configured = getattr(self._config_locked(), "hidraw", None)
        if configured:
            self._hidraw_path, self._hidraw_auto, self.hidraw_source = configured, False, "config"
            return configured
        try:
            infos = list(self._find_hidraw())
        except OSError as exc:
            raise MonitorError(_with_hint(
                f"Could not scan /sys for the monitor's USB HID device ({_os_detail(exc)})"), exc.errno) from exc
        info = pick_hidraw(infos)
        if info is None:
            raise MonitorError(_with_hint(_NO_HID_MESSAGE))
        self._hidraw_path, self._hidraw_auto = info.path, True
        self.hidraw_source = "auto" if getattr(info, "is_monitor_interface", False) else "auto (first 05ac:9213 node)"
        return info.path

    def _resolve_bus_locked(self) -> int:
        if self._i2c_bus is not None:
            return self._i2c_bus
        configured = getattr(self._config_locked(), "i2c_bus", None)
        if configured is not None:
            self._i2c_bus, self._bus_auto, self.i2c_bus_source = int(configured), False, "config"
            return self._i2c_bus
        try:
            buses = list(self._discover_buses())
        except OSError as exc:
            raise MonitorError(_with_hint(
                f"Could not scan for DDC/CI buses ({_os_detail(exc)})"), exc.errno) from exc
        bus = pick_bus(buses)
        if bus is None:
            raise MonitorError(_with_hint(_NO_BUS_MESSAGE))
        self._i2c_bus, self._bus_auto, self.ddc_bus_info = int(bus.number), True, bus
        self.i2c_bus_source = f"auto ({getattr(bus, 'source', 'discovery')})"
        return self._i2c_bus

    # --- HID plumbing (all *_locked methods require self._lock to be held) ---------------------
    def _device_locked(self) -> Any:
        if self._device is None:
            path = self._resolve_hidraw_locked()
            try:
                self._device = self._hid_factory(path)
            except OSError as exc:
                self._forget_hidraw_locked()
                raise _hid_failure("Cannot open the monitor's USB HID interface", path, exc) from exc
        return self._device

    def _close_device_locked(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                device.close()
            except OSError:
                pass

    def _forget_hidraw_locked(self) -> None:
        if self._hidraw_auto:  # re-discover next time (the node number can change on replug)
            self._hidraw_path, self._hidraw_auto, self.hidraw_source = None, False, None

    def _io_failed_locked(self, exc: BaseException) -> None:
        if getattr(exc, "errno", None) in _GONE_ERRNOS:
            self._close_device_locked()
            self._forget_hidraw_locked()

    def _get_locked(self, code: int) -> bytes:
        device = self._device_locked()
        path = self._hidraw_path or "?"
        try:
            return bytes(device.get_feature(code, FEATURE_REPORT_MAX))
        except OSError as exc:
            self._io_failed_locked(exc)
            raise _hid_failure(f"Reading HID feature report 0x{code:02X}", path, exc) from exc

    def _write_locked(self, code: int, payload: Union[bytes, bytearray, List[int]]) -> bytes:
        candidates = feature_write_candidates(code, payload)
        device = self._device_locked()
        path = self._hidraw_path or "?"
        last: Optional[BaseException] = None
        for data in candidates:
            try:
                device.set_feature(data)
                return data
            except OSError as exc:  # try every candidate, like MacHIDTransport.writeFeature
                last = exc
        assert last is not None
        self._io_failed_locked(last)
        tried = " and ".join(f"{len(c)}-byte" for c in candidates)
        raise _hid_failure(f"Writing HID feature report 0x{code:02X} ({tried} form tried)", path, last) from last

    # --- raw feature reports (used by `adc-crt hid read/write`) --------------------------------
    def read_feature(self, code: int) -> bytes:
        """The raw GET_REPORT(feature) bytes for `code` (report ID usually echoed in byte 0)."""
        with self._lock:
            return self._get_locked(code)

    def write_feature(self, code: int, payload: Union[bytes, bytearray, List[int]]) -> bytes:
        """SET_REPORT(feature) [code] + payload; returns the candidate buffer the device accepted."""
        with self._lock:
            return self._write_locked(code, payload)

    # --- controls ------------------------------------------------------------------------------
    def _read_control_locked(self, code: int) -> int:
        value = parse_feature_value(code, self._get_locked(code))
        if value is None:
            raise MonitorError(_with_hint(
                f"The monitor returned an empty HID report for 0x{code:02X} on {self._hidraw_path}"))
        return value

    def read_control(self, code: int) -> int:
        with self._lock:
            return self._read_control_locked(code)

    def read_all(self) -> Dict[int, int]:
        """Every control in CONTROLS order: {code: value}."""
        with self._lock:
            return {control.code: self._read_control_locked(control.code) for control in CONTROLS}

    @staticmethod
    def _control(code: int) -> Any:
        control = CONTROLS_BY_CODE.get(code)
        if control is None:
            known = ", ".join(f"0x{c.code:02X} ({c.slug})" for c in CONTROLS)
            raise ValueError(f"0x{code:02X} is not a contrast/geometry control; known controls: {known}")
        return control

    def _write_control_locked(self, code: int, value: Union[int, float]) -> int:
        control = self._control(code)
        clamped = control.clamp(value)
        self._write_locked(code, control_write_payload(control, clamped))
        return clamped

    def write_control(self, code: int, value: Union[int, float]) -> int:
        """Clamp `value` into 0..max, write it, and return the value actually written."""
        with self._lock:
            return self._write_control_locked(code, value)

    def write_factory_defaults(self) -> None:
        """Write all 11 factory defaults in CONTROLS order (not saved to NVRAM; see save())."""
        with self._lock:
            for control in CONTROLS:
                self._write_control_locked(control.code, control.factory_default)

    def degauss(self) -> None:
        with self._lock:
            self._write_locked(CODE_DEGAUSS, DEGAUSS_PAYLOAD)

    def save(self) -> None:
        """Store the current settings in the monitor's NVRAM (0xB0 = 1)."""
        with self._lock:
            self._write_locked(CODE_SAVE, SAVE_PAYLOAD)

    def read_vsync_hz(self) -> Optional[float]:
        """Vertical refresh in Hz (HID 0xAE is Hz x 100); None if the monitor returned no data."""
        with self._lock:
            value = parse_feature_value(CODE_VSYNC, self._get_locked(CODE_VSYNC))
        return None if value is None else value / 100.0

    # --- power ---------------------------------------------------------------------------------
    def _read_power_locked(self) -> Optional[PowerState]:
        return power_state_from_raw(parse_feature_value(CODE_POWER, self._get_locked(CODE_POWER)))

    def read_power(self) -> Optional[PowerState]:
        """Power state over HID; None for an empty/unknown value. Raises MonitorError if HID fails."""
        with self._lock:
            return self._read_power_locked()

    def _set_power_locked(self, on: bool) -> None:
        bus = self._resolve_bus_locked()
        try:
            self._ddc_set_power(bus, bool(on))
        except OSError as exc:
            if self._bus_auto and getattr(exc, "errno", None) in (_errno.ENOENT, _errno.ENODEV):
                self._i2c_bus, self._bus_auto, self.ddc_bus_info, self.i2c_bus_source = None, False, None, None
            raise _ddc_failure(bool(on), bus, exc) from exc

    def set_power(self, on: bool) -> None:
        """Send DDC/CI power on/off (HID cannot set power on this monitor)."""
        with self._lock:
            self._set_power_locked(on)

    def toggle_power(self) -> PowerState:
        """ON -> send OFF; anything else (OFF, SLEEP, unknown, HID unreadable) -> send ON.

        Returns the state that was requested. Matches the old crt-toggle: if HID cannot be read
        the monitor is assumed to be off, because powering on is idempotent and safe.
        """
        with self._lock:
            try:
                state = self._read_power_locked()
            except MonitorError:
                state = None
            turn_on = state != PowerState.ON
            self._set_power_locked(turn_on)
        return PowerState.ON if turn_on else PowerState.OFF

    def close(self) -> None:
        with self._lock:
            self._close_device_locked()
