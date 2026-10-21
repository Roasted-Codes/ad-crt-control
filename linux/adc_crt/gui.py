"""GTK 3 control panel for the Apple Studio Display 17" CRT (``adc-crt gui``).

Port of the 2.x ``pi-adc-gui.py``: same layout (11 contrast/geometry sliders with spin buttons,
V. Rate label, Degauss / Read / Write / Defaults, power switch with a 5 s cancellable power-off
countdown), the same ``_writing`` feedback guard, SIGUSR1 = refresh contrast, SIGTERM = quit.

Differences from 2.x: runs as the logged-in user (no sudo, no hardcoded Wayland environment),
talks to the monitor through ``adc_crt.monitor.Monitor``, is single-instance via a pidfile, and
runs all monitor I/O on one background worker thread in submission order (UI updates go through
``GLib.idle_add``), so a fast slider drag can never land an older value after a newer one.
"""

from __future__ import annotations

import collections
import signal
import sys
import threading
from typing import Any, Callable, Deque, Dict, Optional

from . import runtime
from .protocol import CODE_CONTRAST, CONTROLS, PowerState

# Bound by _load_gtk(). Keeping gi out of module import lets the CLI and the tests import this
# module on machines without PyGObject.
Gtk: Any = None
GLib: Any = None

POWER_OFF_DELAY_S = 5
_TICK_MS = 1000  # countdown tick; tests shorten it
DOCTOR_HINT = "Run `adc-crt doctor` in a terminal for a step-by-step fix."
GTK_MISSING = ("GTK 3 for Python is not available ({}). Install python3-gi and gir1.2-gtk-3.0 "
               "(Raspberry Pi OS / Debian) or python-gobject and gtk3 (CachyOS / Arch).")


def _log(message: str) -> None:
    print(f"adc-crt gui: {message}", file=sys.stderr, flush=True)


def _load_gtk() -> None:
    global Gtk, GLib
    if Gtk is None:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib as glib, Gtk as gtk
        Gtk, GLib = gtk, glib


