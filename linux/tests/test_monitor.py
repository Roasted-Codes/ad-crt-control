"""Monitor API tests with fake transports (no hardware, no dependency on hid.py/ddc.py/config.py)."""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from adc_crt.monitor import Monitor, MonitorError  # noqa: E402
from adc_crt.protocol import CONTROLS, CONTROLS_BY_CODE, PowerState  # noqa: E402


@dataclass
class FakeInfo:
    path: str
    is_monitor_interface: bool
    sysfs: str = "/sys/class/hidraw/x"
    name: str = "Apple Studio Display"


@dataclass
class FakeBus:
    number: int
    source: str
    edid_verified: bool = True


@dataclass
class FakeConfig:
    i2c_bus: Optional[int] = None
    hidraw: Optional[str] = None


class FakeTransportError(OSError):
    """Like hid.HidError / ddc.DdcError: str() is an actionable message."""

    def __str__(self) -> str:
        return self.strerror or super().__str__()


class FakeDevice:
    def __init__(self, path: str, reports: Dict[int, bytes], reject: Optional[Dict[bytes, int]] = None,
                 read_error: Optional[OSError] = None) -> None:
        self.path = path
        self.reports = reports
        self.reject = reject or {}
        self.read_error = read_error
        self.attempts: List[bytes] = []
        self.accepted: List[bytes] = []
        self.closed = False
        self.busy = False
        self.overlaps = 0

    def _enter(self) -> None:
        if self.busy:
            self.overlaps += 1
        self.busy = True
        time.sleep(0.001)

    def get_feature(self, code: int, length: int = 132) -> bytes:
        self._enter()
        try:
            if self.read_error is not None:
                raise self.read_error
            return self.reports.get(code, b"")
        finally:
            self.busy = False

    def set_feature(self, data: bytes) -> int:
        self._enter()
        try:
            data = bytes(data)
            self.attempts.append(data)
            if data in self.reject:
                code = self.reject[data]
                raise OSError(code, os.strerror(code))
            self.accepted.append(data)
            return len(data)
        finally:
            self.busy = False

    def close(self) -> None:
        self.closed = True


class Harness:
    """Builds a Monitor whose every hardware hook is a fake, and records what it did."""

    def __init__(self, power: Optional[bytes] = b"\xd6\x01", infos=None, buses=None, config=None,
                 reject=None, read_error=None, ddc_error: Optional[OSError] = None) -> None:
        self.reports: Dict[int, bytes] = {c.code: bytes([c.code, c.factory_default, 0]) for c in CONTROLS}
        self.reports[0xAE] = b"\xae\x70\x17"
        if power is not None:
            self.reports[0xD6] = power
        self.infos = [FakeInfo("/dev/hidraw0", True)] if infos is None else infos
        self.buses = [FakeBus(1, "raspberry-pi-gpio")] if buses is None else buses
        self.config = FakeConfig() if config is None else config
        self.reject = reject
        self.read_error = read_error
        self.ddc_error = ddc_error
        self.devices: List[FakeDevice] = []
        self.power_calls: List[tuple] = []
        self.find_calls = 0
        self.discover_calls = 0

    def factory(self, path: str) -> FakeDevice:
        device = FakeDevice(path, self.reports, self.reject, self.read_error)
        self.devices.append(device)
        return device

    def find(self):
        self.find_calls += 1
        return list(self.infos)

    def discover(self):
        self.discover_calls += 1
        return list(self.buses)

    def set_power(self, bus: int, on: bool) -> None:
        if self.ddc_error is not None:
            raise self.ddc_error
        self.power_calls.append((bus, on))

    def monitor(self, hidraw_path: Optional[str] = None, i2c_bus: Optional[int] = None) -> Monitor:
        return Monitor(hidraw_path, i2c_bus, _hid_factory=self.factory, _find_hidraw=self.find,
                       _discover_buses=self.discover, _ddc_set_power=self.set_power,
                       _load_config=lambda: self.config)

    @property
    def accepted(self) -> List[bytes]:
        return [data for device in self.devices for data in device.accepted]


