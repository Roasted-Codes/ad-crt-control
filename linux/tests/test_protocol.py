"""Protocol constants vs. the Mac source of truth, packets, HID parsing, EDID identification."""

from __future__ import annotations

import os
import re
import unittest

from adc_crt import protocol
from adc_crt.protocol import (
    CONTROLS,
    POWER_OFF_PACKET,
    POWER_ON_PACKET,
    feature_write_candidates,
    is_studio_display_edid,
    parse_feature_value,
)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TESTS_DIR))  # tests -> linux -> repo root
SWIFT_CONTROLS = os.path.join(REPO_ROOT, "mac", "ADCControl", "Sources", "ADCMonitorCore", "ADCControl.swift")
EDID_FIXTURE = os.path.join(TESTS_DIR, "fixtures", "studio-display-17-crt.edid")

_CONTROL_INIT = (r'ADCControl\(\s*code:\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*label:\s*"([^"]*)"\s*,'
                 r'\s*maxValue:\s*(\d+)\s*,\s*factoryDefault:\s*(\d+)\s*\)')


def parse_swift_controls(source: str):
    """ADCControls.all as [(code, label, max, default)], resolving named `static let` entries."""
    named = {
        m.group(1): m.groups()[1:]
        for m in re.finditer(r"static\s+let\s+(\w+)\s*=\s*" + _CONTROL_INIT, source)
    }
    block = re.search(r"static\s+let\s+all\s*:\s*\[ADCControl\]\s*=\s*\[(.*?)\n\s*\]", source, re.S)
    if block is None:
        raise AssertionError("ADCControls.all not found in ADCControl.swift")
    entries = []
    for m in re.finditer(_CONTROL_INIT + r"|\b([A-Za-z_]\w*)\b(?=\s*,|\s*$)", block.group(1)):
        if m.group(5):
            if m.group(5) not in named:
                raise AssertionError(f"unresolved control reference {m.group(5)!r}")
            fields = named[m.group(5)]
        else:
            fields = m.groups()[:4]
        code, label, max_value, default = fields
        entries.append((int(code, 0), label, int(max_value), int(default)))
    return entries


class SwiftParityTest(unittest.TestCase):
    def test_controls_match_mac_source_exactly(self):
        with open(SWIFT_CONTROLS, encoding="utf-8") as handle:
            swift = parse_swift_controls(handle.read())
        python = [(c.code, c.label, c.max_value, c.factory_default) for c in CONTROLS]
        self.assertEqual(len(swift), 11)
        self.assertEqual(python, swift)  # code, label, max, default *and* order

    def test_controls_by_code_is_consistent(self):
        self.assertEqual(list(protocol.CONTROLS_BY_CODE), [c.code for c in CONTROLS])


class PacketTest(unittest.TestCase):
    def test_power_packets(self):
        self.assertEqual(POWER_ON_PACKET, bytes.fromhex("51 84 03 D6 00 01 6F"))
        self.assertEqual(POWER_OFF_PACKET, bytes.fromhex("51 84 03 D6 00 04 6A"))

    def test_checksum_is_xor_with_destination(self):
        packet = protocol.ddc_set_vcp_packet(0x10, 0x1234)
        check = 0x6E
        for byte in packet[:-1]:
            check ^= byte
        self.assertEqual(packet[-1], check)
        self.assertEqual(packet[:6], bytes([0x51, 0x84, 0x03, 0x10, 0x12, 0x34]))


class FeatureReportTest(unittest.TestCase):
    def test_parse_feature_value(self):
        cases = [
            ((0x12, [95, 0]), 95),
            ((0x12, [0x12, 95, 0]), 95),
            ((0xAE, [0x70, 0x17]), 6000),
            ((0xAE, [0xAE, 0x70, 0x17]), 6000),
            ((0x12, []), None),
            ((0x12, b""), None),
        ]
        for (code, raw), expected in cases:
            with self.subTest(code=hex(code), raw=raw):
                self.assertEqual(parse_feature_value(code, raw), expected)

    def test_feature_write_candidates(self):
        self.assertEqual(feature_write_candidates(0x12, [95, 0]), [b"\x12\x5f\x00", b"\x12\x5f"])
        self.assertEqual(feature_write_candidates(0x01, [1]), [b"\x01\x01"])


class EdidTest(unittest.TestCase):
    def setUp(self):
        with open(EDID_FIXTURE, "rb") as handle:
            self.edid = handle.read()

    def test_fixture_is_recognized(self):
        self.assertTrue(is_studio_display_edid(self.edid))
        self.assertTrue(is_studio_display_edid(self.edid[:128]))

    def test_corrupted_fixture_is_rejected(self):
        for index, value in ((0, 0x01), (8, 0x4C), (10, 0x14), (11, 0x93)):
            corrupted = bytearray(self.edid)
            corrupted[index] = value
            with self.subTest(byte=index):
                self.assertFalse(is_studio_display_edid(bytes(corrupted)))
        self.assertFalse(is_studio_display_edid(self.edid[:127]))
        self.assertFalse(is_studio_display_edid(b""))

    def test_bad_checksum_is_still_recognized(self):
        corrupted = bytearray(self.edid[:128])
        corrupted[127] ^= 0xFF
        self.assertTrue(is_studio_display_edid(bytes(corrupted)))


if __name__ == "__main__":
    unittest.main()