class _Worker:
    """Runs monitor I/O off the GTK thread, one job at a time, in submission order.

    Control writes coalesce: while a write for a code is still queued, newer values replace its
    value, so dragging a slider cannot pile up stale writes and the last value always wins.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._jobs: Deque[Callable[[], None]] = collections.deque()
        self._pending: Dict[int, int] = {}
        self._thread: Optional[threading.Thread] = None

    def submit(self, job: Callable[[], None]) -> None:
        with self._cond:
            self._jobs.append(job)
            self._wake_locked()

    def submit_write(self, code: int, value: int, write: Callable[[int, int], None]) -> None:
        with self._cond:
            already_queued = code in self._pending
            self._pending[code] = value
            if already_queued:
                return

            def job() -> None:
                with self._cond:
                    latest = self._pending.pop(code)
                write(code, latest)

            self._jobs.append(job)
            self._wake_locked()

    def _wake_locked(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="adc-crt-io", daemon=True)
            self._thread.start()
        self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs:
                    self._cond.wait()
                job = self._jobs.popleft()
            try:
                job()
            except Exception as exc:  # jobs report their own errors; this is a last resort
                _log(f"background job failed: {exc}")


def _try(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except Exception:
        return None


class ControlWindow:
    """The main window. `monitor` is an adc_crt.monitor.Monitor (or a test fake)."""

    def __init__(self, monitor: Any) -> None:
        self.monitor = monitor
        self.adjustments: Dict[int, Any] = {}
        self.scales: Dict[int, Any] = {}
        # Codes whose adjustment is being set from a monitor read: their change signals must
        # not write the value straight back (slider change -> write -> read -> slider change).
        self._writing: set = set()
        self._updating_power = False
        self._poweroff_timer: Optional[int] = None
        self._poweroff_countdown = 0
        self._worker = _Worker()
        self._signal_sources: list = []

        window = Gtk.Window(title="ADC CRT Control")
        self.window = window
        window.set_border_width(8)
        window.set_icon_name("video-display")
        window.connect("destroy", self._on_destroy)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        window.add(vbox)

        # --- Geometry controls ---
        geo_frame = Gtk.Frame(label="Geometry")
        geo_frame.set_margin_bottom(4)
        vbox.pack_start(geo_frame, True, True, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(4)
        grid.set_margin_start(8)
        grid.set_margin_end(8)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)
        geo_frame.add(grid)

        for row, control in enumerate(CONTROLS):
            label = Gtk.Label(label=control.label, xalign=0)
            label.set_width_chars(14)
            grid.attach(label, 0, row, 1, 1)

            adj = Gtk.Adjustment(value=0, lower=0, upper=control.max_value,
                                 step_increment=1, page_increment=10)
            self.adjustments[control.code] = adj

            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_width_chars(4)
            spin.connect("value-changed", self._on_spin_changed, control.code)
            grid.attach(spin, 1, row, 1, 1)

            scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
            scale.set_draw_value(False)
            scale.set_hexpand(True)
            scale.set_size_request(200, -1)
            scale.connect("button-release-event", self._on_scale_release, control.code)
            self.scales[control.code] = scale
            grid.attach(scale, 2, row, 1, 1)

        # --- Button bar ---
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_margin_bottom(4)
        vbox.pack_start(btn_box, False, False, 0)

        self.vsync_label = Gtk.Label(label="V. Rate: -- Hz")
        self.vsync_label.set_margin_end(12)
        btn_box.pack_start(self.vsync_label, False, False, 4)

        for text, handler, tip in (
                ("Degauss", self.degauss, "Degauss the tube"),
                ("Read", self.read_all, "Read all values from the monitor"),
                ("Write", self.save, "Save the current settings in the monitor's memory (NVRAM)"),
                ("Defaults", self.restore_defaults, "Restore factory geometry and contrast")):
            button = Gtk.Button(label=text)
            button.set_tooltip_text(tip)
            button.connect("clicked", handler)
            btn_box.pack_start(button, False, False, 2)

        # --- Power ---
        pwr_frame = Gtk.Frame(label="Power")
        pwr_frame.set_margin_bottom(4)
        vbox.pack_start(pwr_frame, False, False, 0)

        power_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        power_box.set_margin_start(8)
        power_box.set_margin_top(4)
        power_box.set_margin_bottom(4)
        pwr_frame.add(power_box)

        self.power_switch = Gtk.Switch()
        self.power_switch.connect("state-set", self._on_power_state_set)
        power_box.pack_start(self.power_switch, False, False, 0)

        self.countdown_label = Gtk.Label()
        power_box.pack_start(self.countdown_label, False, False, 0)

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self.cancel_poweroff)
        power_box.pack_start(self.cancel_button, False, False, 0)

        # --- Status bar ---
        self.status_label = Gtk.Label(label="", xalign=0)
        vbox.pack_start(self.status_label, False, False, 0)

        window.show_all()
        self.countdown_label.hide()
        self.cancel_button.hide()
        self.read_all()

    # --- plumbing ------------------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """SIGUSR1 (sent by tools that changed brightness/contrast) refreshes the contrast slider."""
        self._signal_sources.append(
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_sigusr1))

    def _on_sigusr1(self, *_args: object) -> bool:
        self.refresh_contrast()
        return True  # keep the handler installed (the 2.x GUI dropped it after one signal)

    def _on_destroy(self, *_args: object) -> None:
        if self._poweroff_timer is not None:
            GLib.source_remove(self._poweroff_timer)
            self._poweroff_timer = None
        for source in self._signal_sources:
            GLib.source_remove(source)
        self._signal_sources.clear()
        if Gtk.main_level() > 0:
            Gtk.main_quit()

    def _set_status(self, text: str) -> None:
        """Thread-safe; idle callbacks run in order, so the last status set wins."""
        GLib.idle_add(self._apply_status, text)

    def _apply_status(self, text: str) -> bool:
        self.status_label.set_text(text)
        return False

    def _set_adjustment(self, code: int, value: int) -> None:
        adj = self.adjustments.get(code)
        if adj is None:
            return
        self._writing.add(code)
        try:
            adj.set_value(value)
        finally:
            self._writing.discard(code)

    def _set_switch(self, active: bool) -> None:
        self._updating_power = True
        try:
            self.power_switch.set_active(active)
        finally:
            self._updating_power = False

    def _show_values(self, values: Dict[int, int], vsync: Optional[float] = None,
                     power: Optional[PowerState] = None, status: Optional[str] = None) -> bool:
        for code, value in values.items():
            self._set_adjustment(code, value)
        if vsync is not None:
            self.vsync_label.set_text(f"V. Rate: {vsync:.1f} Hz")
        if power is not None and self._poweroff_timer is None:
            self._set_switch(power == PowerState.ON)
        if status is not None:
            self.status_label.set_text(status)
        return False

    def _run_job(self, busy: str, action: Callable[[], None], done: str) -> None:
        self._set_status(busy)

        def job() -> None:
            try:
                action()
            except Exception as exc:
                self._set_status(f"Error: {exc}")
            else:
                self._set_status(done)

        self._worker.submit(job)

    # --- sliders -------------------------------------------------------------------------------

    def _on_spin_changed(self, spin: Any, code: int) -> None:
        if code in self._writing:
            return
        self._queue_write(code, int(round(spin.get_value())))

    def _on_scale_release(self, _scale: Any, _event: Any, code: int) -> bool:
        self._queue_write(code, int(round(self.adjustments[code].get_value())))
        return False

    def _queue_write(self, code: int, value: int) -> None:
        self._set_status(f"Setting 0x{code:02X} = {value}...")
        self._worker.submit_write(code, value, self._write_value)

    def _write_value(self, code: int, value: int) -> None:
        try:
            self.monitor.write_control(code, value)
        except Exception as exc:
            self._set_status(f"Error: {exc}")
        else:
            self._set_status("")

    # --- buttons -------------------------------------------------------------------------------

    def read_all(self, _widget: Any = None) -> None:
        self._set_status("Reading values from monitor...")

        def job() -> None:
            try:
                values = self.monitor.read_all()
            except Exception as exc:
                self._set_status(f"Error: {exc}")
                return
            vsync = _try(self.monitor.read_vsync_hz)
            power = _try(self.monitor.read_power)
            GLib.idle_add(self._show_values, values, vsync, power, "")

        self._worker.submit(job)

    def refresh_contrast(self) -> None:
        def job() -> None:
            try:
                value = self.monitor.read_control(CODE_CONTRAST)
            except Exception as exc:
                self._set_status(f"Error: {exc}")
                return
            GLib.idle_add(self._show_values, {CODE_CONTRAST: value})

        self._worker.submit(job)

    def degauss(self, _widget: Any = None) -> None:
        self._run_job("Degaussing...", self.monitor.degauss, "")

    def save(self, _widget: Any = None) -> None:
        """The "Write" button: store the current settings in the monitor's NVRAM."""
        self._run_job("Saving settings to monitor NVRAM...", self.monitor.save, "Settings saved.")

    def restore_defaults(self, _widget: Any = None) -> None:
        self._set_status("Restoring defaults...")

        def job() -> None:
            try:
                self.monitor.write_factory_defaults()
            except Exception as exc:
                self._set_status(f"Error: {exc}")
                return
            try:
                values = self.monitor.read_all()
            except Exception:
                values = {c.code: c.factory_default for c in CONTROLS}
            GLib.idle_add(self._show_values, values, None, None, "Defaults restored.")

        self._worker.submit(job)

    # --- power ---------------------------------------------------------------------------------

    def _on_power_state_set(self, _switch: Any, on: bool) -> bool:
        if self._updating_power:
            return False
        if on:
            if self._poweroff_timer is not None:
                # Switched back on during the countdown: nothing was sent, just stop it.
                self._stop_countdown()
                self._set_status("Power-off cancelled.")
                return False
            self._set_status("Powering on...")
            self._worker.submit(lambda: self._do_power(True))
        else:
            # Powering off the CRT can blank the only screen (and make the Pi drop HDMI), so
            # give the user a few seconds to change their mind.
            self._start_poweroff_countdown()
        return False

    def _do_power(self, on: bool) -> None:
        try:
            self.monitor.set_power(on)
        except Exception as exc:
            self._set_status(f"Error: {exc}")
            GLib.idle_add(self._restore_switch, not on)
        else:
            self._set_status("" if on else "Monitor powered off.")

    def _restore_switch(self, active: bool) -> bool:
        if self._poweroff_timer is None:
            self._set_switch(active)
        return False

    def _countdown_text(self) -> str:
        return (f"Powering off in {self._poweroff_countdown}s — "
                "Ctrl+Alt+P or `adc-crt power on` turns it back on")

    def _start_poweroff_countdown(self) -> None:
        self._stop_countdown()
        self._poweroff_countdown = POWER_OFF_DELAY_S
        self.countdown_label.set_text(self._countdown_text())
        self.countdown_label.show()
        self.cancel_button.show()
        self._poweroff_timer = GLib.timeout_add(_TICK_MS, self._poweroff_tick)

    def _poweroff_tick(self) -> bool:
        self._poweroff_countdown -= 1
        if self._poweroff_countdown > 0:
            self.countdown_label.set_text(self._countdown_text())
            return True
        self._poweroff_timer = None  # returning False removes the source
        self.countdown_label.hide()
        self.cancel_button.hide()
        self._set_status("Powering off...")
        self._worker.submit(lambda: self._do_power(False))
        return False

    def _stop_countdown(self) -> None:
        if self._poweroff_timer is not None:
            GLib.source_remove(self._poweroff_timer)
            self._poweroff_timer = None
        self.countdown_label.hide()
        self.cancel_button.hide()

    def cancel_poweroff(self, _widget: Any = None) -> None:
        was_pending = self._poweroff_timer is not None
        self._stop_countdown()
        self._set_switch(True)
        self._set_status("Power-off cancelled." if was_pending else "")