class TogglePowerTests(unittest.TestCase):
    def test_on_turns_off(self):
        h = Harness(power=b"\xd6\x01")
        self.assertEqual(h.monitor().toggle_power(), PowerState.OFF)
        self.assertEqual(h.power_calls, [(1, False)])

    def test_off_sleep_and_unknown_turn_on(self):
        for raw in (b"\xd6\x04", b"\xd6\x02", b"\xd6\x03", b"", b"\x04", b"\x02"):
            with self.subTest(raw=raw):
                h = Harness(power=raw)
                self.assertEqual(h.monitor().toggle_power(), PowerState.ON)
                self.assertEqual(h.power_calls, [(1, True)])

    def test_unreadable_hid_turns_on(self):
        h = Harness(read_error=OSError(errno.EIO, "Input/output error"))
        self.assertEqual(h.monitor().toggle_power(), PowerState.ON)
        self.assertEqual(h.power_calls, [(1, True)])

    def test_missing_hid_device_turns_on(self):
        h = Harness(infos=[])
        self.assertEqual(h.monitor().toggle_power(), PowerState.ON)
        self.assertEqual(h.power_calls, [(1, True)])

    def test_unopenable_hid_device_turns_on(self):
        h = Harness()

        def denied(path):
            raise OSError(errno.EACCES, "Permission denied")

        m = Monitor(None, None, _hid_factory=denied, _find_hidraw=h.find, _discover_buses=h.discover,
                    _ddc_set_power=h.set_power, _load_config=lambda: h.config)
        self.assertEqual(m.toggle_power(), PowerState.ON)
        self.assertEqual(h.power_calls, [(1, True)])

    def test_toggle_without_bus_raises_actionable_error(self):
        h = Harness(power=b"\xd6\x04", buses=[])
        with self.assertRaises(MonitorError) as cm:
            h.monitor().toggle_power()
        self.assertIn("adc-crt doctor", str(cm.exception))
        self.assertEqual(h.power_calls, [])


class ControlWriteTests(unittest.TestCase):
    def test_clamps_and_returns_written_value(self):
        h = Harness()
        m = h.monitor()
        self.assertEqual(m.write_control(0x12, 200), 96)
        self.assertEqual(m.write_control(0x12, -5), 0)
        self.assertEqual(m.write_control(0x44, 255), 255)
        self.assertEqual(m.write_control(0x44, 300), 255)
        self.assertEqual(m.write_control(0x20, 50.6), 51)
        self.assertEqual(h.accepted, [b"\x12\x60\x00", b"\x12\x00\x00", b"\x44\xff\x00", b"\x44\xff\x00",
                                      b"\x20\x33\x00"])

    def test_unknown_control_code(self):
        with self.assertRaises(ValueError):
            Harness().monitor().write_control(0x01, 1)

    def test_factory_defaults_written_in_controls_order(self):
        h = Harness()
        h.monitor().write_factory_defaults()
        self.assertEqual(h.accepted, [bytes([c.code, c.factory_default, 0]) for c in CONTROLS])
        self.assertEqual(len(h.accepted), 11)

    def test_falls_back_to_trimmed_candidate(self):
        h = Harness(reject={b"\x12\x50\x00": errno.EPIPE})
        self.assertEqual(h.monitor().write_control(0x12, 0x50), 0x50)
        self.assertEqual(h.devices[0].attempts, [b"\x12\x50\x00", b"\x12\x50"])
        self.assertEqual(h.accepted, [b"\x12\x50"])

    def test_zero_value_falls_back_to_two_bytes(self):
        h = Harness(reject={b"\x12\x00\x00": errno.EINVAL})
        h.monitor().write_control(0x12, 0)
        self.assertEqual(h.accepted, [b"\x12\x00"])

    def test_all_candidates_fail_raises_last_errno(self):
        h = Harness(reject={b"\x12\x50\x00": errno.EPIPE, b"\x12\x50": errno.EIO})
        with self.assertRaises(MonitorError) as cm:
            h.monitor().write_control(0x12, 0x50)
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertEqual(h.devices[0].attempts, [b"\x12\x50\x00", b"\x12\x50"])
        self.assertIn("0x12", str(cm.exception))
        self.assertIn("adc-crt doctor", str(cm.exception))

    def test_degauss_and_save(self):
        h = Harness()
        m = h.monitor()
        m.degauss()
        m.save()
        self.assertEqual(h.accepted, [b"\x01\x01", b"\xb0\x01"])

    def test_raw_write_feature_returns_accepted_buffer(self):
        h = Harness(reject={b"\x30\x10\x00\x00": errno.EPIPE})
        self.assertEqual(h.monitor().write_feature(0x30, b"\x10\x00\x00"), b"\x30\x10")


