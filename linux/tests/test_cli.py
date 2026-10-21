"""CLI wiring tests: a fake Monitor and fake hid/ddc/config modules, no hardware."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from typing import Any, List, Optional
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

import adc_crt  # noqa: E402
from adc_crt import cli  # noqa: E402
from adc_crt.monitor import MonitorError  # noqa: E402
from adc_crt.protocol import CONTROLS, PowerState  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "studio-display-17-crt.edid")
WRITE_METHODS = ("write_control", "write_factory_defaults", "degauss", "save", "set_power", "toggle_power",
                 "write_feature")


class FakeMonitor:
    instances: List["FakeMonitor"] = []
    power: Optional[PowerState] = PowerState.ON
    fail_with: Optional[str] = None

    def __init__(self, hidraw_path=None, i2c_bus=None):
        self.hidraw_path_arg = hidraw_path
        self.i2c_bus_arg = i2c_bus
        self.calls: List[tuple] = []
        self.closed = False
        FakeMonitor.instances.append(self)

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if FakeMonitor.fail_with:
            raise MonitorError(FakeMonitor.fail_with)

    @property
    def i2c_bus(self):
        return 1 if self.i2c_bus_arg is None else self.i2c_bus_arg

    def read_feature(self, code):
        self._record("read_feature", code)
        return {0x12: b"\x12\x5f\x00", 0xAE: b"\xae\x70\x17"}.get(code, bytes([code, 7]))

    def write_feature(self, code, payload):
        self._record("write_feature", code, bytes(payload))
        return bytes([code]) + bytes(payload)

    def read_control(self, code):
        self._record("read_control", code)
        return 42

    def read_all(self):
        self._record("read_all")
        return {c.code: c.factory_default for c in CONTROLS}

    def write_control(self, code, value):
        self._record("write_control", code, value)
        return min(max(value, 0), {c.code: c.max_value for c in CONTROLS}[code])

    def write_factory_defaults(self):
        self._record("write_factory_defaults")

    def degauss(self):
        self._record("degauss")

    def save(self):
        self._record("save")

    def read_vsync_hz(self):
        self._record("read_vsync_hz")
        return 85.0

    def read_power(self):
        self._record("read_power")
        return FakeMonitor.power

    def set_power(self, on):
        self._record("set_power", on)

    def toggle_power(self):
        self._record("toggle_power")
        return PowerState.OFF if FakeMonitor.power == PowerState.ON else PowerState.ON

    def close(self):
        self.closed = True

    @property
    def write_calls(self):
        return [c for c in self.calls if c[0] in WRITE_METHODS]


@dataclass
class Info:
    path: str
    is_monitor_interface: bool
    sysfs: str = "/sys/class/hidraw/hidraw0"
    name: str = "Apple Computer, Inc. Studio Display"


@dataclass
class Bus:
    number: int
    source: str
    edid_verified: bool
    path: str = ""


@dataclass
class Config:
    i2c_bus: Optional[int] = None
    hidraw: Optional[str] = None


def _forbidden(*args, **kwargs):
    raise AssertionError("a write/transport-mutating function was called")


def make_fake_hid(infos=None, hiddev="/dev/usb/hiddev0"):
    return types.SimpleNamespace(
        find_hidraw=lambda: list([Info("/dev/hidraw0", True)] if infos is None else infos),
        find_hiddev=lambda: hiddev,
        HidrawDevice=_forbidden,
    )


def make_fake_ddc(buses=None, edid=b""):
    calls = []

    def discover_buses(config_bus=None):
        calls.append(("discover_buses", config_bus))
        return list([Bus(1, "raspberry-pi-gpio", True, "/dev/i2c-1")] if buses is None else buses)

    def read_edid(bus):
        calls.append(("read_edid", bus))
        return edid

    return types.SimpleNamespace(discover_buses=discover_buses, read_edid=read_edid, set_power=_forbidden,
                                 write_ddc=_forbidden, calls=calls)


def make_fake_config(config=None):
    return types.SimpleNamespace(load_config=lambda: config or Config())


class CliTestCase(unittest.TestCase):
    def setUp(self):
        FakeMonitor.instances = []
        FakeMonitor.power = PowerState.ON
        FakeMonitor.fail_with = None
        self.hid = make_fake_hid()
        self.ddc = make_fake_ddc()
        self.config = make_fake_config()
        patches = [
            mock.patch.object(cli, "Monitor", FakeMonitor),
            mock.patch.object(cli, "_hid_module", lambda: self.hid),
            mock.patch.object(cli, "_ddc_module", lambda: self.ddc),
            mock.patch.object(cli, "_config_module", lambda: self.config),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    @property
    def monitor(self) -> FakeMonitor:
        self.assertEqual(len(FakeMonitor.instances), 1)
        return FakeMonitor.instances[0]


class BasicTests(CliTestCase):
    def test_version(self):
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(adc_crt.__version__, out)

    def test_help(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        for command in ("doctor", "self-test", "hid", "ddc", "power", "get", "set", "defaults", "degauss",
                        "save", "vsync", "gui", "buttons", "poweron", "keybinding"):
            self.assertIn(command, out)

    def test_no_command_is_usage_error(self):
        self.assertEqual(self.run_cli()[0], 2)

    def test_unknown_command_is_usage_error(self):
        self.assertEqual(self.run_cli("frobnicate")[0], 2)

    def test_bad_arguments_are_usage_errors(self):
        for argv in (("set", "contrast", "abc"), ("set", "contrast"), ("hid", "write", "0x12", "300"),
                     ("hid", "read", "zz"), ("hid",), ("ddc",), ("power", "sideways"), ("--bus", "x", "power"),
                     ("poweron", "--wait", "-1"), ("keybinding", "sway")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv)[0], 2)
        self.assertEqual(FakeMonitor.instances, [])

    def test_unknown_control_lists_valid_names(self):
        code, _, err = self.run_cli("get", "brightnes")
        self.assertEqual(code, 2)
        self.assertIn("h-key-balance", err)
        self.assertIn("contrast", err)
        code, _, err = self.run_cli("set", "0x01", "1")
        self.assertEqual(code, 2)
        self.assertIn("valid names", err)

    def test_monitor_error_exits_1(self):
        FakeMonitor.fail_with = "USB HID device not found. Run `adc-crt doctor` for a full diagnosis."
        code, _, err = self.run_cli("power", "read")
        self.assertEqual(code, 1)
        self.assertIn("adc-crt doctor", err)
        self.assertTrue(self.monitor.closed)

    def test_oserror_exits_1_with_hint(self):
        def boom():
            raise OSError(13, "Permission denied")

        self.hid.find_hidraw = boom
        code, _, err = self.run_cli("hid", "list")
        self.assertEqual(code, 1)
        self.assertIn("adc-crt doctor", err)

    def test_self_test(self):
        code, out, _ = self.run_cli("self-test")
        self.assertEqual(code, 0)
        self.assertIn("self-test passed", out)
        if os.path.exists(FIXTURE):
            self.assertIn("reference dump recognized", out)
        self.assertEqual(FakeMonitor.instances, [])

    def test_self_test_detects_drift(self):
        with mock.patch.object(cli, "POWER_ON_PACKET", b"\x51\x84\x03\xd6\x00\x01\x00"):
            code, out, err = self.run_cli("self-test")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err)


class GlobalOptionTests(CliTestCase):
    def test_global_options_before_command(self):
        self.assertEqual(self.run_cli("--bus", "3", "--hidraw", "/dev/hidraw7", "power", "on")[0], 0)
        self.assertEqual((self.monitor.i2c_bus_arg, self.monitor.hidraw_path_arg), (3, "/dev/hidraw7"))

    def test_global_options_after_command(self):
        self.assertEqual(self.run_cli("power", "off", "--bus", "0x4", "--hidraw", "/dev/hidraw2")[0], 0)
        self.assertEqual((self.monitor.i2c_bus_arg, self.monitor.hidraw_path_arg), (4, "/dev/hidraw2"))

    def test_options_before_command_survive_subparser(self):
        self.assertEqual(self.run_cli("--bus", "5", "hid", "read", "0x12")[0], 0)
        self.assertEqual(self.monitor.i2c_bus_arg, 5)

    def test_defaults_are_auto(self):
        self.run_cli("vsync")
        self.assertEqual((self.monitor.i2c_bus_arg, self.monitor.hidraw_path_arg), (None, None))


class PowerTests(CliTestCase):
    def test_power_read_default(self):
        code, out, _ = self.run_cli("power")
        self.assertEqual(code, 0)
        self.assertIn("ON (1)", out)
        self.assertEqual(self.monitor.calls, [("read_power",)])

    def test_power_read_unknown_exits_1(self):
        FakeMonitor.power = None
        code, out, _ = self.run_cli("power", "read")
        self.assertEqual(code, 1)
        self.assertIn("unknown", out)

    def test_power_on_off(self):
        self.assertEqual(self.run_cli("power", "on")[0], 0)
        self.assertEqual(FakeMonitor.instances[0].calls, [("set_power", True)])
        self.assertEqual(self.run_cli("power", "off")[0], 0)
        self.assertEqual(FakeMonitor.instances[1].calls, [("set_power", False)])

    def test_power_toggle(self):
        code, out, _ = self.run_cli("power", "toggle")
        self.assertEqual(code, 0)
        self.assertIn("power off sent", out)
        self.assertEqual(self.monitor.calls, [("toggle_power",)])
        FakeMonitor.power = PowerState.OFF
        FakeMonitor.instances = []
        code, out, _ = self.run_cli("power", "toggle")
        self.assertIn("power on sent", out)


class ControlTests(CliTestCase):
    def test_get_all_table(self):
        for argv in (("get",), ("get", "all")):
            FakeMonitor.instances = []
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0)
            lines = out.strip().splitlines()
            self.assertEqual(len(lines), 12)
            self.assertIn("Code", lines[0])
            self.assertIn("Max", lines[0])
            self.assertTrue(lines[1].startswith("Contrast"))
            self.assertIn("0x12", lines[1])
            self.assertTrue(lines[1].rstrip().endswith("95   96"))
            self.assertTrue(lines[9].startswith("Rotation"))
            self.assertTrue(lines[9].rstrip().endswith("152  255"))

    def test_get_by_name_and_code(self):
        for key in ("h-phase", "H Phase", "0x20", "32"):
            FakeMonitor.instances = []
            code, out, _ = self.run_cli("get", key)
            self.assertEqual(code, 0)
            self.assertEqual(self.monitor.calls, [("read_control", 0x20)])
            self.assertIn("H Phase (0x20) = 42", out)

    def test_set_reports_clamping(self):
        code, out, _ = self.run_cli("set", "contrast", "200")
        self.assertEqual(code, 0)
        self.assertEqual(self.monitor.calls, [("write_control", 0x12, 200)])
        self.assertIn("= 96 (clamped from 200)", out)

    def test_set_hex_value(self):
        self.assertEqual(self.run_cli("set", "rotation", "0x80")[0], 0)
        self.assertEqual(self.monitor.calls, [("write_control", 0x44, 0x80)])

    def test_defaults_degauss_save_vsync(self):
        expected = {"defaults": ("write_factory_defaults",), "degauss": ("degauss",), "save": ("save",),
                    "vsync": ("read_vsync_hz",)}
        for command, call in expected.items():
            FakeMonitor.instances = []
            code, out, _ = self.run_cli(command)
            self.assertEqual(code, 0, command)
            self.assertEqual(self.monitor.calls, [call])
        self.assertIn("85.00 Hz", out)


class HidDdcTests(CliTestCase):
    def test_hid_list(self):
        self.hid = make_fake_hid([Info("/dev/hidraw0", False), Info("/dev/hidraw1", True)])
        code, out, _ = self.run_cli("hid", "list")
        self.assertEqual(code, 0)
        self.assertIn("* /dev/hidraw1", out)
        self.assertIn("  /dev/hidraw0", out)
        self.assertIn("monitor control", out)
        self.assertIn("/dev/usb/hiddev0", out)
        self.assertEqual(FakeMonitor.instances, [])

    def test_hid_list_empty(self):
        self.hid = make_fake_hid([])
        code, out, _ = self.run_cli("hid", "list")
        self.assertEqual(code, 1)
        self.assertIn("adc-crt doctor", out)

    def test_hid_read(self):
        code, out, _ = self.run_cli("hid", "read", "0x12")
        self.assertEqual(code, 0)
        self.assertIn("0x12 = 95", out)
        self.assertIn("raw: 12 5F 00", out)
        FakeMonitor.instances = []
        code, out, _ = self.run_cli("hid", "read", "0xAE")
        self.assertIn("vsync: 60.00 Hz", out)

    def test_hid_write(self):
        code, out, _ = self.run_cli("hid", "write", "0x12", "95", "0")
        self.assertEqual(code, 0)
        self.assertEqual(self.monitor.calls, [("write_feature", 0x12, b"\x5f\x00")])
        self.assertIn("12 5F 00", out)

    def test_ddc_buses(self):
        self.ddc = make_fake_ddc([Bus(5, "drm:card0-DP-1", True, "/dev/i2c-5"),
                                  Bus(1, "raspberry-pi-gpio", False, "/dev/i2c-1")])
        code, out, _ = self.run_cli("ddc", "buses", "--bus", "5")
        self.assertEqual(code, 0)
        self.assertIn("* /dev/i2c-5  drm:card0-DP-1  EDID verified", out)
        self.assertIn("  /dev/i2c-1  raspberry-pi-gpio  EDID not verified", out)
        self.assertIn("51 84 03 D6 00 01 6F", out)
        self.assertEqual(self.ddc.calls, [("discover_buses", 5)])

    def test_ddc_buses_uses_config_bus(self):
        self.config = make_fake_config(Config(i2c_bus=9))
        self.run_cli("ddc", "buses")
        self.assertEqual(self.ddc.calls, [("discover_buses", 9)])

    def test_ddc_buses_none(self):
        self.ddc = make_fake_ddc([])
        self.assertEqual(self.run_cli("ddc", "buses")[0], 1)

    @unittest.skipUnless(os.path.exists(FIXTURE), "EDID fixture missing")
    def test_ddc_edid(self):
        with open(FIXTURE, "rb") as handle:
            edid = handle.read()[:128]
        self.ddc = make_fake_ddc(edid=edid)
        code, out, err = self.run_cli("ddc", "edid", "--bus", "4")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), edid.hex().upper())
        self.assertIn("Apple Studio Display", err)
        self.assertEqual(self.ddc.calls, [("read_edid", 4)])

    def test_ddc_edid_resolves_bus_through_monitor(self):
        self.ddc = make_fake_ddc(edid=bytes(128))
        code, _, err = self.run_cli("ddc", "edid")
        self.assertEqual(code, 0)
        self.assertEqual(self.ddc.calls, [("read_edid", 1)])
        self.assertIn("not a valid EDID", err)


class LazyModuleTests(CliTestCase):
    def install(self, name, module):
        module.__name__ = f"adc_crt.{name}"
        p1 = mock.patch.dict(sys.modules, {f"adc_crt.{name}": module})
        p2 = mock.patch.object(adc_crt, name, module, create=True)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def test_gui(self):
        fake = types.ModuleType("gui")
        fake.main = mock.Mock(return_value=0)
        self.install("gui", fake)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADC_CRT_I2C_BUS", None)
            code, _, _ = self.run_cli("--bus", "6", "gui")
            self.assertEqual(os.environ.get("ADC_CRT_I2C_BUS"), "6")
        self.assertEqual(code, 0)
        fake.main.assert_called_once_with()

    def test_buttons(self):
        fake = types.ModuleType("buttons")
        fake.main = mock.Mock(return_value=3)
        self.install("buttons", fake)
        self.assertEqual(self.run_cli("buttons")[0], 3)
        fake.main.assert_called_once_with()

    def test_poweron(self):
        fake = types.ModuleType("poweron")
        fake.main = mock.Mock(return_value=0)
        self.install("poweron", fake)
        self.assertEqual(self.run_cli("poweron")[0], 0)
        fake.main.assert_called_once_with(wait_seconds=30)
        fake.main.reset_mock()
        self.assertEqual(self.run_cli("poweron", "--wait", "5")[0], 0)
        fake.main.assert_called_once_with(wait_seconds=5)

    def test_keybinding(self):
        fake = types.ModuleType("keybinding")
        fake.main = mock.Mock(return_value=0)
        self.install("keybinding", fake)
        self.assertEqual(self.run_cli("keybinding", "labwc")[0], 0)
        args = fake.main.call_args[0][0]
        self.assertEqual((args.wm, args.install), ("labwc", False))
        self.assertEqual(self.run_cli("keybinding", "labwc", "--install")[0], 0)
        args = fake.main.call_args[0][0]
        self.assertEqual((args.wm, args.install), ("labwc", True))

    def test_gui_missing_gtk_is_actionable(self):
        real_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 1 and fromlist and "gui" in fromlist:
                raise ImportError("No module named 'gi'")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", fake_import):
            code, _, err = self.run_cli("gui")
        self.assertEqual(code, 1)
        self.assertIn("python3-gi", err)


class DoctorTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.sys_root = os.path.join(root, "sys")
        self.dev_root = os.path.join(root, "dev")
        self.proc_root = os.path.join(root, "proc")
        os.makedirs(os.path.join(self.sys_root, "class", "i2c-dev"))
        os.makedirs(self.dev_root)
        os.makedirs(os.path.join(self.proc_root, "device-tree"))
        for name in ("hidraw0", "i2c-1", "i2c-5"):
            open(os.path.join(self.dev_root, name), "w").close()
        self.hid = make_fake_hid([Info(os.path.join(self.dev_root, "hidraw0"), True)],
                                 hiddev=os.path.join(self.dev_root, "hiddev0"))
        self.ddc = make_fake_ddc([Bus(5, "drm:card0-DP-1", True, os.path.join(self.dev_root, "i2c-5"))])
        self.access = {}
        for name, value in (("SYS_ROOT", self.sys_root), ("DEV_ROOT", self.dev_root),
                            ("PROC_ROOT", self.proc_root)):
            p = mock.patch.object(cli, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cli, "_access", lambda path, mode: self.access.get(path, True))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cli, "_i2c_group_status", lambda: ("member", "james"))
        p.start()
        self.addCleanup(p.stop)

    def make_pi(self):
        with open(os.path.join(self.proc_root, "device-tree", "model"), "wb") as handle:
            handle.write(b"Raspberry Pi 2 Model B Rev 1.1\x00")
        self.ddc = make_fake_ddc([Bus(1, "raspberry-pi-gpio", True, os.path.join(self.dev_root, "i2c-1"))])

    def assert_read_only(self):
        for monitor in FakeMonitor.instances:
            self.assertEqual(monitor.write_calls, [])
            self.assertTrue(monitor.closed)

    def test_all_good_desktop(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("All required checks passed", out)
        self.assertIn("power state (HID 0xD6): ON (1)", out)
        self.assertIn("drm:card0-DP-1", out)
        self.assertNotIn("fix:", out)
        self.assertNotIn("Raspberry Pi", out)
        self.assertEqual(self.monitor.calls, [("read_power",)])
        self.assert_read_only()

    def test_all_good_pi(self):
        self.make_pi()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("Raspberry Pi I2C enabled", out)
        self.assertIn("user james is in group i2c", out)
        self.assert_read_only()

    def test_nothing_found(self):
        self.hid = make_fake_hid([], hiddev=None)
        self.ddc = make_fake_ddc([])
        os.rmdir(os.path.join(self.sys_root, "class", "i2c-dev"))
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("USB HID device 05ac:9213 (Apple Studio Display) not found", out)
        self.assertIn("sudo modprobe i2c-dev", out)
        self.assertIn("--bus N", out)
        self.assertIn("power state: not checked", out)
        self.assertIn("required check(s) failed", out)
        self.assertEqual(FakeMonitor.instances, [])
        # every failure line is followed by at least one fix line
        lines = out.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(cli._marks()[1]):
                following = [ln for ln in lines[index + 1:index + 6] if ln.startswith("    ")]
                self.assertTrue(any(ln.strip().startswith("fix:") for ln in following), line)

    def test_permission_failures(self):
        self.make_pi()
        hidraw = os.path.join(self.dev_root, "hidraw0")
        bus = os.path.join(self.dev_root, "i2c-1")
        self.access = {hidraw: False, bus: False}
        with mock.patch.object(cli, "_i2c_group_status", lambda: ("not-member", "james")):
            code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn(f"you cannot read and write {hidraw}", out)
        self.assertIn("udevadm control --reload-rules", out)
        self.assertIn("sudo usermod -aG i2c $USER", out)
        self.assertIn("user james is not in group i2c", out)
        self.assertEqual(FakeMonitor.instances, [])  # HID not usable -> power not read

    def test_pi_i2c_disabled(self):
        self.make_pi()
        os.remove(os.path.join(self.dev_root, "i2c-1"))
        self.ddc = make_fake_ddc([])
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("raspi-config nonint do_i2c 0", out)
        self.assertIn("GPIO wiring", out)
        self.assert_read_only()

    def test_unverified_bus_is_a_warning(self):
        self.make_pi()
        self.ddc = make_fake_ddc([Bus(1, "raspberry-pi-gpio", False, os.path.join(self.dev_root, "i2c-1"))])
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("(optional)", out)
        self.assertIn("EDID not verified", out)

    def test_power_read_failure(self):
        FakeMonitor.fail_with = "Reading HID feature report 0xD6 failed. Run `adc-crt doctor` for a full diagnosis."
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("power state could not be read over HID", out)
        self.assert_read_only()

    def test_bus_override_passed_to_discovery(self):
        self.run_cli("doctor", "--bus", "5")
        self.assertEqual(self.ddc.calls, [("discover_buses", 5)])
        self.assert_read_only()

    def test_config_error_is_reported(self):
        def broken():
            raise ValueError("[ddc] bus must be a number")

        self.config = types.SimpleNamespace(load_config=broken)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("[ddc] bus must be a number", out)


if __name__ == "__main__":
    unittest.main()
