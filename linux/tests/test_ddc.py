"""DDC bus discovery, EDID read and DDC/CI writes against a fake sysfs/proc/dev tree and fake I2C."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from unittest import mock

from adc_crt import ddc
from adc_crt.ddc import DdcBus, DdcError
from adc_crt.protocol import POWER_OFF_PACKET, POWER_ON_PACKET

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(TESTS_DIR, "fixtures", "studio-display-17-crt.edid"), "rb") as _handle:
    STUDIO_EDID = _handle.read()
OTHER_EDID = STUDIO_EDID[:10] + b"\x14\x92" + STUDIO_EDID[12:128]  # Apple, different product
EREMOTEIO = getattr(errno, "EREMOTEIO", 121)


class FakeI2C:
    """os.open/os.read/os.write/os.close/fcntl.ioctl for /dev/i2c-N, recording every access.

    ``buses`` maps bus number -> EDID bytes (device answers at 0x50 and 0x37) or an errno that
    the first transfer raises. ``open_errors`` / ``ioctl_errors`` map bus -> errno.
    """

    def __init__(self, dev_root, buses=None, open_errors=None, ioctl_errors=None, short_write=False):
        self.dev_root = dev_root
        self.buses = buses or {}
        self.open_errors = open_errors or {}
        self.ioctl_errors = ioctl_errors or {}
        self.short_write = short_write
        self.opened = []      # bus numbers, in order
        self.closed = []
        self.log = []         # (bus, op, detail)
        self.fds = {}
        self.slave = {}

    def bus_of(self, path):
        directory, name = os.path.split(path)
        assert directory == self.dev_root and name.startswith("i2c-"), path
        return int(name[4:])

    def open(self, path, flags):
        assert flags & os.O_ACCMODE == os.O_RDWR, flags
        bus = self.bus_of(path)
        self.opened.append(bus)
        if bus in self.open_errors:
            raise OSError(self.open_errors[bus], os.strerror(self.open_errors[bus]), path)
        fd = 100 + len(self.opened)
        self.fds[fd] = bus
        return fd

    def ioctl(self, fd, request, arg):
        bus = self.fds[fd]
        assert request == ddc.I2C_SLAVE
        self.log.append((bus, "slave", arg))
        if bus in self.ioctl_errors:
            raise OSError(self.ioctl_errors[bus], os.strerror(self.ioctl_errors[bus]))
        self.slave[fd] = arg
        return 0

    def _device(self, fd):
        answer = self.buses.get(self.fds[fd], errno.ENXIO)
        if isinstance(answer, int):
            raise OSError(answer, os.strerror(answer))
        return answer

    def write(self, fd, data):
        bus = self.fds[fd]
        self.log.append((bus, "write", self.slave[fd], bytes(data)))
        self._device(fd)
        return len(data) - 1 if self.short_write else len(data)

    def read(self, fd, count):
        bus = self.fds[fd]
        self.log.append((bus, "read", self.slave[fd], count))
        edid = self._device(fd)
        assert self.slave[fd] == 0x50
        return edid[:count]

    def close(self, fd):
        self.closed.append(fd)

    def install(self, test):
        for name, func in (("_os_open", self.open), ("_ioctl", self.ioctl), ("_os_write", self.write),
                           ("_os_read", self.read), ("_os_close", self.close)):
            patcher = mock.patch.object(ddc, name, func)
            patcher.start()
            test.addCleanup(patcher.stop)
        return self

    def all_closed(self):
        return sorted(self.closed) == sorted(self.fds)


class DiscoverBusesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.sys = os.path.join(self.root, "sys")
        self.dev = os.path.join(self.root, "dev")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(os.path.join(self.sys, "class", "drm"))
        os.makedirs(self.dev)
        os.makedirs(self.proc)
        sleeper = mock.patch.object(ddc, "_sleep", lambda s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    # --- fixture builders ---
    def add_dev_buses(self, *numbers):
        for number in numbers:
            open(os.path.join(self.dev, f"i2c-{number}"), "wb").close()

    def make_pi(self):
        os.makedirs(os.path.join(self.proc, "device-tree"))
        with open(os.path.join(self.proc, "device-tree", "model"), "wb") as handle:
            handle.write(b"Raspberry Pi 2 Model B Rev 1.1\x00")

    def add_connector(self, name, bus=None, edid=b"", aux_child=None):
        card = name.split("-", 1)[0]
        device = os.path.join(self.sys, "devices", "pci0000:00", "0000:01:00.0", "drm", card, name)
        os.makedirs(device)
        with open(os.path.join(device, "edid"), "wb") as handle:
            handle.write(edid)
        if bus is not None:
            adapter = os.path.join(self.sys, "devices", "pci0000:00", "0000:01:00.0", f"i2c-{bus}")
            os.makedirs(adapter, exist_ok=True)
            os.symlink(os.path.relpath(adapter, device), os.path.join(device, "ddc"))
        if aux_child is not None:
            os.makedirs(os.path.join(device, f"i2c-{aux_child}"))
        os.symlink(device, os.path.join(self.sys, "class", "drm", name))

    def add_drm_noise(self):
        os.makedirs(os.path.join(self.sys, "class", "drm", "card0"))
        os.makedirs(os.path.join(self.sys, "class", "drm", "renderD128"))
        open(os.path.join(self.sys, "class", "drm", "version"), "w").close()

    def discover(self, config_bus=None):
        return ddc.discover_buses(config_bus=config_bus, sysfs_root=self.sys, dev_root=self.dev,
                                  proc_root=self.proc)

    # --- tests ---
    def test_raspberry_pi_gpio_bus_verified(self):
        self.make_pi()
        self.add_dev_buses(1, 20, 21)
        fake = FakeI2C(self.dev, buses={1: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(), [DdcBus(1, "raspberry-pi-gpio", True)])
        self.assertEqual(fake.opened, [1])
        self.assertEqual(fake.log, [(1, "slave", 0x50), (1, "write", 0x50, b"\x00"), (1, "read", 0x50, 128)])
        self.assertTrue(fake.all_closed())

    def test_drm_connector_with_ddc_symlink(self):
        self.add_dev_buses(*range(0, 12))
        self.add_drm_noise()
        self.add_connector("card0-DP-1", bus=5, edid=STUDIO_EDID)
        self.add_connector("card0-HDMI-A-1", bus=6, edid=OTHER_EDID)
        self.add_connector("card0-DVI-D-1", bus=7, edid=b"")
        self.add_connector("card0-DP-2", bus=None, edid=STUDIO_EDID)  # no DDC adapter at all
        fake = FakeI2C(self.dev, buses={n: STUDIO_EDID for n in range(12)}).install(self)
        self.assertEqual(self.discover(), [DdcBus(5, "drm:card0-DP-1", True)])
        self.assertEqual(fake.opened, [])  # sysfs EDID only: no I2C device is touched

    def test_drm_dp_aux_child_adapter_when_no_ddc_symlink(self):
        self.add_connector("card1-DP-3", aux_child=9, edid=STUDIO_EDID)
        # Both a ddc symlink and an AUX child: AUX first (active DP-to-VGA), then the ddc bus.
        self.add_connector("card1-DP-4", bus=4, aux_child=12, edid=STUDIO_EDID)
        fake = FakeI2C(self.dev).install(self)
        self.assertEqual(self.discover(), [DdcBus(9, "drm:card1-DP-3", True),
                                           DdcBus(12, "drm:card1-DP-4:aux", True),
                                           DdcBus(4, "drm:card1-DP-4", True)])
        self.assertEqual(fake.opened, [])

    def test_bus_path(self):
        self.assertEqual(DdcBus(4, "config", False).path, "/dev/i2c-4")

    def test_config_override_only(self):
        self.make_pi()
        self.add_dev_buses(1, 7)
        self.add_connector("card0-DP-1", bus=5, edid=STUDIO_EDID)
        fake = FakeI2C(self.dev, buses={1: STUDIO_EDID, 7: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(config_bus=7), [DdcBus(7, "config", True)])
        self.assertEqual(fake.opened, [7])

    def test_config_override_unverified(self):
        self.add_dev_buses(7)
        fake = FakeI2C(self.dev, buses={7: errno.ENXIO}).install(self)
        self.assertEqual(self.discover(config_bus=7), [DdcBus(7, "config", False)])
        self.assertEqual(fake.opened, [7])
        self.assertTrue(fake.all_closed())

    def test_config_override_missing_device(self):
        fake = FakeI2C(self.dev, open_errors={3: errno.ENOENT}).install(self)
        self.assertEqual(self.discover(config_bus=3), [DdcBus(3, "config", False)])
        self.assertEqual(fake.opened, [3])

    def test_invalid_config_bus(self):
        for bad in (-1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.discover(config_bus=bad)

    def test_ordering_pi_first_then_drm_in_natural_order(self):
        self.make_pi()
        self.add_dev_buses(1, 3, 13, 20)
        self.add_connector("card1-HDMI-A-10", bus=13, edid=STUDIO_EDID)
        self.add_connector("card1-HDMI-A-2", bus=20, edid=STUDIO_EDID)
        self.add_connector("card1-HDMI-A-1", bus=1, edid=STUDIO_EDID)  # same bus as the Pi: not repeated
        fake = FakeI2C(self.dev, buses={1: STUDIO_EDID, 3: STUDIO_EDID, 13: STUDIO_EDID, 20: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(), [
            DdcBus(1, "raspberry-pi-gpio", True),
            DdcBus(20, "drm:card1-HDMI-A-2", True),
            DdcBus(13, "drm:card1-HDMI-A-10", True),
        ])
        self.assertEqual(fake.opened, [1])

    def test_pi_unverified_fallback(self):
        self.make_pi()
        self.add_dev_buses(1, 2, 20)
        self.add_connector("card0-HDMI-A-1", bus=20, edid=OTHER_EDID)
        fake = FakeI2C(self.dev, buses={1: errno.ENXIO, 2: STUDIO_EDID, 20: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(), [DdcBus(1, "raspberry-pi-gpio", False)])
        self.assertEqual(fake.opened, [1])

    def test_pi_bus_with_other_monitor_is_unverified_fallback(self):
        self.make_pi()
        self.add_dev_buses(1)
        fake = FakeI2C(self.dev, buses={1: OTHER_EDID}).install(self)
        self.assertEqual(self.discover(), [DdcBus(1, "raspberry-pi-gpio", False)])
        self.assertEqual(fake.opened, [1])

    def test_pi_with_drm_verified_has_no_unverified_fallback(self):
        self.make_pi()
        self.add_dev_buses(1, 20)
        self.add_connector("card1-HDMI-A-1", bus=20, edid=STUDIO_EDID)
        FakeI2C(self.dev, buses={1: errno.ENXIO}).install(self)
        self.assertEqual(self.discover(), [DdcBus(20, "drm:card1-HDMI-A-1", True)])

    def test_pi_without_i2c_enabled(self):
        self.make_pi()
        self.add_dev_buses(20)
        fake = FakeI2C(self.dev, buses={20: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(), [])
        self.assertEqual(fake.opened, [])

    def test_desktop_without_apple_edid_touches_no_bus(self):
        self.add_dev_buses(*range(0, 16))  # SMBus, GPU buses, ... with "EDIDs" everywhere
        self.add_drm_noise()
        self.add_connector("card0-DP-1", bus=4, edid=OTHER_EDID)
        self.add_connector("card0-HDMI-A-1", bus=5, edid=b"")
        fake = FakeI2C(self.dev, buses={n: STUDIO_EDID for n in range(16)}).install(self)
        self.assertEqual(self.discover(), [])
        self.assertEqual(fake.opened, [])

    def test_non_pi_bus_one_is_not_probed(self):
        self.add_dev_buses(0, 1, 2)
        with open(os.path.join(self.proc, "cpuinfo"), "w") as handle:
            handle.write("model name : AMD Ryzen 5 4600G\n")
        fake = FakeI2C(self.dev, buses={1: STUDIO_EDID}).install(self)
        self.assertEqual(self.discover(), [])
        self.assertEqual(fake.opened, [])


class ReadEdidTest(unittest.TestCase):
    def test_reads_128_bytes_from_0x50(self):
        fake = FakeI2C("/dev", buses={2: STUDIO_EDID}).install(self)
        self.assertEqual(ddc.read_edid(2), STUDIO_EDID[:128])
        self.assertEqual(fake.log, [(2, "slave", 0x50), (2, "write", 0x50, b"\x00"), (2, "read", 0x50, 128)])
        self.assertTrue(fake.all_closed())

    def test_short_read(self):
        FakeI2C("/dev", buses={2: STUDIO_EDID[:64]}).install(self)
        with self.assertRaises(DdcError) as ctx:
            ddc.read_edid(2)
        self.assertEqual(ctx.exception.errno, errno.EIO)


class WriteDdcTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(ddc, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_power_on_writes_packet_twice_with_gap(self):
        fake = FakeI2C("/dev", buses={3: STUDIO_EDID}).install(self)
        ddc.set_power(3, True)
        self.assertEqual(fake.opened, [3])
        self.assertEqual(fake.log, [(3, "slave", 0x37),
                                    (3, "write", 0x37, POWER_ON_PACKET),
                                    (3, "write", 0x37, POWER_ON_PACKET)])
        self.assertEqual(POWER_ON_PACKET, bytes.fromhex("51 84 03 D6 00 01 6F"))
        self.assertEqual(self.sleeps, [0.05])
        self.assertTrue(fake.all_closed())

    def test_set_power_off(self):
        fake = FakeI2C("/dev", buses={1: STUDIO_EDID}).install(self)
        ddc.set_power(1, False)
        self.assertEqual([entry[3] for entry in fake.log if entry[1] == "write"], [POWER_OFF_PACKET] * 2)
        self.assertEqual(POWER_OFF_PACKET, bytes.fromhex("51 84 03 D6 00 04 6A"))

    def test_custom_repeats_and_gap(self):
        fake = FakeI2C("/dev", buses={1: STUDIO_EDID}).install(self)
        ddc.write_ddc(1, POWER_ON_PACKET, repeats=3, gap_s=0.1)
        self.assertEqual(len([e for e in fake.log if e[1] == "write"]), 3)
        self.assertEqual(self.sleeps, [0.1, 0.1])

    def test_dev_root(self):
        fake = FakeI2C("/tmp/fake-dev", buses={1: STUDIO_EDID}).install(self)
        ddc.write_ddc(1, POWER_ON_PACKET, repeats=1, dev_root="/tmp/fake-dev")
        self.assertEqual(fake.opened, [1])
        self.assertEqual(self.sleeps, [])

    def test_short_write_is_an_error(self):
        fake = FakeI2C("/dev", buses={1: STUDIO_EDID}, short_write=True).install(self)
        with self.assertRaises(DdcError) as ctx:
            ddc.write_ddc(1, POWER_ON_PACKET)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertTrue(fake.all_closed())

    def test_invalid_arguments(self):
        FakeI2C("/dev", buses={1: STUDIO_EDID}).install(self)
        with self.assertRaises(ValueError):
            ddc.write_ddc(1, b"")
        with self.assertRaises(ValueError):
            ddc.write_ddc(1, POWER_ON_PACKET, repeats=0)
        with self.assertRaises(ValueError):
            ddc.write_ddc(-1, POWER_ON_PACKET)

    def assert_mapped(self, fake, err, *needles):
        fake.install(self)
        with self.assertRaises(DdcError) as ctx:
            ddc.set_power(4, True)
        exc = ctx.exception
        self.assertIsInstance(exc, OSError)
        self.assertEqual(exc.errno, err)
        for needle in needles:
            self.assertIn(needle, str(exc))
        self.assertTrue(fake.all_closed())
        return exc

    def test_errno_eacces(self):
        self.assert_mapped(FakeI2C("/dev", open_errors={4: errno.EACCES}), errno.EACCES,
                           "no permission for /dev/i2c-4", "udev", "i2c group", "adc-crt doctor")

    def test_errno_eperm(self):
        self.assert_mapped(FakeI2C("/dev", open_errors={4: errno.EPERM}), errno.EPERM, "no permission")

    def test_errno_enoent(self):
        self.assert_mapped(FakeI2C("/dev", open_errors={4: errno.ENOENT}), errno.ENOENT,
                           "/dev/i2c-4 does not exist", "i2c-dev", "raspi-config nonint do_i2c 0")

    def test_errno_enxio(self):
        self.assert_mapped(FakeI2C("/dev", buses={4: errno.ENXIO}), errno.ENXIO,
                           "no ACK", "0x37", "pass DDC", "wiring")

    def test_errno_eremoteio(self):
        self.assert_mapped(FakeI2C("/dev", buses={4: EREMOTEIO}), EREMOTEIO, "no ACK", "0x37")

    def test_errno_eio(self):
        self.assert_mapped(FakeI2C("/dev", buses={4: errno.EIO}), errno.EIO, "no ACK")

    def test_errno_ebusy(self):
        fake = FakeI2C("/dev", buses={4: STUDIO_EDID}, ioctl_errors={4: errno.EBUSY})
        self.assert_mapped(fake, errno.EBUSY, "0x37", "kernel driver")
        self.assertFalse([e for e in fake.log if e[1] == "write"])


if __name__ == "__main__":
    unittest.main()
