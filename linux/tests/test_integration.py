"""Desktop-integration tests: runtime locks, button toggle, boot power-on, labwc keybinding, GUI.

Fakes only; no hardware. The GUI tests skip unless PyGObject/GTK 3 and a display are available
(run them headless with `xvfb-run python3 -m unittest tests.test_integration`).
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adc_crt  # noqa: E402
from adc_crt import buttons, gui, keybinding, poweron, runtime  # noqa: E402
from adc_crt.protocol import BUTTON_USAGE, CODE_CONTRAST, CONTROLS, PowerState  # noqa: E402


@contextlib.contextmanager
def fake_submodules(**modules):
    """Make `from . import NAME` / `from .NAME import X` inside adc_crt resolve to fakes."""
    with contextlib.ExitStack() as stack:
        for name, module in modules.items():
            stack.enter_context(mock.patch.dict(sys.modules, {f"adc_crt.{name}": module}))
            stack.enter_context(mock.patch.object(adc_crt, name, module, create=True))
        yield


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TempRuntimeDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.rundir = os.path.join(self.tmp, runtime.APP_DIR)


# --- runtime ---------------------------------------------------------------------------------

class RuntimeTests(TempRuntimeDir):
    def test_runtime_dir_is_private(self):
        path = runtime.runtime_dir()
        self.assertEqual(path, self.rundir)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        os.chmod(path, 0o755)
        runtime.runtime_dir()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_fallback_without_xdg_runtime_dir(self):
        fallback = os.path.join(self.tmp, "fallback-{uid}")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(runtime, "FALLBACK_TEMPLATE", fallback):
            path = runtime.runtime_dir()
        self.assertEqual(path, fallback.format(uid=os.getuid()))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_lock_is_exclusive_and_release_removes_file(self):
        lock = runtime.acquire_instance("gui.pid")
        self.assertIsNotNone(lock)
        path = os.path.join(self.rundir, "gui.pid")
        self.assertEqual(runtime.read_pid(path), os.getpid())
        self.assertIsNone(runtime.acquire_instance("gui.pid"))  # flock conflicts across opens
        lock.release()
        self.assertFalse(os.path.exists(path))
        again = runtime.acquire_instance("gui.pid")
        self.assertIsNotNone(again)
        again.release()

    def test_stale_pidfile_is_taken_over(self):
        os.makedirs(self.rundir, mode=0o700)
        path = os.path.join(self.rundir, "gui.pid")
        stale = dead_pid()
        with open(path, "w") as handle:
            handle.write(f"{stale}\n")
        self.assertFalse(runtime.pid_alive(stale))
        lock = runtime.acquire_instance("gui.pid")  # nobody holds the flock
        self.assertIsNotNone(lock)
        self.assertEqual(runtime.read_pid(path), os.getpid())
        lock.release()

    def test_pid_helpers(self):
        self.assertTrue(runtime.pid_alive(os.getpid()))
        self.assertFalse(runtime.pid_alive(0))
        self.assertIn("python", runtime.process_cmdline(os.getpid()).lower())
        self.assertEqual(runtime.process_cmdline(dead_pid()), "")
        path = os.path.join(self.tmp, "junk")
        for content in ("", "abc", "-5", "0"):
            with open(path, "w") as handle:
                handle.write(content)
            self.assertIsNone(runtime.read_pid(path), content)
        self.assertIsNone(runtime.read_pid(os.path.join(self.tmp, "missing")))


# --- buttons ---------------------------------------------------------------------------------

class ButtonsToggleTests(TempRuntimeDir):
    def setUp(self):
        super().setUp()
        buttons._children.clear()
        self.addCleanup(buttons._children.clear)
        os.makedirs(self.rundir, mode=0o700)
        self.pidfile = os.path.join(self.rundir, runtime.GUI_PIDFILE)
        self.kill = mock.patch("os.kill").start()
        self.popen = mock.patch.object(buttons.subprocess, "Popen").start()
        self.popen.return_value.pid = 4321
        self.popen.return_value.poll.return_value = None
        self.which = mock.patch.object(buttons.shutil, "which", return_value="/usr/bin/adc-crt").start()
        self.addCleanup(mock.patch.stopall)

    def write_pidfile(self, pid):
        with open(self.pidfile, "w") as handle:
            handle.write(f"{pid}\n")

    def toggle(self, alive, cmdline):
        with mock.patch.object(runtime, "pid_alive", return_value=alive), \
                mock.patch.object(runtime, "process_cmdline", return_value=cmdline), \
                contextlib.redirect_stderr(io.StringIO()):
            return buttons.toggle_gui()

    def test_alive_gui_gets_sigterm(self):
        self.write_pidfile(777)
        self.assertEqual(self.toggle(True, "/usr/bin/python3 /usr/bin/adc-crt gui"), "closed")
        self.kill.assert_called_once_with(777, signal.SIGTERM)
        self.popen.assert_not_called()

    def test_module_launched_gui_is_recognised(self):
        self.write_pidfile(778)
        self.assertEqual(self.toggle(True, "/usr/bin/python3 -m adc_crt.cli gui"), "closed")
        self.kill.assert_called_once_with(778, signal.SIGTERM)

    def test_dead_pid_spawns_detached_gui(self):
        self.write_pidfile(779)
        self.assertEqual(self.toggle(False, ""), "opened")
        self.kill.assert_not_called()
        argv = self.popen.call_args[0][0]
        kwargs = self.popen.call_args[1]
        self.assertEqual(argv, ["/usr/bin/adc-crt", "gui"])
        self.assertTrue(kwargs["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[stream], subprocess.DEVNULL)

    def test_missing_pidfile_spawns(self):
        self.assertEqual(self.toggle(False, ""), "opened")
        self.popen.assert_called_once()

    def test_unrelated_process_is_never_killed(self):
        self.write_pidfile(780)  # pid reused by something else
        self.assertEqual(self.toggle(True, "/usr/lib/firefox/firefox"), "opened")
        self.kill.assert_not_called()
        self.popen.assert_called_once()

    def test_just_spawned_child_is_closed_before_its_pidfile_exists(self):
        self.assertEqual(self.toggle(False, ""), "opened")
        self.assertEqual(self.toggle(False, ""), "closed")
        self.kill.assert_called_once_with(4321, signal.SIGTERM)
        self.popen.assert_called_once()

    def test_fallback_command_without_launcher_on_path(self):
        self.which.return_value = None
        argv, cwd, env = buttons.gui_command()
        self.assertEqual(argv, [sys.executable, "-m", "adc_crt.cli", "gui"])
        parent = os.path.dirname(os.path.dirname(os.path.abspath(buttons.__file__)))
        self.assertEqual(cwd, parent)
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], parent)


class StopLoop(Exception):
    pass


class ButtonsListenTests(unittest.TestCase):
    def run_loop(self, find_results, events, clock_values, max_sleeps=1):
        fake_hid = types.SimpleNamespace(
            find_hiddev=mock.Mock(side_effect=list(find_results)),
            iter_hiddev_events=mock.Mock(side_effect=lambda path: iter(events)))
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= max_sleeps:
                raise StopLoop

        clock = iter(clock_values)
        with fake_submodules(hid=fake_hid), \
                mock.patch.object(buttons, "toggle_gui") as toggle, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(StopLoop):
                buttons.listen_forever(_sleep=fake_sleep, _clock=lambda: next(clock))
        return toggle, sleeps, err.getvalue()

    def test_presses_toggle_with_debounce_and_releases_ignored(self):
        events = [(BUTTON_USAGE, 1), (BUTTON_USAGE, 0), (BUTTON_USAGE, 1),  # bounce at +0.1 s
                  (0x0012, 1), (BUTTON_USAGE, 1)]                            # other usage; +1.0 s
        toggle, sleeps, err = self.run_loop(["/dev/usb/hiddev0"], events, [0.0, 0.1, 1.0])
        self.assertEqual(toggle.call_count, 2)
        self.assertEqual(sleeps, [buttons.RETRY_S])
        self.assertIn("event stream ended", err)

    def test_missing_device_retries_every_5_seconds(self):
        toggle, sleeps, err = self.run_loop([None, None, None], [], [], max_sleeps=3)
        self.assertEqual(sleeps, [5.0, 5.0, 5.0])
        toggle.assert_not_called()
        self.assertEqual(err.count("not found"), 1)  # logged once, not every retry

    def test_read_error_retries(self):
        fake_hid = types.SimpleNamespace(
            find_hiddev=mock.Mock(return_value="/dev/usb/hiddev0"),
            iter_hiddev_events=mock.Mock(side_effect=PermissionError(13, "Permission denied")))

        def fake_sleep(_seconds):
            raise StopLoop

        with fake_submodules(hid=fake_hid), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(StopLoop):
                buttons.listen_forever(_sleep=fake_sleep)
        self.assertIn("Permission denied", err.getvalue())

    def test_main_is_single_instance(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            held = runtime.acquire_instance(runtime.BUTTONS_LOCK)
            try:
                with mock.patch.object(buttons, "listen_forever") as listen, \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(buttons.main(), 0)
                listen.assert_not_called()
            finally:
                held.release()


# --- poweron ---------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


class PowerOnTests(unittest.TestCase):
    def run_poweron(self, *, hid_after=None, power=PowerState.OFF, buses=(), set_power_error=None,
                    wait=30):
        """hid_after: poll number (0-based) from which the HID interface exists; None = never."""
        polls = {"n": 0}

        def find_hidraw():
            n = polls["n"]
            polls["n"] += 1
            if hid_after is not None and n >= hid_after:
                return [types.SimpleNamespace(path="/dev/hidraw0", is_monitor_interface=True)]
            return []

        set_power = mock.Mock(side_effect=set_power_error)
        fake_ddc = types.SimpleNamespace(discover_buses=mock.Mock(return_value=list(buses)),
                                         set_power=set_power)
        fake_hid = types.SimpleNamespace(find_hidraw=find_hidraw)

        class FakeMonitor:
            def __init__(self, hidraw_path=None, i2c_bus=None):
                self.path = hidraw_path

            def read_power(self):
                if isinstance(power, Exception):
                    raise power
                return power

            def close(self):
                pass

        fake_monitor = types.SimpleNamespace(Monitor=FakeMonitor, MonitorError=Exception)
        fake_config = types.SimpleNamespace(
            load_config=lambda: types.SimpleNamespace(i2c_bus=None, hidraw=None))
        clock = FakeClock()
        with fake_submodules(hid=fake_hid, ddc=fake_ddc, monitor=fake_monitor, config=fake_config), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = poweron.main(wait, _clock=clock.clock, _sleep=clock.sleep)
        log = err.getvalue()
        for line in log.splitlines():
            self.assertTrue(line.startswith("[adc-crt poweron] "), line)
        return code, set_power, clock, log

    @staticmethod
    def bus(number=1, verified=True, source="raspberry-pi-gpio"):
        return types.SimpleNamespace(number=number, source=source, edid_verified=verified,
                                     path=f"/dev/i2c-{number}")

    def test_already_on(self):
        code, set_power, clock, log = self.run_poweron(hid_after=0, power=PowerState.ON)
        self.assertEqual(code, 0)
        set_power.assert_not_called()
        self.assertIn("already on", log)
        self.assertEqual(clock.sleeps, 0)

    def test_off_or_sleep_powers_on(self):
        for state in (PowerState.OFF, PowerState.SLEEP):
            code, set_power, clock, _ = self.run_poweron(hid_after=0, power=state,
                                                         buses=[self.bus(1)])
            self.assertEqual(code, 0, state)
            set_power.assert_called_once_with(1, True)
            self.assertEqual(clock.sleeps, 0)

    def test_unreadable_power_powers_on(self):
        code, set_power, _, log = self.run_poweron(hid_after=0, power=OSError(5, "EIO"),
                                                   buses=[self.bus(1)])
        self.assertEqual(code, 0)
        set_power.assert_called_once_with(1, True)
        self.assertIn("unreadable", log)

    def test_waits_for_hid_to_appear(self):
        code, set_power, clock, _ = self.run_poweron(hid_after=3, buses=[self.bus(4, source="drm:card0-DP-1")])
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, 3)
        set_power.assert_called_once_with(4, True)

    def test_no_hid_but_verified_bus_powers_on_anyway(self):
        code, set_power, clock, log = self.run_poweron(hid_after=None, buses=[self.bus(1)], wait=10)
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, 10)
        set_power.assert_called_once_with(1, True)
        self.assertIn("anyway", log)

    def test_no_hid_and_only_unverified_bus_gives_up(self):
        code, set_power, _, log = self.run_poweron(hid_after=None, buses=[self.bus(1, verified=False)],
                                                   wait=5)
        self.assertEqual(code, 1)
        set_power.assert_not_called()
        self.assertIn("not verified", log)

    def test_nothing_found(self):
        code, set_power, clock, log = self.run_poweron(hid_after=None, wait=5)
        self.assertEqual(code, 1)
        self.assertEqual(clock.sleeps, 5)
        set_power.assert_not_called()
        self.assertIn("no USB HID interface", log)
        self.assertIn("no DDC bus", log)

    def test_hid_found_but_no_bus(self):
        code, set_power, _, log = self.run_poweron(hid_after=0, power=PowerState.OFF, wait=5)
        self.assertEqual(code, 1)
        set_power.assert_not_called()
        self.assertIn("no DDC bus", log)

    def test_ddc_failure_returns_1(self):
        code, _, _, log = self.run_poweron(hid_after=0, buses=[self.bus(1)],
                                           set_power_error=OSError(6, "No such device or address"))
        self.assertEqual(code, 1)
        self.assertIn("failed", log)

    def test_zero_wait_checks_once(self):
        code, _, clock, _ = self.run_poweron(hid_after=None, wait=0)
        self.assertEqual(code, 1)
        self.assertEqual(clock.sleeps, 0)


# --- keybinding ------------------------------------------------------------------------------

WITH_KEYBOARD = b"""<?xml version="1.0"?>
<!-- user config: keep this comment and spacing -->
<labwc_config>
  <theme><name>PiXflat</name></theme>
  <keyboard>
    <!-- </keyboard> inside a comment must be ignored -->
    <keybind key="W-Return">
      <action name="Execute" command="lxterminal" />
    </keybind>
  </keyboard>
  <mouse><default /></mouse>