class ReadTests(unittest.TestCase):
    def test_read_control_strips_report_id(self):
        h = Harness()
        h.reports[0x12] = b"\x12\x5f\x00"
        self.assertEqual(h.monitor().read_control(0x12), 95)

    def test_read_control_without_report_id(self):
        h = Harness()
        h.reports[0x20] = b"\x40"
        self.assertEqual(h.monitor().read_control(0x20), 0x40)

    def test_read_all_in_controls_order(self):
        values = Harness().monitor().read_all()
        self.assertEqual(list(values), [c.code for c in CONTROLS])
        self.assertEqual(values, {c.code: c.factory_default for c in CONTROLS})

    def test_vsync_and_power(self):
        m = Harness(power=b"\xd6\x04").monitor()
        self.assertEqual(m.read_vsync_hz(), 60.0)
        self.assertEqual(m.read_power(), PowerState.OFF)

    def test_unknown_power_is_none(self):
        self.assertIsNone(Harness(power=b"\xd6\x07").monitor().read_power())
        self.assertIsNone(Harness(power=None).monitor().read_power())

    def test_empty_control_report_raises(self):
        h = Harness()
        h.reports[0x12] = b""
        with self.assertRaises(MonitorError) as cm:
            h.monitor().read_control(0x12)
        self.assertIn("adc-crt doctor", str(cm.exception))

    def test_read_error_is_actionable(self):
        h = Harness(read_error=OSError(errno.EPIPE, "Broken pipe"))
        with self.assertRaises(MonitorError) as cm:
            h.monitor().read_control(0x12)
        self.assertEqual(cm.exception.errno, errno.EPIPE)
        self.assertIn("adc-crt doctor", str(cm.exception))


