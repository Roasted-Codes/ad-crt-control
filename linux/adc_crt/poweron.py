"""Boot-time power-on (``adc-crt poweron``), run as root by adc-crt-poweron.service.

Waits up to `wait_seconds` for the monitor's USB HID interface and a DDC bus, then:

    HID found, reads ON                          -> nothing to do                     (exit 0)
    HID found, OFF / SLEEP / unreadable + a bus  -> DDC/CI power-on                   (exit 0)
    HID never found, but an EDID-verified bus    -> DDC/CI power-on anyway; it is
                                                    idempotent, so this is safe       (exit 0)
    anything else at the deadline                -> log what was missing              (exit 1)

HID can only *read* power; DDC/CI can only *set* it (see the project README).
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, List, Optional, Tuple

from .protocol import PowerState


class _Log:
    """stderr logger that suppresses an identical message repeated by the 1 s poll loop."""

    def __init__(self) -> None:
        self._last: Optional[str] = None

    def __call__(self, message: str, *, once: bool = False) -> None:
        if once and message == self._last:
            return
        self._last = message
        print(f"[adc-crt poweron] {message}", file=sys.stderr, flush=True)


def _config(log: _Log) -> Tuple[Optional[int], Optional[str]]:
    try:
        from . import config
        cfg = config.load_config()
    except Exception as exc:
        log(f"ignoring configuration ({exc})")
        return None, None
    return getattr(cfg, "i2c_bus", None), getattr(cfg, "hidraw", None)


def _find_hid(hid: Any, config_hidraw: Optional[str], log: _Log) -> Optional[str]:
    if config_hidraw:
        return config_hidraw if os.path.exists(config_hidraw) else None
    try:
        infos = hid.find_hidraw()
    except Exception as exc:
        log(f"hidraw discovery failed: {exc}", once=True)
        return None
    if not infos:
        return None
    best = next((info for info in infos if info.is_monitor_interface), infos[0])
    return best.path


def _find_buses(ddc: Any, config_bus: Optional[int], log: _Log) -> List[Any]:
    try:
        return list(ddc.discover_buses(config_bus))
    except Exception as exc:
        log(f"DDC bus discovery failed: {exc}", once=True)
        return []


def _read_power(path: str, log: _Log) -> Optional[PowerState]:
    from .monitor import Monitor

    monitor = None
    try:
        monitor = Monitor(hidraw_path=path)
        return monitor.read_power()
    except Exception as exc:
        log(f"could not read the power state over HID ({path}): {exc}", once=True)
        return None
    finally:
        if monitor is not None:
            try:
                monitor.close()
            except Exception:
                pass


def _bus_name(bus: Any) -> str:
    path = getattr(bus, "path", None) or f"/dev/i2c-{bus.number}"
    return f"{path} ({bus.source})"


def _power_on(ddc: Any, buses: List[Any], reason: str, log: _Log) -> int:
    for bus in buses:
        try:
            ddc.set_power(bus.number, True)
        except Exception as exc:
            log(f"DDC/CI power-on via {_bus_name(bus)} failed: {exc}")
            continue
        log(f"sent DDC/CI power-on via {_bus_name(bus)}: {reason}")
        return 0
    log("could not power the monitor on; run `adc-crt doctor` for a step-by-step fix")
    return 1


def main(wait_seconds: int = 30, *, _clock: Callable[[], float] = time.monotonic,
         _sleep: Callable[[float], None] = time.sleep) -> int:
    from . import ddc, hid

    log = _Log()
    wait_seconds = max(0, int(wait_seconds))
    config_bus, config_hidraw = _config(log)
    deadline = _clock() + wait_seconds
    log(f"waiting up to {wait_seconds} s for the monitor (USB HID 05ac:9213) and a DDC bus")

    hid_path: Optional[str] = None
    buses: List[Any] = []
    power: Optional[PowerState] = None
    while True:
        hid_path = _find_hid(hid, config_hidraw, log)
        buses = _find_buses(ddc, config_bus, log)
        if hid_path:
            power = _read_power(hid_path, log)
            if power == PowerState.ON:
                log(f"monitor already on (HID {hid_path}); nothing to do")
                return 0
            if buses:
                break
        if _clock() >= deadline:
            break
        _sleep(1)

    if hid_path:
        state = (f"reports {getattr(power, 'name', power)}" if power is not None
                 else "power state unreadable")
        if not buses:
            log(f"monitor found on USB ({hid_path}, {state}) but no DDC bus after "
                f"{wait_seconds} s, so it cannot be powered on; run `adc-crt doctor`")
            return 1
        return _power_on(ddc, buses, f"HID {hid_path} {state}", log)

    verified = [bus for bus in buses if getattr(bus, "edid_verified", False)]
    if verified:
        log(f"no USB HID interface after {wait_seconds} s, but {_bus_name(verified[0])} carries "
            "the Studio Display EDID; sending power-on anyway (the command is idempotent)")
        return _power_on(ddc, verified, "HID missing, EDID verified", log)

    if buses:
        missing = (f"no USB HID interface (05ac:9213), and {_bus_name(buses[0])} is not verified "
                   "by EDID, so no blind DDC write was attempted")
    else:
        missing = "no USB HID interface (05ac:9213) and no DDC bus"
    log(f"gave up after {wait_seconds} s: {missing}; run `adc-crt doctor`")
    return 1


if __name__ == "__main__":
    sys.exit(main())
