"""hidraw/hiddev discovery, report-descriptor parsing and feature I/O against fake sysfs/ioctl."""

from __future__ import annotations

import errno
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from adc_crt import hid
from adc_crt.hid import HIDIOCGFEATURE, HIDIOCSFEATURE, HidError, HidrawDevice

# Usage Page 0x80 (Monitor), Usage 0x01, Collection (Application) ... End Collection
MONITOR_DESCRIPTOR = bytes.fromhex(
    "05 80 09 01 A1 01 85 12 15 00 26 FF 00 75 08 95 01 09 10 B1 02 C0")
# A consumer-control style interface on the same USB device
CONSUMER_DESCRIPTOR = bytes.fromhex("05 0C 09 01 A1 01 85 01 09 E9 81 02 C0")
KEYBOARD_DESCRIPTOR = bytes.fromhex("05 01 09 06 A1 01 05 07 19 E0 29 E7 81 02 C0")


class IoctlNumberTest(unittest.TestCase):
    def test_numbers_match_linux_headers(self):
        self.assertEqual(HIDIOCGFEATURE(132), 0xC0844807)
        self.assertEqual(HIDIOCSFEATURE(3), 0xC0034806)

    def test_size_out_of_range(self):
        with self.assertRaises(ValueError):
            HIDIOCGFEATURE(1 << 14)


class DescriptorParserTest(unittest.TestCase):
    def check(self, hex_text, expected):
        self.assertEqual(hid.is_monitor_descriptor(bytes.fromhex(hex_text)), expected)

    def test_one_byte_items(self):
        self.check("05 80 09 01 A1 01 C0", True)

    def test_two_byte_usage_page(self):
        self.check("06 80 00 09 01 A1 01 C0", True)

    def test_four_byte_usage_page(self):
        self.check("07 80 00 00 00 09 01 A1 01 C0", True)

    def test_two_byte_usage(self):
        self.check("05 80 0A 01 00 A1 01 C0", True)

    def test_extended_four_byte_usage_carries_its_page(self):
        self.check("05 01 0B 01 00 80 00 A1 01 C0", True)    # page 0x80 in the high 16 bits
        self.check("05 80 0B 01 00 01 00 A1 01 C0", False)   # Generic Desktop / 1 despite page 0x80

    def test_size_code_three_means_four_bytes(self):
        # Logical Maximum with 4 data bytes; mis-sizing it would desynchronise the parse.
        self.check("27 FF FF 00 00 05 80 09 01 A1 01 C0", True)

    def test_long_items_are_skipped(self):
        self.check("FE 03 10 05 80 09 05 80 09 01 A1 01 C0", True)
        self.check("FE 04 10 05 80 09 01 A1 01", False)  # all of it is long-item data

    def test_wrong_usage_or_page(self):
        self.check("05 80 09 02 A1 01 C0", False)
        self.check("05 81 09 01 A1 01 C0", False)

    def test_non_application_collection(self):
        self.check("05 80 09 01 A1 00 C0", False)  # Physical
        self.check("05 80 09 01 A1 02 C0", False)  # Logical

    def test_nested_application_is_not_top_level(self):
        self.check("05 01 09 02 A1 01 05 80 09 01 A1 01 C0 C0", False)

    def test_second_top_level_collection(self):
        descriptor = CONSUMER_DESCRIPTOR + MONITOR_DESCRIPTOR
        self.assertEqual(hid.application_collections(descriptor), [(0x0C, 0x01), (0x80, 0x01)])
        self.assertTrue(hid.is_monitor_descriptor(descriptor))

    def test_usage_is_consumed_by_main_items(self):
        # The Usage before an Input item must not be reused by the following collection.
        self.check("05 80 09 01 81 02 A1 01 C0", False)

    def test_push_pop(self):
        self.check("05 80 A4 05 01 B4 09 01 A1 01 C0", True)

    def test_truncated_and_empty(self):
        self.check("", False)
        self.check("05", False)
        self.check("05 80 09 01 A1", False)
        self.check("FE", False)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())


class FindHidrawTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.sys = os.path.join(self.root, "sys")
        self.dev = os.path.join(self.root, "dev")
        os.makedirs(os.path.join(self.sys, "class", "hidraw"))
        os.makedirs(self.dev)

    def add_node(self, number, hid_id, descriptor, name="Apple Computer Inc. Studio Display"):
        """Realistic layout: class/hidraw/hidrawN -> devices/.../<hid dev>/hidraw/hidrawN,
        whose device symlink points back up at the HID device directory."""
        hid_dev = os.path.join(self.sys, "devices", "usb1", "1-1", f"1-1:1.{number}",
                               f"0003:05AC:9213.{number:04X}")
        node_dir = os.path.join(hid_dev, "hidraw", f"hidraw{number}")
        os.makedirs(node_dir)
        _write(os.path.join(hid_dev, "uevent"), f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME={name}\n")
        if descriptor is not None:
            _write(os.path.join(hid_dev, "report_descriptor"), descriptor)
        os.symlink("../..", os.path.join(node_dir, "device"))
        os.symlink(node_dir, os.path.join(self.sys, "class", "hidraw", f"hidraw{number}"))

    def test_monitor_interface_first_and_filtering(self):
        self.add_node(1, "0003:000005AC:00009213", CONSUMER_DESCRIPTOR)
        self.add_node(2, "0003:0000046D:0000C52B", KEYBOARD_DESCRIPTOR, name="Logitech USB Receiver")
        self.add_node(3, "0003:000005ac:00009213", MONITOR_DESCRIPTOR)  # lower-case hex
        self.add_node(11, "0003:000005AC:00009213", None)               # unreadable descriptor
        self.add_node(12, "0005:000005AC:00009213", MONITOR_DESCRIPTOR) # Bluetooth bus: not ours

        found = hid.find_hidraw(sysfs_root=self.sys, dev_root=self.dev)

        self.assertEqual([info.path for info in found],
                         [os.path.join(self.dev, n) for n in ("hidraw3", "hidraw1", "hidraw11")])
        self.assertEqual([info.is_monitor_interface for info in found], [True, False, False])
        self.assertEqual(found[0].sysfs, os.path.join(self.sys, "class", "hidraw", "hidraw3"))
        self.assertEqual(found[0].name, "Apple Computer Inc. Studio Display")

    def test_missing_class_dir(self):
        self.assertEqual(hid.find_hidraw(sysfs_root=os.path.join(self.root, "nope"), dev_root=self.dev), [])

    def test_parse_hid_id(self):
        self.assertEqual(hid.parse_hid_id("0003:000005AC:00009213"), (3, 0x05AC, 0x9213))
        self.assertIsNone(hid.parse_hid_id("garbage"))
        self.assertIsNone(hid.parse_hid_id(""))


class FakeHidraw:
    """Stands in for os.open/os.close/fcntl.ioctl on a hidraw node."""

    def __init__(self, reports=None, error=None):
        self.reports = reports or {}
        self.error = error
        self.calls = []
        self.closed = []

    def open(self, path, flags):
        self.assert_rdwr(flags)
        self.calls.append(("open", path))
        return 42

    @staticmethod
    def assert_rdwr(flags):
        assert flags & os.O_ACCMODE == os.O_RDWR, flags

    def close(self, fd):
        self.closed.append(fd)

    def ioctl(self, fd, request, buf, mutate):
        assert fd == 42 and mutate is True and isinstance(buf, bytearray)
        self.calls.append(("ioctl", request, bytes(buf)))
        if self.error is not None:
            raise OSError(self.error, os.strerror(self.error))
        if request == HIDIOCGFEATURE(len(buf)):
            reply = self.reports[buf[0]]
            buf[:len(reply)] = reply
            return len(reply)
        return len(buf)

    def patches(self):
        return [mock.patch.object(hid, "_os_open", self.open), mock.patch.object(hid, "_os_close", self.close),
                mock.patch.object(hid, "_ioctl", self.ioctl)]


class HidrawDeviceTest(unittest.TestCase):
    def use(self, fake):
        for patcher in fake.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake

    def test_get_feature(self):
        fake = self.use(FakeHidraw(reports={0x12: b"\x12\x5f\x00"}))
        with HidrawDevice("/dev/hidraw3") as device:
            self.assertEqual(device.get_feature(0x12), b"\x12\x5f\x00")
        request, sent = fake.calls[1][1], fake.calls[1][2]
        self.assertEqual(request, 0xC0844807)
        self.assertEqual(len(sent), 132)
        self.assertEqual(sent[0], 0x12)
        self.assertEqual(fake.closed, [42])

    def test_get_feature_custom_length(self):
        fake = self.use(FakeHidraw(reports={0xAE: b"\xae\x70\x17"}))
        with HidrawDevice("/dev/hidraw3") as device:
            self.assertEqual(device.get_feature(0xAE, length=8), b"\xae\x70\x17")
        self.assertEqual(fake.calls[1][1], HIDIOCGFEATURE(8))

    def test_set_feature(self):
        fake = self.use(FakeHidraw())
        with HidrawDevice("/dev/hidraw3") as device:
            self.assertEqual(device.set_feature(b"\x12\x5f\x00"), 3)
        self.assertEqual(fake.calls[1], ("ioctl", 0xC0034806, b"\x12\x5f\x00"))

    def test_ioctl_error_becomes_hid_error(self):
        self.use(FakeHidraw(error=errno.EPIPE))
        with HidrawDevice("/dev/hidraw3") as device:
            with self.assertRaises(HidError) as ctx:
                device.set_feature(b"\x12\x5f")
        self.assertEqual(ctx.exception.errno, errno.EPIPE)
        self.assertIn("0x12", str(ctx.exception))

    def test_open_permission_error_is_actionable(self):
        def denied(path, flags):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        with mock.patch.object(hid, "_os_open", denied):
            with self.assertRaises(HidError) as ctx:
                HidrawDevice("/dev/hidraw7")
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.errno, errno.EACCES)
        self.assertIn("no permission for /dev/hidraw7", message)
        self.assertIn("udev rule", message)
        self.assertIn("adc-crt doctor", message)
        self.assertIsInstance(ctx.exception, OSError)

    def test_closed_device_and_bad_arguments(self):
        self.use(FakeHidraw())
        device = HidrawDevice("/dev/hidraw3")
        with self.assertRaises(ValueError):
            device.get_feature(0x100)
        with self.assertRaises(ValueError):
            device.set_feature(b"")
        device.close()
        device.close()  # idempotent
        with self.assertRaises(HidError):
            device.get_feature(0x12)


class FindHiddevTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.sys = os.path.join(self.root, "sys")
        self.dev = os.path.join(self.root, "dev")
        os.makedirs(os.path.join(self.sys, "class", "usbmisc"))
        os.makedirs(os.path.join(self.dev, "usb"))
        # The monitor is a hub (1-1, 05ac:9215) with the control device 1-1.2 (05ac:9213) and
        # an unrelated mouse 1-1.3 (046d:c077) plugged into it.
        usb = os.path.join(self.sys, "devices", "pci0000:00", "0000:00:14.0", "usb1")
        self.add_usb_device(os.path.join(usb, "1-1"), "05ac", "9215")
        self.add_usb_device(os.path.join(usb, "1-1", "1-1.3"), "046d", "c077")
        self.add_hiddev(os.path.join(usb, "1-1", "1-1.3", "1-1.3:1.0"), 0)
        self.add_usb_device(os.path.join(usb, "1-1", "1-1.2"), "05ac", "9213")
        self.add_hiddev(os.path.join(usb, "1-1", "1-1.2", "1-1.2:1.0"), 1)

    @staticmethod
    def add_usb_device(path, vendor, product):
        _write(os.path.join(path, "idVendor"), vendor + "\n")
        _write(os.path.join(path, "idProduct"), product + "\n")

    def add_hiddev(self, interface, number):
        node = os.path.join(interface, "usbmisc", f"hiddev{number}")
        os.makedirs(node)
        os.symlink("../..", os.path.join(node, "device"))
        os.symlink(os.path.relpath(node, os.path.join(self.sys, "class", "usbmisc")),
                   os.path.join(self.sys, "class", "usbmisc", f"hiddev{number}"))
        _write(os.path.join(self.dev, "usb", f"hiddev{number}"), b"")

    def test_finds_monitor_through_symlinks(self):
        self.assertEqual(hid.find_hiddev(sysfs_root=self.sys, dev_root=self.dev),
                         os.path.join(self.dev, "usb", "hiddev1"))

    def test_device_behind_monitor_hub_is_not_matched(self):
        shutil.rmtree(os.path.join(self.sys, "devices", "pci0000:00", "0000:00:14.0", "usb1", "1-1", "1-1.2"))
        self.assertIsNone(hid.find_hiddev(sysfs_root=self.sys, dev_root=self.dev))

    def test_no_usbmisc(self):
        self.assertIsNone(hid.find_hiddev(sysfs_root=os.path.join(self.root, "nope"), dev_root=self.dev))


class HiddevEventTest(unittest.TestCase):
    EVENTS = [(0x008C00E4, 1), (0x008C00E4, 0), (0x00800012, -3)]

    def raw(self):
        return b"".join(struct.pack("=Ii", h, v) for h, v in self.EVENTS)

    def test_parse(self):
        self.assertEqual(hid.parse_hiddev_events(self.raw() + b"\x01\x02"),
                         [(0x00E4, 1), (0x00E4, 0), (0x0012, -3)])

    def test_iter_handles_split_reads_and_closes(self):
        data = self.raw()
        chunks = [data[:5], data[5:19], data[19:], b""]
        closed = []
        with mock.patch.object(hid, "_os_open", lambda path, flags: 9), \
                mock.patch.object(hid, "_os_read", lambda fd, n: chunks.pop(0)), \
                mock.patch.object(hid, "_os_close", closed.append):
            events = list(hid.iter_hiddev_events("/dev/usb/hiddev1"))
        self.assertEqual(events, [(0x00E4, 1), (0x00E4, 0), (0x0012, -3)])
        self.assertEqual(closed, [9])

    def test_iter_real_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(self.raw())
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual([u for u, v in hid.iter_hiddev_events(handle.name) if v != 0], [0x00E4, 0x0012])

    def test_iter_open_error(self):
        with self.assertRaises(HidError) as ctx:
            next(hid.iter_hiddev_events("/nonexistent/hiddev9"))
        self.assertEqual(ctx.exception.errno, errno.ENOENT)


if __name__ == "__main__":
    unittest.main()