def _show_error_dialog(message: str) -> None:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                               text="ADC CRT Control cannot reach the monitor")
    dialog.set_title("ADC CRT Control")
    hint = "" if "adc-crt doctor" in message else f"\n\n{DOCTOR_HINT}"
    dialog.format_secondary_text(f"{message}{hint}")
    dialog.run()
    dialog.destroy()


def _quit(*_args: object) -> bool:
    if Gtk.main_level() > 0:
        Gtk.main_quit()
    return True


def _close_quietly(monitor: Any) -> None:
    try:
        monitor.close()
    except Exception:
        pass


def main() -> int:
    try:
        instance = runtime.acquire_instance(runtime.GUI_PIDFILE)
    except (OSError, RuntimeError) as exc:
        _log(f"warning: no single-instance pidfile ({exc}); continuing without it")
        instance = None
    else:
        if instance is None:
            return 0  # a GUI is already open
    try:
        return _run_gui()
    finally:
        if instance is not None:
            instance.release()


def _run_gui() -> int:
    try:
        _load_gtk()
    except (ImportError, ValueError) as exc:
        _log(GTK_MISSING.format(exc))
        return 1
    ok = Gtk.init_check(sys.argv)
    if isinstance(ok, tuple):  # PyGObject returns (success, argv)
        ok = ok[0]
    if not ok:
        _log("cannot open a display; start it from inside the desktop session "
             "(not over plain SSH or sudo)")
        return 1

    from .monitor import Monitor, MonitorError

    monitor = None
    try:
        monitor = Monitor()
        monitor.read_all()
    except (MonitorError, OSError) as exc:
        _log(str(exc))
        _show_error_dialog(str(exc))
        if monitor is not None:
            _close_quietly(monitor)
        return 1

    try:
        app = ControlWindow(monitor)
        app.install_signal_handlers()
        # Clean exit on SIGTERM (sent by `adc-crt buttons` to close the GUI) and Ctrl+C.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)
        Gtk.main()
    finally:
        _close_quietly(monitor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