</labwc_config>
"""

WITHOUT_KEYBOARD = b"""<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
\t<theme><name>Clearlooks</name></theme>
</openbox_config>
"""


class KeybindingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        self.sysdir = os.path.join(tmp, "xdg")
        env = {"XDG_CONFIG_HOME": os.path.join(tmp, "config"), "XDG_CONFIG_DIRS": self.sysdir}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(keybinding.shutil, "which", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rc = os.path.join(tmp, "config", "labwc", "rc.xml")

    def install(self):
        args = types.SimpleNamespace(wm="labwc", install=True)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = keybinding.main(args)
        return code, out.getvalue() + err.getvalue()

    def write_rc(self, data):
        os.makedirs(os.path.dirname(self.rc), exist_ok=True)
        with open(self.rc, "wb") as handle:
            handle.write(data)

    def read_rc(self):
        with open(self.rc, "rb") as handle:
            return handle.read()

    def backups(self):
        folder = os.path.dirname(self.rc)
        return sorted(n for n in os.listdir(folder) if n.startswith("rc.xml.bak-"))

    def assert_bound(self, data):
        root = keybinding.ET.fromstring(data)
        self.assertTrue(keybinding._has_keybind(root))

    def test_missing_file_is_created(self):
        code, _ = self.install()
        self.assertEqual(code, 0)
        data = self.read_rc()
        self.assertEqual(data, keybinding.NEW_FILE_TEMPLATE.encode())
        self.assertIn(b'<keybind key="C-A-p">', data)
        self.assertIn(b'command="adc-crt power toggle"', data)
        self.assertIn(b"<default />", data)  # keeps labwc's built-in keybinds (Alt+Tab, ...)
        self.assert_bound(data)
        self.assertEqual(self.backups(), [])

    def test_missing_file_starts_from_system_config(self):
        system_rc = os.path.join(self.sysdir, "labwc", "rc.xml")
        os.makedirs(os.path.dirname(system_rc))
        with open(system_rc, "wb") as handle:
            handle.write(WITH_KEYBOARD)
        code, _ = self.install()
        self.assertEqual(code, 0)
        data = self.read_rc()
        self.assert_bound(data)
        self.assertIn(b"<name>PiXflat</name>", data)
        with open(system_rc, "rb") as handle:
            self.assertEqual(handle.read(), WITH_KEYBOARD)  # system file untouched

    def test_inserts_before_keyboard_close_keeping_rest_identical(self):
        self.write_rc(WITH_KEYBOARD)
        code, _ = self.install()
        self.assertEqual(code, 0)
        data = self.read_rc()
        index = WITH_KEYBOARD.index(b"  </keyboard>")
        block = keybinding.keybind_block("    ").encode()
        self.assertEqual(data, WITH_KEYBOARD[:index] + block + WITH_KEYBOARD[index:])
        self.assertNotIn(b"<default />\n    <keybind", data)  # file already had keybinds
        self.assert_bound(data)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^rc\.xml\.bak-\d{8}-\d{6}$")
        with open(os.path.join(os.path.dirname(self.rc), backups[0]), "rb") as handle:
            self.assertEqual(handle.read(), WITH_KEYBOARD)

    def test_adds_keyboard_element_when_missing(self):
        self.write_rc(WITHOUT_KEYBOARD)
        code, _ = self.install()
        self.assertEqual(code, 0)
        data = self.read_rc()
        index = WITHOUT_KEYBOARD.index(b"</openbox_config>")
        self.assertTrue(data.startswith(WITHOUT_KEYBOARD[:index]))
        self.assertTrue(data.endswith(WITHOUT_KEYBOARD[index:]))
        self.assertIn(b"  <keyboard>\n    <default />\n    <keybind", data)
        self.assert_bound(data)
        self.assertEqual(len(self.backups()), 1)

    def test_already_installed_is_unchanged(self):
        self.write_rc(WITH_KEYBOARD)
        self.install()
        before = self.read_rc()
        backups = self.backups()
        code, output = self.install()
        self.assertEqual(code, 0)
        self.assertIn("Already installed", output)
        self.assertEqual(self.read_rc(), before)
        self.assertEqual(self.backups(), backups)

    def test_malformed_xml_is_left_alone(self):
        broken = b"<labwc_config>\n  <keyboard>\n    <keybind key='W-a'>\n  </keyboard>\n"
        self.write_rc(broken)
        code, output = self.install()
        self.assertEqual(code, 1)
        self.assertIn("unchanged", output)
        self.assertEqual(self.read_rc(), broken)
        self.assertEqual(self.backups(), [])

    def test_print_mode_changes_nothing(self):
        args = types.SimpleNamespace(wm="labwc", install=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(keybinding.main(args), 0)
        self.assertIn(self.rc, out.getvalue())
        self.assertIn('<keybind key="C-A-p">', out.getvalue())
        self.assertFalse(os.path.exists(self.rc))

    def test_never_binds_ctrl_alt_function_keys(self):
        self.assertIsNone(re.match(r"^C-A-F\d+$", keybinding.KEY))


# --- GUI -------------------------------------------------------------------------------------

def _gtk_unavailable_reason():
    try:
        gui._load_gtk()
    except Exception as exc:  # ImportError, ValueError (namespace version), broken gi build
        return f"PyGObject/GTK 3 not available: {exc}"
    ok = gui.Gtk.init_check(sys.argv[:1])
    if isinstance(ok, tuple):
        ok = ok[0]
    return None if ok else "no display (run under xvfb-run)"


GTK_SKIP = _gtk_unavailable_reason()


class FakeMonitor:
    def __init__(self):
        self.values = {c.code: max(c.factory_default - 7, 0) for c in CONTROLS}
        self.writes = []
        self.power_calls = []
        self.calls = []
        self.power = PowerState.ON
        self.closed = False

    def read_all(self):
        return dict(self.values)

    def read_control(self, code):
        return self.values[code]

    def write_control(self, code, value):
        self.writes.append((code, value))
        self.values[code] = value
        return value

    def write_factory_defaults(self):
        self.calls.append("defaults")
        for control in CONTROLS:
            self.values[control.code] = control.factory_default

    def degauss(self):
        self.calls.append("degauss")

    def save(self):
        self.calls.append("save")

    def read_vsync_hz(self):
        return 85.0

    def read_power(self):
        return self.power

    def set_power(self, on):
        self.power_calls.append(on)

    def close(self):
        self.closed = True


@unittest.skipIf(GTK_SKIP, GTK_SKIP or "")
class GuiTests(TempRuntimeDir):
    def setUp(self):
        super().setUp()
        self.Gtk = gui.Gtk
        self.monitor = FakeMonitor()
        self.app = gui.ControlWindow(self.monitor)
        self.addCleanup(self._destroy)
        self.pump(lambda: self.app.vsync_label.get_text() == "V. Rate: 85.0 Hz")

    def _destroy(self):
        self.app.window.destroy()
        self.pump(lambda: False, timeout=0.05)

    def pump(self, condition, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while self.Gtk.events_pending():
                self.Gtk.main_iteration_do(False)
            if condition():
                return True
            time.sleep(0.005)
        if timeout >= 0.5:
            self.fail("GUI condition not reached in time")
        return False

    def values(self):
        return {code: int(adj.get_value()) for code, adj in self.app.adjustments.items()}

    def test_layout_and_initial_read_without_write_back(self):
        self.assertEqual(len(self.app.adjustments), 11)
        for control in CONTROLS:
            self.assertEqual(self.app.adjustments[control.code].get_upper(), control.max_value)
        self.assertEqual(self.values(), self.monitor.values)
        self.assertTrue(self.app.power_switch.get_active())
        self.assertFalse(self.app.countdown_label.get_visible())
        self.pump(lambda: False, timeout=0.1)
        self.assertEqual(self.monitor.writes, [])  # _writing guard held

    def test_slider_change_writes_value(self):
        self.app.adjustments[0x20].set_value(10)
        self.pump(lambda: (0x20, 10) in self.monitor.writes)

    def test_buttons(self):
        self.app.degauss()
        self.app.save()
        self.app.restore_defaults()
        self.pump(lambda: self.app.status_label.get_text() == "Defaults restored.")
        self.assertEqual(self.monitor.calls, ["degauss", "save", "defaults"])
        self.assertEqual(self.values(), {c.code: c.factory_default for c in CONTROLS})
        self.assertEqual(self.monitor.writes, [])  # display update did not echo writes

    def test_power_off_countdown_cancel_and_fire(self):
        with mock.patch.object(gui, "_TICK_MS", 20):
            self.app.power_switch.set_active(False)
            self.assertTrue(self.app.countdown_label.get_visible())
            self.assertIn("Powering off in 5s", self.app.countdown_label.get_text())
            self.app.cancel_poweroff()
            self.assertTrue(self.app.power_switch.get_active())
            self.pump(lambda: False, timeout=0.25)
            self.assertEqual(self.monitor.power_calls, [])

            self.app.power_switch.set_active(False)
            self.pump(lambda: self.monitor.power_calls == [False])
            self.assertFalse(self.app.countdown_label.get_visible())
            self.app.power_switch.set_active(True)
            self.pump(lambda: self.monitor.power_calls == [False, True])

    def test_switching_back_on_during_countdown_sends_nothing(self):
        with mock.patch.object(gui, "_TICK_MS", 20):
            self.app.power_switch.set_active(False)
            self.app.power_switch.set_active(True)
            self.pump(lambda: False, timeout=0.25)
        self.assertEqual(self.monitor.power_calls, [])
        self.assertFalse(self.app.countdown_label.get_visible())

    def test_sigusr1_refreshes_contrast(self):
        self.app.install_signal_handlers()
        self.monitor.values[CODE_CONTRAST] = 42
        os.kill(os.getpid(), signal.SIGUSR1)
        self.pump(lambda: self.app.adjustments[CODE_CONTRAST].get_value() == 42)
        self.assertEqual(self.monitor.writes, [])


@unittest.skipIf(GTK_SKIP, GTK_SKIP or "")
class GuiMainTests(TempRuntimeDir):
    def test_missing_monitor_shows_doctor_dialog_and_exits_1(self):
        class Boom(Exception):
            pass

        def make_monitor(*_a, **_k):
            raise Boom("Apple Studio Display not found on USB")

        fake_monitor = types.SimpleNamespace(Monitor=make_monitor, MonitorError=Boom)
        with fake_submodules(monitor=fake_monitor), \
                mock.patch.object(gui, "_show_error_dialog") as dialog, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gui.main(), 1)
        dialog.assert_called_once_with("Apple Studio Display not found on USB")
        self.assertFalse(os.path.exists(os.path.join(self.rundir, runtime.GUI_PIDFILE)))
        self.assertIn("adc-crt doctor", gui.DOCTOR_HINT)


class GuiSingleInstanceTests(TempRuntimeDir):
    def test_second_gui_exits_quietly(self):
        held = runtime.acquire_instance(runtime.GUI_PIDFILE)
        try:
            with mock.patch.object(gui, "_run_gui", side_effect=AssertionError("started")):
                self.assertEqual(gui.main(), 0)
        finally:
            held.release()

    def test_pidfile_removed_after_run(self):
        seen = []
        with mock.patch.object(gui, "_run_gui", side_effect=lambda: seen.append(
                runtime.read_pid(os.path.join(self.rundir, runtime.GUI_PIDFILE))) or 0):
            self.assertEqual(gui.main(), 0)
        self.assertEqual(seen, [os.getpid()])
        self.assertFalse(os.path.exists(os.path.join(self.rundir, runtime.GUI_PIDFILE)))


if __name__ == "__main__":
    unittest.main()
