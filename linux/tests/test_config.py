"""Config precedence (env > user file > system file) and error reporting."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from adc_crt import config
from adc_crt.config import Config, load_config


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.home = os.path.join(self.root, "home")
        self.xdg = os.path.join(self.root, "xdg")
        self.system = os.path.join(self.root, "etc", "adc-crt-control.conf")
        self.user = os.path.join(self.xdg, "adc-crt-control", "config.ini")
        self.env = {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg}

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def load(self, **env):
        environ = dict(self.env, **env)
        return load_config(environ=environ, system_path=self.system)

    def test_nothing_configured(self):
        self.assertEqual(self.load(), Config(None, None))

    def test_system_file(self):
        self.write(self.system, "[ddc]\nbus = 1\n[hid]\nhidraw = /dev/hidraw3\n")
        self.assertEqual(self.load(), Config(1, "/dev/hidraw3"))

    def test_user_overrides_system_per_setting(self):
        self.write(self.system, "[ddc]\nbus = 1\n[hid]\nhidraw = /dev/hidraw3\n")
        self.write(self.user, "[ddc]\nbus = 5\n")
        self.assertEqual(self.load(), Config(5, "/dev/hidraw3"))

    def test_env_overrides_files(self):
        self.write(self.system, "[ddc]\nbus = 1\n[hid]\nhidraw = /dev/hidraw3\n")
        self.write(self.user, "[ddc]\nbus = 5\n[hid]\nhidraw = /dev/hidraw4\n")
        self.assertEqual(self.load(ADC_CRT_I2C_BUS="9"), Config(9, "/dev/hidraw4"))
        self.assertEqual(self.load(ADC_CRT_I2C_BUS="9", ADC_CRT_HIDRAW="/dev/hidraw8"), Config(9, "/dev/hidraw8"))

    def test_empty_values_fall_through(self):
        self.write(self.system, "[ddc]\nbus = 2\n")
        self.write(self.user, "[ddc]\nbus =\n")
        self.assertEqual(self.load(ADC_CRT_I2C_BUS="", ADC_CRT_HIDRAW=" "), Config(2, None))

    def test_default_user_path_uses_home_without_xdg(self):
        env = {"HOME": self.home}
        self.write(os.path.join(self.home, ".config", "adc-crt-control", "config.ini"), "[ddc]\nbus = 4\n")
        self.assertEqual(load_config(environ=env, system_path=self.system), Config(4, None))
        self.assertEqual(config.user_config_path({"HOME": self.home, "XDG_CONFIG_HOME": "relative"}),
                         os.path.join(self.home, ".config", "adc-crt-control", "config.ini"))

    def test_uses_os_environ_by_default(self):
        with mock.patch.dict(os.environ, {"ADC_CRT_I2C_BUS": "11", "ADC_CRT_HIDRAW": "hidraw2",
                                          "XDG_CONFIG_HOME": self.xdg}):
            self.assertEqual(load_config(system_path=self.system), Config(11, "/dev/hidraw2"))

    def test_value_forms(self):
        for text, bus in (("3", 3), (" 3 ", 3), ("i2c-12", 12), ("/dev/i2c-0", 0)):
            with self.subTest(text=text):
                self.assertEqual(self.load(ADC_CRT_I2C_BUS=text).i2c_bus, bus)
        self.assertEqual(self.load(ADC_CRT_HIDRAW="hidraw6").hidraw, "/dev/hidraw6")
        self.assertEqual(self.load(ADC_CRT_HIDRAW="/dev/adc-hid").hidraw, "/dev/adc-hid")

    def test_bad_env_values_name_the_variable(self):
        for name, value in (("ADC_CRT_I2C_BUS", "abc"), ("ADC_CRT_I2C_BUS", "-1"),
                            ("ADC_CRT_I2C_BUS", "1.5"), ("ADC_CRT_HIDRAW", "dev/hidraw3")):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError) as ctx:
                    self.load(**{name: value})
                self.assertIn(name, str(ctx.exception))
                self.assertIn(repr(value), str(ctx.exception))

    def test_bad_file_values_name_the_file(self):
        self.write(self.user, "[ddc]\nbus = one\n")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn(self.user, str(ctx.exception))
        self.assertIn("[ddc] bus", str(ctx.exception))

        self.write(self.user, "[hid]\nhidraw = hidraw\n")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("[hid] hidraw", str(ctx.exception))

    def test_bad_system_file_is_reported_even_when_overridden(self):
        self.write(self.system, "[ddc]\nbus = x\n")
        with self.assertRaises(ValueError) as ctx:
            self.load(ADC_CRT_I2C_BUS="1")
        self.assertIn(self.system, str(ctx.exception))

    def test_malformed_ini_names_the_file(self):
        self.write(self.system, "bus = 1\n")  # no section header
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn(self.system, str(ctx.exception))

    def test_unreadable_file_is_an_error(self):
        os.makedirs(self.user)  # a directory where the file should be
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn(self.user, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
