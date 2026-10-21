"""Front-panel button listener (``adc-crt buttons``), started by desktop autostart.

The monitor's physical brightness button (hiddev usage 0x00E4) toggles the GUI: press once to
open it, press again to close it. Runs headless as the logged-in user. Only one process may read
the hiddev node (two listeners split the button events between them), hence the flock.

If the monitor is unplugged or not readable, the listener logs it and retries every 5 s instead
of exiting.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import runtime
from .protocol import BUTTON_USAGE

DEBOUNCE_S = 0.5
RETRY_S = 5.0
GUI_MARKERS = ("adc-crt", "adc_crt")
PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# GUIs we spawned; polled to reap them and to catch one that has not written its pidfile yet.
_children: List[subprocess.Popen] = []


def _log(message: str) -> None:
    print(f"[adc-crt buttons] {message}", file=sys.stderr, flush=True)


def running_gui_pid() -> Optional[int]:
    """Pid from the GUI pidfile if that process is alive and really is an adc-crt GUI."""
    try:
        pid = runtime.read_pid(runtime.instance_path(runtime.GUI_PIDFILE))
    except (OSError, RuntimeError):
        return None
    if pid is None or pid == os.getpid() or not runtime.pid_alive(pid):
        return None
    cmdline = runtime.process_cmdline(pid)
    if any(marker in cmdline for marker in GUI_MARKERS):
        return pid
    return None  # stale pidfile whose pid now belongs to an unrelated process


def _starting_child_pid() -> Optional[int]:
    """A GUI we spawned that is still running but may not have written its pidfile yet."""
    for child in list(_children):
        if child.poll() is not None:  # also reaps it
            _children.remove(child)
    return _children[-1].pid if _children else None


def gui_command() -> Tuple[List[str], Optional[str], Optional[Dict[str, str]]]:
    """(argv, cwd, env) used to start the GUI."""
    launcher = shutil.which("adc-crt")
    if launcher:
        return [launcher, "gui"], None, None
    env = dict(os.environ)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = PACKAGE_PARENT + (os.pathsep + old if old else "")
    return [sys.executable, "-m", "adc_crt.cli", "gui"], PACKAGE_PARENT, env


def spawn_gui() -> Optional[subprocess.Popen]:
    argv, cwd, env = gui_command()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
    except OSError as exc:
        _log(f"cannot start the GUI ({' '.join(argv)}): {exc}")
        return None
    _children.append(proc)
    _log(f"opened GUI (pid {proc.pid})")
    return proc


def toggle_gui() -> str:
    """Close the running GUI, or open one. Returns "closed", "opened" or "error"."""
    pid = running_gui_pid()
    if pid is None:
        pid = _starting_child_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # exited in the meantime: open a new one instead
        except OSError as exc:
            _log(f"cannot close the GUI (pid {pid}): {exc}")
            return "error"
        else:
            _log(f"closed GUI (pid {pid})")
            return "closed"
    return "opened" if spawn_gui() is not None else "error"


def listen_forever(*, _sleep: Callable[[float], None] = time.sleep,
                   _clock: Callable[[], float] = time.monotonic) -> None:
    """Find the hiddev node, toggle the GUI on button presses, retry every 5 s on problems."""
    from . import hid

    last_press: Optional[float] = None
    last_error: Optional[str] = None
    while True:
        error: Optional[str] = None
        try:
            path = hid.find_hiddev()
        except Exception as exc:
            path, error = None, f"hiddev discovery failed: {exc}"
        if path:
            if last_error is None or path not in last_error:
                _log(f"listening for the brightness button on {path}")
            try:
                for usage, value in hid.iter_hiddev_events(path):
                    if usage != BUTTON_USAGE or value == 0:  # value 0 = release
                        continue
                    now = _clock()
                    if last_press is not None and now - last_press < DEBOUNCE_S:
                        continue
                    last_press = now
                    try:
                        toggle_gui()
                    except Exception as exc:
                        _log(f"toggle failed: {exc}")
                error = f"{path}: event stream ended (monitor unplugged?)"
            except Exception as exc:  # OSError/HidError: unplugged (ENODEV), no permission, ...
                error = f"{path}: {exc}"
        elif error is None:
            error = "monitor button device (hiddev for USB 05ac:9213) not found"
        if error != last_error:  # don't repeat the same complaint every 5 s
            _log(f"{error}; retrying every {RETRY_S:g} s")
        last_error = error
        _sleep(RETRY_S)


def _exit_on_signal(_signum: int, _frame: object) -> None:
    raise SystemExit(0)


def main() -> int:
    try:
        lock = runtime.acquire_instance(runtime.BUTTONS_LOCK)
    except (OSError, RuntimeError) as exc:
        _log(f"cannot create the lock file: {exc}")
        return 1
    if lock is None:
        _log("another `adc-crt buttons` is already running; exiting")
        return 0
    signal.signal(signal.SIGTERM, _exit_on_signal)  # so the lock file is cleaned up
    try:
        listen_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