class ResolutionTests(unittest.TestCase):
    def test_no_hidraw_found(self):
        h = Harness(infos=[])
        with self.assertRaises(MonitorError) as cm:
            h.monitor().read_power()
        message = str(cm.exception)
        self.assertIn("05ac:9213", message)
        self.assertIn("--hidraw", message)
        self.assertIn("adc-crt doctor", message)

    def test_no_bus_found(self):
        h = Harness(buses=[])
        m = h.monitor()
        with self.assertRaises(MonitorError) as cm:
            m.set_power(True)
        message = str(cm.exception)
        self.assertIn("--bus", message)
        self.assertIn("raspi-config", message)
        self.assertIn("adc-crt doctor", message)
        self.assertEqual(h.power_calls, [])

    def test_prefers_monitor_interface(self):
        h = Harness(infos=[FakeInfo("/dev/hidraw2", False), FakeInfo("/dev/hidraw5", True)])
        self.assertEqual(h.monitor().hidraw_path, "/dev/hidraw5")

    def test_falls_back_to_first_node(self):
        h = Harness(infos=[FakeInfo("/dev/hidraw2", False), FakeInfo("/dev/hidraw3", False)])
        self.assertEqual(h.monitor().hidraw_path, "/dev/hidraw2")

    def test_hidraw_precedence(self):
        h = Harness(config=FakeConfig(hidraw="/dev/hidraw9"))
        self.assertEqual(h.monitor("/dev/hidraw7").hidraw_path, "/dev/hidraw7")
        self.assertEqual(h.monitor().hidraw_path, "/dev/hidraw9")
        self.assertEqual(h.find_calls, 0)
        self.assertEqual(Harness().monitor().hidraw_path, "/dev/hidraw0")

    def test_bus_precedence(self):
        h = Harness(config=FakeConfig(i2c_bus=7), buses=[FakeBus(4, "drm:card0-DP-1")])
        m = h.monitor(i2c_bus=0)
        self.assertEqual(m.i2c_bus, 0)  # explicit 0 is a real bus number, not "unset"
        m.set_power(False)
        self.assertEqual(h.power_calls, [(0, False)])
        self.assertEqual(h.monitor().i2c_bus, 7)
        self.assertEqual(h.discover_calls, 0)
        h2 = Harness(buses=[FakeBus(4, "drm:card0-DP-1"), FakeBus(1, "raspberry-pi-gpio", False)])
        m2 = h2.monitor()
        self.assertEqual(m2.i2c_bus, 4)
        self.assertEqual(m2.i2c_bus_source, "auto (drm:card0-DP-1)")

    def test_resolution_is_lazy(self):
        h = Harness()
        h.monitor()
        self.assertEqual((h.find_calls, h.discover_calls, len(h.devices)), (0, 0, 0))

    def test_config_error_is_actionable(self):
        def broken():
            raise ValueError("bus must be a number")

        m = Monitor(_find_hidraw=lambda: [], _load_config=broken)
        with self.assertRaises(MonitorError) as cm:
            m.read_power()
        self.assertIn("bus must be a number", str(cm.exception))
        self.assertIn("config.ini", str(cm.exception))

    def test_ddc_error_passes_through_actionable_message(self):
        error = FakeTransportError(errno.ENXIO, "no ACK from the monitor at 0x37 on /dev/i2c-1; run `adc-crt doctor`")
        h = Harness(ddc_error=error)
        with self.assertRaises(MonitorError) as cm:
            h.monitor().set_power(True)
        self.assertEqual(cm.exception.errno, errno.ENXIO)
        self.assertIn("no ACK", str(cm.exception))
        self.assertEqual(str(cm.exception).count("adc-crt doctor"), 1)

    def test_plain_ddc_oserror_gets_a_fix(self):
        h = Harness(ddc_error=OSError(errno.EACCES, "Permission denied"))
        with self.assertRaises(MonitorError) as cm:
            h.monitor().set_power(False)
        self.assertIn("usermod -aG i2c", str(cm.exception))
        self.assertIn("adc-crt doctor", str(cm.exception))

    def test_open_permission_error_mentions_udev(self):
        def denied(path):
            raise OSError(errno.EACCES, "Permission denied")

        m = Monitor(_hid_factory=denied, _find_hidraw=lambda: [FakeInfo("/dev/hidraw0", True)],
                    _load_config=FakeConfig)
        with self.assertRaises(MonitorError) as cm:
            m.read_power()
        self.assertEqual(cm.exception.errno, errno.EACCES)
        self.assertIn("udev", str(cm.exception))
        self.assertIn("adc-crt doctor", str(cm.exception))


class DeviceLifecycleTests(unittest.TestCase):
    def test_one_device_reused_and_closed(self):
        h = Harness()
        m = h.monitor()
        m.read_all()
        m.write_control(0x12, 50)
        m.read_power()
        m.toggle_power()
        self.assertEqual(len(h.devices), 1)
        m.close()
        self.assertTrue(h.devices[0].closed)
        m.read_power()  # reopens after close
        self.assertEqual(len(h.devices), 2)

    def test_context_manager_closes(self):
        h = Harness()
        with h.monitor() as m:
            m.read_power()
        self.assertTrue(h.devices[0].closed)

    def test_device_gone_is_rediscovered(self):
        h = Harness()
        m = h.monitor()
        m.read_power()
        h.devices[0].read_error = OSError(errno.ENODEV, "No such device")
        with self.assertRaises(MonitorError):
            m.read_power()
        self.assertTrue(h.devices[0].closed)
        h.infos = [FakeInfo("/dev/hidraw4", True)]
        self.assertEqual(m.read_power(), PowerState.ON)
        self.assertEqual(h.devices[1].path, "/dev/hidraw4")

    def test_access_is_serialized(self):
        h = Harness()
        m = h.monitor()
        m.read_power()
        errors: List[BaseException] = []

        def worker():
            try:
                for _ in range(20):
                    m.read_control(0x12)
                    m.write_control(0x20, 10)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(h.devices[0].overlaps, 0)
        self.assertEqual(len(h.devices), 1)


if __name__ == "__main__":
    unittest.main()
