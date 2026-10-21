"""Packaging checks: the old-labwc-keybind cleanup script, the udev rule, the systemd unit, versions.

No hardware and no root needed. The cleanup script runs in a subprocess against files in a
temporary directory.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

LINUX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEANUP = os.path.join(LINUX_DIR, "data", "libexec", "cleanup-old-labwc-keybind")
UDEV_RULES = os.path.join(LINUX_DIR, "data", "udev", "70-adc-crt-control.rules")
UNIT = os.path.join(LINUX_DIR, "data", "systemd", "adc-crt-poweron.service")
BACKUP_SUFFIX = ".adc-crt-control.bak"

# What pi-adc-control 2.1 left in /etc/xdg/labwc/rc.xml: its postinst `sed` insertion with the
# line its prerm `sed '/crt-toggle/d'` deleted (derived by running both; see test_real_old_sed).
LEFTOVER = b'  <keyboard>\n    <keybind key="C-A-p">\n    </keybind>\n  </keyboard>\n'

# pi-adc-control 2.x maintainer-script commands, verbatim (2.0 used C-A-F12 instead of C-A-p).
OLD_POSTINST_SED = ('s|</openbox_config>|  <keyboard>\\n    <keybind key="%s">\\n      <action name="Execute">'
                    '<command>sudo /usr/bin/crt-toggle</command></action>\\n    </keybind>\\n  </keyboard>\\n'
                    '</openbox_config>|')
OLD_PRERM_SEDS = ("/crt-toggle/d", "/Super_L-m/d")
# pi-adc-control 2.x 99-crt-control.xml, copied to rc.xml when there was none.
OLD_TEMPLATE = ('<?xml version="1.0"?>\n<!-- Super+M toggles Apple Studio Display power -->\n'
                '<openbox_config xmlns="http://openbox.org/3.4/rc">\n  <keyboard>\n    <keybind key="%s">\n'
                '      <action name="Execute">\n        <command>sudo /usr/bin/crt-toggle</command>\n'
                '      </action>\n    </keybind>\n  </keyboard>\n</openbox_config>\n')

ORIGINAL_RC = (b'<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n'
               b'  <theme>\n    <name>PiXflat</name>\n  </theme>\n</openbox_config>\n')


def _is_gnu_sed() -> bool:
    if not shutil.which("sed"):
        return False
    try:
        out = subprocess.run(["sed", "--version"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return False
    return "GNU sed" in out


class CleanupOldLabwcKeybindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="adc-crt-pkg-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.rc = os.path.join(self.tmp, "rc.xml")
        self.backup = self.rc + BACKUP_SUFFIX

    def write(self, data: bytes, mode: int = 0o644) -> None:
        with open(self.rc, "wb") as handle:
            handle.write(data)
        os.chmod(self.rc, mode)

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def run_cleanup(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", "-I", CLEANUP, *args],
                              capture_output=True, check=False)

    def assert_cleaned(self, before: bytes, expected: bytes) -> None:
        result = self.run_cleanup(self.rc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(self.rc), expected)
        self.assertEqual(self.read(self.backup), before)

    def test_exact_leftover_is_removed_and_backed_up(self) -> None:
        before = ORIGINAL_RC.replace(b"</openbox_config>", LEFTOVER + b"</openbox_config>")
        self.write(before)
        self.assert_cleaned(before, ORIGINAL_RC)

    def test_only_the_block_is_removed_from_surrounding_content(self) -> None:
        head = (b'<?xml version="1.0"?>\n<!-- my config -->\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n'
                b'  <keyboard>\n    <keybind key="W-Return">\n'
                b'      <action name="Execute" command="lxterminal" />\n    </keybind>\n  </keyboard>\n')
        tail = b"</openbox_config>\n<!-- trailing comment -->\n"
        before = head + LEFTOVER + tail
        self.write(before, mode=0o640)
        self.assert_cleaned(before, head + tail)
        self.assertEqual(stat.S_IMODE(os.stat(self.rc).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.backup).st_mode), 0o640)
        self.assertEqual(os.listdir(self.tmp).__len__(), 2, os.listdir(self.tmp))  # no temp files left

    def test_stacked_leftovers_from_repeated_install_remove_cycles(self) -> None:
        before = ORIGINAL_RC.replace(b"</openbox_config>", LEFTOVER + LEFTOVER + b"</openbox_config>")
        self.write(before)
        self.assert_cleaned(before, ORIGINAL_RC)

    def test_file_without_leftover_is_untouched(self) -> None:
        variants = [
            ORIGINAL_RC,
            # A live binding (the block still has its action): not a leftover.
            ORIGINAL_RC.replace(b"</openbox_config>",
                                b'  <keyboard>\n    <keybind key="C-A-p">\n      <action name="Execute" '
                                b'command="adc-crt power toggle" />\n    </keybind>\n  </keyboard>\n</openbox_config>'),
            # Same block, but not directly before </openbox_config>.
            ORIGINAL_RC.replace(b"  <theme>", LEFTOVER + b"  <theme>"),
            # Same block with different indentation (someone reformatted the file).
            ORIGINAL_RC.replace(b"</openbox_config>", LEFTOVER.replace(b"  ", b"\t") + b"</openbox_config>"),
            # The root element labwc also accepts; the old sed never matched it.
            ORIGINAL_RC.replace(b"openbox_config", b"labwc_config").replace(
                b"</labwc_config>", LEFTOVER + b"</labwc_config>"),
        ]
        for data in variants:
            with self.subTest(data=data):
                self.write(data)
                stamp = os.stat(self.rc)
                result = self.run_cleanup(self.rc)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.read(self.rc), data)
                self.assertEqual(os.stat(self.rc).st_ino, stamp.st_ino)
                self.assertEqual(os.stat(self.rc).st_mtime_ns, stamp.st_mtime_ns)
                self.assertFalse(os.path.exists(self.backup))
                self.assertEqual(result.stdout, b"")

    def test_missing_file_is_fine(self) -> None:
        result = self.run_cleanup(self.rc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.rc))
        self.assertFalse(os.path.exists(self.backup))

    def test_symlinked_rc_xml_stays_a_symlink(self) -> None:
        real = os.path.join(self.tmp, "real-rc.xml")
        before = ORIGINAL_RC.replace(b"</openbox_config>", LEFTOVER + b"</openbox_config>")
        with open(real, "wb") as handle:
            handle.write(before)
        os.symlink(real, self.rc)
        self.assert_cleaned(before, ORIGINAL_RC)
        self.assertTrue(os.path.islink(self.rc))
        self.assertEqual(self.read(real), ORIGINAL_RC)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.run_cleanup(self.rc, self.rc).returncode, 2)
        self.assertEqual(self.run_cleanup("--bogus").returncode, 2)

    @unittest.skipUnless(_is_gnu_sed(), "needs GNU sed to replay the old maintainer scripts")
    def test_real_old_sed_install_then_remove(self) -> None:
        """Replay pi-adc-control 2.x postinst + prerm with GNU sed; cleanup must restore the original."""
        for key in ("C-A-p", "C-A-F12"):
            with self.subTest(key=key):
                self.write(ORIGINAL_RC)
                subprocess.run(["sed", "-i", OLD_POSTINST_SED % key, self.rc], check=True)
                self.assertIn(b"crt-toggle", self.read(self.rc))
                for expr in OLD_PRERM_SEDS:
                    subprocess.run(["sed", "-i", expr, self.rc], check=True)
                leftover = self.read(self.rc)
                if key == "C-A-p":
                    self.assertEqual(leftover, ORIGINAL_RC.replace(b"</openbox_config>",
                                                                   LEFTOVER + b"</openbox_config>"))
                self.assert_cleaned(leftover, ORIGINAL_RC)
                os.unlink(self.backup)

    @unittest.skipUnless(_is_gnu_sed(), "needs GNU sed to replay the old maintainer scripts")
    def test_real_upgrade_chain_2_0_to_2_1_then_remove(self) -> None:
        """2.x prerm also ran on upgrade, so 2.0 -> 2.1 -> removal stacks a C-A-F12 and a C-A-p leftover."""
        self.write(ORIGINAL_RC)
        for key in ("C-A-F12", "C-A-p"):  # 2.0 postinst, then 2.0 prerm (upgrade), 2.1 postinst, 2.1 prerm
            subprocess.run(["sed", "-i", OLD_POSTINST_SED % key, self.rc], check=True)
            for expr in OLD_PRERM_SEDS:
                subprocess.run(["sed", "-i", expr, self.rc], check=True)
        leftover = self.read(self.rc)
        self.assertEqual(leftover.count(b"<keybind "), 2)
        self.assert_cleaned(leftover, ORIGINAL_RC)

    @unittest.skipUnless(_is_gnu_sed(), "needs GNU sed to replay the old maintainer scripts")
    def test_real_old_template_then_remove(self) -> None:
        """Old postinst copied 99-crt-control.xml when rc.xml was missing; prerm then ran the same sed."""
        for key in ("C-A-p", "C-A-F12"):
            with self.subTest(key=key):
                template = (OLD_TEMPLATE % key).encode()
                self.write(template)
                for expr in OLD_PRERM_SEDS:
                    subprocess.run(["sed", "-i", expr, self.rc], check=True)
                leftover = self.read(self.rc)
                self.assert_cleaned(leftover, template.split(b"  <keyboard>\n")[0] + b"</openbox_config>\n")
                os.unlink(self.backup)


class DataFilesTest(unittest.TestCase):
    @staticmethod
    def _rule_lines(path: str) -> list:
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]

    def test_udev_rules(self) -> None:
        required = [
            'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="9213", TAG+="uaccess"',
            'SUBSYSTEM=="usbmisc", KERNEL=="hiddev*", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="9213", '
            'TAG+="uaccess"',
            'SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[0-9]*", ATTRS{class}=="0x030000", TAG+="uaccess"',
            'SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[0-9]*", ATTRS{class}=="0x038000", TAG+="uaccess"',
        ]
        self.assertEqual(self._rule_lines(UDEV_RULES), required)
        # uaccess is acted on by 73-seat-late.rules, so this file must sort before it.
        self.assertLess(os.path.basename(UDEV_RULES), "73-seat-late.rules")

    def test_systemd_unit(self) -> None:
        lines = self._rule_lines(UNIT)
        self.assertIn("ExecStart=/usr/bin/adc-crt poweron --wait 30", lines)
        self.assertIn("Type=oneshot", lines)
        self.assertIn("After=systemd-modules-load.service systemd-udevd.service", lines)
        self.assertIn("WantedBy=multi-user.target", lines)

    def test_modules_load(self) -> None:
        self.assertEqual(self._rule_lines(os.path.join(LINUX_DIR, "data", "modules-load", "adc-crt-control.conf")),
                         ["i2c-dev"])

    def test_desktop_entries(self) -> None:
        app = self._rule_lines(os.path.join(LINUX_DIR, "data", "applications", "adc-crt-control.desktop"))
        self.assertIn("Exec=adc-crt gui", app)
        auto = self._rule_lines(os.path.join(LINUX_DIR, "data", "autostart", "adc-crt-buttons.desktop"))
        self.assertIn("Exec=adc-crt buttons", auto)
        self.assertIn("NoDisplay=true", auto)

    def test_versions_agree(self) -> None:
        def grab(rel: str, pattern: str) -> str:
            with open(os.path.join(LINUX_DIR, rel), encoding="utf-8") as handle:
                match = re.search(pattern, handle.read(), re.M)
            self.assertIsNotNone(match, f"no version in {rel}")
            return match.group(1)

        version = grab("Makefile", r"^VERSION\s*:?=\s*(\S+)")
        self.assertEqual(grab("packaging/debian/control", r"^Version:\s*(\S+)"), version)
        self.assertEqual(grab("packaging/arch/PKGBUILD", r"^pkgver=(\S+)"), version)
        self.assertEqual(grab("adc_crt/__init__.py", r'^__version__\s*=\s*"([^"]+)"'), version)

    def test_maintainer_scripts_are_posix_sh(self) -> None:
        for name in ("postinst", "prerm", "postrm"):
            path = os.path.join(LINUX_DIR, "packaging", "debian", name)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.readline(), "#!/bin/sh\n", name)
            if shutil.which("sh"):
                result = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
