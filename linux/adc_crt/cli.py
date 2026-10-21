"""`adc-crt`: command-line control of the Apple Studio Display 17" CRT on Linux.

Exit status: 0 = success, 1 = error (device, permission, I/O), 2 = usage error.

hid.py, ddc.py, config.py and the GUI/button/power-on modules are imported lazily inside the
commands that need them, so `--help`, `--version` and `self-test` work on any machine.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from typing import Any, Callable, List, Optional, Sequence

from . import __version__
from .monitor import DOCTOR_HINT, Monitor, MonitorError, pick_bus, pick_hidraw
from .protocol import (
    CODE_DEGAUSS,
    CODE_POWER,
    CODE_SAVE,
    CODE_VSYNC,
    CONTROLS,
    CONTROLS_BY_CODE,
    DDC_DEST_ADDR,
    EDID_HEADER,
    EDID_LENGTH,
    POWER_OFF_PACKET,
    POWER_ON_PACKET,
    PowerState,
    control_write_payload,
    ddc_set_vcp_packet,
    edid_identity,
    feature_write_candidates,
    is_studio_display_edid,
    lookup_control,
    parse_feature_value,
    parse_int,
    power_state_from_raw,
)

PROG = "adc-crt"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# Filesystem roots used by `doctor`; tests point these at fake trees.
SYS_ROOT = "/sys"
DEV_ROOT = "/dev"
PROC_ROOT = "/proc"

USER_CONFIG_HINT = "~/.config/adc-crt-control/config.ini"

_FIX_UDEV = [
    "make sure /usr/lib/udev/rules.d/70-adc-crt-control.rules is installed (it comes with the "
    "adc-crt-control package), then run `sudo udevadm control --reload-rules && sudo udevadm trigger`",
    "unplug and replug the monitor's USB cable, or log out and back in; access is granted to the "
    "active local desktop session (an SSH session does not get it)",
]
_FIX_I2C_GROUP = "`sudo usermod -aG i2c $USER`, then log out and back in"
_FIX_MODPROBE = ("`sudo modprobe i2c-dev` (the package loads it at every boot via "
                 "/usr/lib/modules-load.d/adc-crt-control.conf)")
_FIX_PI_I2C = "`sudo raspi-config nonint do_i2c 0`, then reboot"
_FIX_BUS_OVERRIDE = (f"if you know the right bus, pass `--bus N` or set `[ddc] bus = N` in {USER_CONFIG_HINT} "
                     "(or ADC_CRT_I2C_BUS=N)")
_FIX_PI_WIRING = ("check the GPIO wiring to the adapter's DVI port (pin 3 -> DVI 7 SDA, pin 5 -> DVI 6 SCL, "
                  "pin 9 -> DVI 15 GND); `i2cdetect -y 1` should show 37 and 50")
_FIX_DESKTOP_CABLE = ("connect the monitor to this computer's graphics card with a cable/adapter that passes "
                      "DDC (most HDMI/DisplayPort-to-VGA adapters do, some cheap ones do not)")
_GUI_DEPS = ("the GUI needs PyGObject and GTK 3: `sudo apt install python3-gi gir1.2-gtk-3.0` "
             "(Raspberry Pi OS / Debian) or `sudo pacman -S python-gobject gtk3` (Arch / CachyOS)")


class UsageError(Exception):
    """Bad command-line input detected after parsing (exit status 2)."""


# --- lazily imported transports (tests replace these functions) -------------------------------

def _hid_module() -> Any:
    from . import hid
    return hid


def _ddc_module() -> Any:
    from . import ddc
    return ddc


def _config_module() -> Any:
    from . import config
    return config


# --- small helpers -------------------------------------------------------------------------------

def _err(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _with_hint(message: str) -> str:
    message = message.rstrip()
    if "adc-crt doctor" in message:
        return message
    return f"{message.rstrip('.')}. {DOCTOR_HINT}"


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _bus_path(bus: Any) -> str:
    return getattr(bus, "path", None) or f"/dev/i2c-{bus.number}"


def _load_config() -> Any:
    try:
        return _config_module().load_config()
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError(_with_hint(
            f"could not read the adc-crt-control configuration ({exc}); fix or remove {USER_CONFIG_HINT} "
            "or /etc/adc-crt-control.conf, and check ADC_CRT_I2C_BUS / ADC_CRT_HIDRAW")) from exc


def _marks() -> tuple:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"  # None: an in-memory stream
    try:
        "✓✗".encode(encoding)
        return "✓", "✗"
    except (LookupError, UnicodeEncodeError):
        return "[ok]", "[FAIL]"


# --- argparse value types ----------------------------------------------------------------------

def _int_arg(text: str) -> int:
    try:
        return parse_int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r} (use decimal like 32 or hex like 0x20)") from None


def _byte_arg(text: str) -> int:
    value = _int_arg(text)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError(f"{text!r} is out of range 0..255 (0x00..0xFF)")
    return value


def _bus_arg(text: str) -> int:
    value = _int_arg(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"I2C bus number must be 0 or more, got {text!r}")
    return value


def _seconds_arg(text: str) -> int:
    value = _int_arg(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"seconds must be 0 or more, got {text!r}")
    return value


# --- command context ----------------------------------------------------------------------------

class _Context:
    """Holds the one Monitor a command uses, created on first use and closed by main()."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._monitor: Any = None

    @property
    def monitor(self) -> Any:
        if self._monitor is None:
            self._monitor = Monitor(hidraw_path=self.args.hidraw, i2c_bus=self.args.bus)
        return self._monitor

    def close(self) -> None:
        if self._monitor is not None:
            self._monitor.close()
            self._monitor = None


def _export_overrides(args: argparse.Namespace) -> None:
    """Pass --bus/--hidraw to gui/buttons/poweron (and processes they spawn) via the config env vars."""
    if args.bus is not None:
        os.environ["ADC_CRT_I2C_BUS"] = str(args.bus)
    if args.hidraw:
        os.environ["ADC_CRT_HIDRAW"] = args.hidraw


def _control_arg(key: str) -> Any:
    try:
        return lookup_control(key)
    except KeyError as exc:
        raise UsageError(exc.args[0] if exc.args else f"unknown control {key!r}") from None


# --- doctor -------------------------------------------------------------------------------------

def _exists(path: str) -> bool:
    return os.path.exists(path)


def _access(path: str, mode: int) -> bool:
    return os.access(path, mode)


def _pi_model() -> Optional[str]:
    """The Raspberry Pi model string from the device tree, or None when this is not a Pi."""
    try:
        with open(os.path.join(PROC_ROOT, "device-tree", "model"), "rb") as handle:
            model = handle.read().replace(b"\x00", b"").decode("ascii", "replace").strip()
    except OSError:
        return None
    return model if "Raspberry Pi" in model else None


def _i2c_group_status() -> tuple:
    """("root" | "member" | "pending" | "not-member" | "no-group", user name)."""
    import grp
    import pwd

    uid = os.getuid()
    try:
        user = pwd.getpwuid(uid).pw_name
        primary_gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        user, primary_gid = str(uid), None
    if uid == 0:
        return "root", user
    try:
        group = grp.getgrnam("i2c")
    except KeyError:
        return "no-group", user
    if group.gr_gid in os.getgroups() or group.gr_gid == os.getegid():
        return "member", user
    if user in group.gr_mem or primary_gid == group.gr_gid:
        return "pending", user  # added to the group, but this login session predates it
    return "not-member", user


class _Doctor:
    def __init__(self) -> None:
        self.ok_mark, self.fail_mark = _marks()
        self.required_failures = 0
        self.optional_failures = 0

    def ok(self, text: str, details: Sequence[str] = ()) -> None:
        print(f"{self.ok_mark} {text}")
        for line in details:
            print(f"    {line}")

    def fail(self, text: str, fixes: Sequence[str], required: bool = True, details: Sequence[str] = ()) -> None:
        print(f"{self.fail_mark} {text}" + ("" if required else " (optional)"))
        for line in details:
            print(f"    {line}")
        for fix in fixes:
            print(f"    fix: {fix}")
        if required:
            self.required_failures += 1
        else:
            self.optional_failures += 1

    def skip(self, text: str) -> None:
        print(f"- {text}")


def _strip_hint(message: str) -> str:
    return message.replace(" " + DOCTOR_HINT, "").strip()


def cmd_doctor(args: argparse.Namespace, ctx: _Context) -> int:
    """Read-only checklist. Never writes to the monitor, the config or the system."""
    doc = _Doctor()
    pi_model = _pi_model()
    print(f"adc-crt-control {__version__} doctor (Python {platform.python_version()}"
          + (f", {pi_model})" if pi_model else ")"))
    print()

    # Configuration
    cfg: Any = None
    try:
        cfg = _config_module().load_config()
        parts = []
        if getattr(cfg, "i2c_bus", None) is not None:
            parts.append(f"i2c bus {cfg.i2c_bus}")
        if getattr(cfg, "hidraw", None):
            parts.append(f"hidraw {cfg.hidraw}")
        doc.ok("configuration: " + (", ".join(parts) if parts else "no overrides (auto-detect)"))
    except Exception as exc:
        doc.fail(f"configuration could not be read: {exc}",
                 [f"fix or remove {USER_CONFIG_HINT} and /etc/adc-crt-control.conf, and check the "
                  "ADC_CRT_I2C_BUS / ADC_CRT_HIDRAW environment variables"])

    # USB HID (hidraw): everything except power-setting goes through it
    hid = _hid_module()
    hid_path: Optional[str] = None
    explicit = args.hidraw or getattr(cfg, "hidraw", None)
    if explicit:
        source = "--hidraw" if args.hidraw else "config"
        if _exists(explicit):
            hid_path = explicit
            doc.ok(f"USB HID device: {explicit} (from {source})")
        else:
            doc.fail(f"USB HID device {explicit} (from {source}) does not exist",
                     ["list the monitor's nodes with `adc-crt hid list`, or remove the override to auto-detect"])
    else:
        try:
            infos = list(hid.find_hidraw())
        except OSError as exc:
            infos = []
            doc.fail(f"could not scan /sys/class/hidraw: {exc}", ["run `adc-crt doctor` again; check `dmesg`"])
        info = pick_hidraw(infos)
        others = [f"also: {i.path}" for i in infos if i is not info]
        if info is None:
            doc.fail("USB HID device 05ac:9213 (Apple Studio Display) not found",
                     ["connect the ADC adapter's USB cable to this computer and make sure the adapter is powered",
                      "`lsusb` should list 05ac:9213; if it does but this check still fails, look at `dmesg` "
                      "for usbhid errors"])
        elif not getattr(info, "is_monitor_interface", False):
            hid_path = info.path
            doc.fail(f"USB HID device: {info.path} found, but no node reports the Monitor Control "
                     "usage (page 0x80); using it anyway",
                     ["if reads fail, pick another node with `--hidraw /dev/hidrawN` (see `adc-crt hid list`)"],
                     required=False, details=others)
        else:
            hid_path = info.path
            name = getattr(info, "name", "") or ""
            doc.ok(f"USB HID device: {info.path} (monitor-control interface{', ' + name if name else ''})", others)

    hid_usable = False
    if hid_path:
        if _access(hid_path, os.R_OK | os.W_OK):
            hid_usable = True
            doc.ok(f"USB HID permission: {hid_path} is readable and writable")
        else:
            doc.fail(f"USB HID permission: you cannot read and write {hid_path}", _FIX_UDEV)

    # hiddev: only the front-panel button listener needs it
    try:
        hiddev = hid.find_hiddev()
    except OSError:
        hiddev = None
    if hiddev is None:
        doc.fail("button events: no hiddev node for 05ac:9213 (needed only by `adc-crt buttons`)",
                 ["the kernel needs CONFIG_USB_HIDDEV (Raspberry Pi OS and Arch kernels have it); "
                  "replug the monitor's USB cable"], required=False)
    elif _access(hiddev, os.R_OK):
        doc.ok(f"button events: {hiddev} is readable")
    else:
        doc.fail(f"button events: you cannot read {hiddev} (needed only by `adc-crt buttons`)",
                 _FIX_UDEV, required=False)

    # I2C / DDC: power on/off goes through it
    if _exists(os.path.join(SYS_ROOT, "class", "i2c-dev")):
        doc.ok("i2c-dev kernel module loaded (/sys/class/i2c-dev present)")
    else:
        doc.fail("i2c-dev kernel module not loaded (/sys/class/i2c-dev missing)",
                 [_FIX_MODPROBE] + ([_FIX_PI_I2C] if pi_model else []))

    pi_bus_path = os.path.join(DEV_ROOT, "i2c-1")
    if pi_model:
        if _exists(pi_bus_path):
            doc.ok(f"Raspberry Pi I2C enabled ({pi_bus_path} present)")
        else:
            doc.fail(f"Raspberry Pi I2C is not enabled ({pi_bus_path} missing)", [_FIX_PI_I2C])
        status, user = _i2c_group_status()
        if status in ("root", "member"):
            doc.ok("Raspberry Pi: running as root" if status == "root" else f"Raspberry Pi: user {user} is in group i2c")
        elif status == "pending":
            doc.fail(f"Raspberry Pi: user {user} was added to group i2c, but this login session predates it",
                     ["log out and back in (or reboot)"],
                     required=not _access(pi_bus_path, os.R_OK | os.W_OK))
        elif status == "no-group":
            doc.fail("Raspberry Pi: there is no i2c group on this system",
                     [_FIX_PI_I2C + " (Raspberry Pi OS creates the group and its udev rule)"],
                     required=not _access(pi_bus_path, os.R_OK | os.W_OK))
        else:
            doc.fail(f"Raspberry Pi: user {user} is not in group i2c", [_FIX_I2C_GROUP],
                     required=not _access(pi_bus_path, os.R_OK | os.W_OK))

    ddc = _ddc_module()
    config_bus = args.bus if args.bus is not None else getattr(cfg, "i2c_bus", None)
    try:
        buses = list(ddc.discover_buses(config_bus=config_bus))
    except OSError as exc:
        buses = []
        doc.fail(f"DDC bus discovery failed: {exc}", [_FIX_BUS_OVERRIDE])
    chosen = pick_bus(buses)
    if chosen is None:
        doc.fail("no DDC/CI bus found for the Apple Studio Display (power on/off will not work)",
                 [_FIX_PI_WIRING if pi_model else _FIX_DESKTOP_CABLE, _FIX_BUS_OVERRIDE])
    else:
        path = _bus_path(chosen)
        details = [f"{'*' if bus is chosen else ' '} {_bus_path(bus)}  {bus.source}  "
                   f"{'EDID verified' if bus.edid_verified else 'EDID not verified'}" for bus in buses]
        if chosen.edid_verified:
            doc.ok(f"DDC bus: {path} ({chosen.source}, Studio Display EDID verified)", details)
        else:
            doc.fail(f"DDC bus: {path} ({chosen.source}), but the Studio Display's EDID could not be read "
                     "there; power commands will be sent blind",
                     [_FIX_PI_WIRING if pi_model else _FIX_DESKTOP_CABLE, _FIX_BUS_OVERRIDE],
                     required=False, details=details)
        if not _exists(path):
            doc.fail(f"DDC bus permission: {path} does not exist",
                     [_FIX_MODPROBE] + ([_FIX_PI_I2C] if pi_model else []))
        elif _access(path, os.R_OK | os.W_OK):
            doc.ok(f"DDC bus permission: {path} is readable and writable")
        else:
            fixes = [_FIX_I2C_GROUP] if (pi_model and path == pi_bus_path) else _FIX_UDEV
            doc.fail(f"DDC bus permission: you cannot read and write {path}", fixes)

    # Power state over HID: proves the HID read path end to end (read-only)
    if hid_usable:
        monitor = Monitor(hidraw_path=hid_path, i2c_bus=args.bus)
        try:
            state = monitor.read_power()
        except MonitorError as exc:
            doc.fail(f"power state could not be read over HID: {_strip_hint(str(exc))}",
                     ["check the adapter's USB cable and power; if another node works better, use "
                      "`--hidraw /dev/hidrawN` (see `adc-crt hid list`)"])
        else:
            if state is None:
                doc.fail("power state: the monitor returned an unknown value for HID 0xD6",
                         ["try again; if it persists, check `adc-crt hid read 0xD6`"], required=False)
            else:
                doc.ok(f"power state (HID 0xD6): {state.name} ({int(state)})")
        finally:
            monitor.close()
    else:
        doc.skip("power state: not checked (needs a working USB HID device)")

    print()
    if doc.required_failures:
        print(f"{doc.required_failures} required check(s) failed; apply the fixes above and run "
              "`adc-crt doctor` again.")
        return EXIT_ERROR
    suffix = f" ({doc.optional_failures} optional check(s) failed)" if doc.optional_failures else ""
    print(f"All required checks passed{suffix}.")
    return EXIT_OK


# --- self-test (offline) ---------------------------------------------------------------------

# The table from ADCControl.swift / LINUX-SPEC.md: (code, label, max, factory default).
_EXPECTED_CONTROLS = (
    (0x12, "Contrast", 96, 95), (0x20, "H Phase", 96, 64), (0x22, "H Size", 96, 70),
    (0x30, "V Phase", 96, 68), (0x32, "V Size", 96, 67), (0x24, "H Pincushion", 96, 63),
    (0x42, "H Keystone", 96, 54), (0x40, "H Key Balance", 96, 77), (0x44, "Rotation", 255, 152),
    (0x28, "H Stat Conv", 255, 127), (0x38, "V Stat Conv", 255, 127),
)


class _SelfTestFailure(Exception):
    pass


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise _SelfTestFailure(what)


def edid_fixture_path() -> str:
    """The reference EDID dump shipped in the source tree (absent from installed copies)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, os.pardir, "tests", "fixtures", "studio-display-17-crt.edid")


def _self_test_steps() -> List[tuple]:
    def controls() -> str:
        actual = tuple((c.code, c.label, c.max_value, c.factory_default) for c in CONTROLS)
        _expect(len(CONTROLS) == 11, f"expected 11 controls, found {len(CONTROLS)}")
        _expect(actual == _EXPECTED_CONTROLS, "control table (codes/labels/max/defaults/order) does not match the Mac table")
        _expect(set(CONTROLS_BY_CODE) == {c.code for c in CONTROLS}, "CONTROLS_BY_CODE is inconsistent")
        _expect((CODE_DEGAUSS, CODE_SAVE, CODE_POWER, CODE_VSYNC) == (0x01, 0xB0, 0xD6, 0xAE), "HID codes changed")
        _expect((PowerState.ON, PowerState.SLEEP, PowerState.OFF) == (1, 2, 4), "power state values changed")
        _expect(power_state_from_raw(3) is None and power_state_from_raw(None) is None, "unknown power values must map to None")
        _expect(CONTROLS_BY_CODE[0x12].clamp(200) == 96 and CONTROLS_BY_CODE[0x44].clamp(-5) == 0, "clamping is wrong")
        _expect(lookup_control("h-phase").code == 0x20 and lookup_control("0x44").code == 0x44
                and lookup_control("brightness").code == 0x12, "control name lookup is wrong")
        return "control table: 11 controls, codes/max/factory defaults match the Mac table"

    def packets() -> str:
        _expect(POWER_ON_PACKET == bytes([0x51, 0x84, 0x03, 0xD6, 0x00, 0x01, 0x6F]), "DDC power-on packet changed")
        _expect(POWER_OFF_PACKET == bytes([0x51, 0x84, 0x03, 0xD6, 0x00, 0x04, 0x6A]), "DDC power-off packet changed")
        _expect(ddc_set_vcp_packet(0xD6, 1) == POWER_ON_PACKET, "ddc_set_vcp_packet disagrees with POWER_ON_PACKET")
        for packet in (POWER_ON_PACKET, POWER_OFF_PACKET):
            check = DDC_DEST_ADDR
            for byte in packet:
                check ^= byte
            _expect(check == 0, "DDC/CI checksum does not cancel out")
        return f"DDC/CI power packets: on {_hex(POWER_ON_PACKET)}, off {_hex(POWER_OFF_PACKET)}"

    def parsing() -> str:
        cases = [
            (0x12, b"\x5f\x00", 95), (0x12, b"\x12\x5f\x00", 95),
            (0xAE, b"\x70\x17", 6000), (0xAE, b"\xae\x70\x17", 6000),
            (0xD6, b"\xd6\x01", 1), (0x12, b"", None), (0x12, b"\x12", 0x12),
        ]
        for code, raw, expected in cases:
            got = parse_feature_value(code, raw)
            _expect(got == expected, f"parse_feature_value(0x{code:02X}, {_hex(raw) or 'empty'}) = {got}, expected {expected}")
        contrast = CONTROLS_BY_CODE[0x12]
        _expect(feature_write_candidates(0x12, control_write_payload(contrast, 95)) == [b"\x12\x5f\x00", b"\x12\x5f"],
                "control write candidates are wrong")
        _expect(feature_write_candidates(0x12, control_write_payload(contrast, 0)) == [b"\x12\x00\x00", b"\x12\x00"],
                "zero-value write candidates are wrong")
        _expect(feature_write_candidates(CODE_DEGAUSS, b"\x01") == [b"\x01\x01"], "degauss write is wrong")
        _expect(feature_write_candidates(CODE_SAVE, b"\x01") == [b"\xb0\x01"], "save write is wrong")
        return f"HID report parsing and write candidates: {len(cases)} parse cases + 4 write cases"

    def edid() -> Optional[str]:
        base = bytearray(EDID_HEADER + bytes(EDID_LENGTH - len(EDID_HEADER)))
        base[8:12] = b"\x06\x10\x13\x92"
        _expect(is_studio_display_edid(bytes(base)), "synthetic Studio Display EDID not recognized")
        other = bytearray(base)
        other[10] = 0x14
        _expect(not is_studio_display_edid(bytes(other)), "a different product code was accepted")
        _expect(not is_studio_display_edid(bytes(128)), "an all-zero block (e.g. an SPD EEPROM) was accepted")
        path = edid_fixture_path()
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            data = handle.read()
        _expect(len(data) >= EDID_LENGTH and len(data) % EDID_LENGTH == 0, f"fixture {path} has {len(data)} bytes")
        _expect(len(data) == EDID_LENGTH * (data[126] + 1), "fixture extension count does not match its size")
        for start in range(0, len(data), EDID_LENGTH):
            _expect(sum(data[start:start + EDID_LENGTH]) % 256 == 0, f"fixture block {start // EDID_LENGTH} checksum is invalid")
        _expect(is_studio_display_edid(data[:EDID_LENGTH]), "the reference EDID dump is not recognized as the Studio Display")
        pnp, product, name = edid_identity(data)
        return f"EDID: reference dump recognized ({pnp} 0x{product:04X} {name or '(unnamed)'})"

    return [("controls", controls), ("packets", packets), ("parsing", parsing), ("edid", edid)]


def cmd_self_test(args: argparse.Namespace, ctx: _Context) -> int:
    ok_mark, fail_mark = _marks()
    for name, step in _self_test_steps():
        try:
            summary = step()
        except (_SelfTestFailure, KeyError, ValueError) as exc:
            print(f"{fail_mark} {name}: {exc}")
            print("self-test FAILED", file=sys.stderr)
            return EXIT_ERROR
        if summary is None:
            print(f"- {name}: reference EDID dump not installed, synthetic checks only")
        else:
            print(f"{ok_mark} {summary}")
    print("self-test passed")
    return EXIT_OK


# --- hid / ddc ----------------------------------------------------------------------------------

def cmd_hid_list(args: argparse.Namespace, ctx: _Context) -> int:
    hid = _hid_module()
    infos = list(hid.find_hidraw())
    if not infos:
        print("No Apple Studio Display HID device (05ac:9213) visible.")
        print(DOCTOR_HINT)
        return EXIT_ERROR
    try:
        configured = getattr(_config_module().load_config(), "hidraw", None)
    except Exception:
        configured = None
    chosen = args.hidraw or configured or pick_hidraw(infos).path
    for info in infos:
        marker = "*" if info.path == chosen else " "
        name = getattr(info, "name", "") or "(no name)"
        print(f"{marker} {info.path}  05AC:9213 {name}")
        if info.is_monitor_interface:
            print("    interface: monitor control (usage page 0x80, usage 0x01)")
        else:
            print("    interface: other (not the monitor-control collection)")
        print(f"    sysfs: {info.sysfs}")
        access = "read/write" if _access(info.path, os.R_OK | os.W_OK) else "no read/write permission"
        print(f"    access: {access}")
    try:
        hiddev = hid.find_hiddev()
    except OSError:
        hiddev = None
    print(f"button events (hiddev): {hiddev or 'not found'}")
    return EXIT_OK


def cmd_hid_read(args: argparse.Namespace, ctx: _Context) -> int:
    code = args.code
    raw = ctx.monitor.read_feature(code)
    value = parse_feature_value(code, raw)
    print(f"0x{code:02X} = {'(nil)' if value is None else value}")
    print(f"raw: {_hex(raw) or '(empty)'}")
    if code == CODE_VSYNC and value is not None:
        print(f"vsync: {value / 100.0:.2f} Hz")
    return EXIT_OK


def cmd_hid_write(args: argparse.Namespace, ctx: _Context) -> int:
    sent = ctx.monitor.write_feature(args.code, bytes(args.bytes))
    print(f"wrote 0x{args.code:02X}: {_hex(sent)}")
    return EXIT_OK


def cmd_ddc_buses(args: argparse.Namespace, ctx: _Context) -> int:
    config_bus = args.bus if args.bus is not None else getattr(_load_config(), "i2c_bus", None)
    buses = list(_ddc_module().discover_buses(config_bus=config_bus))
    if not buses:
        print("No DDC/CI bus for the Apple Studio Display found.")
        print(DOCTOR_HINT)
        return EXIT_ERROR
    chosen = pick_bus(buses)
    for bus in buses:
        verified = "EDID verified" if bus.edid_verified else "EDID not verified"
        print(f"{'*' if bus is chosen else ' '} {_bus_path(bus)}  {bus.source}  {verified}")
    print(f"power-on packet:  {_hex(POWER_ON_PACKET)}")
    print(f"power-off packet: {_hex(POWER_OFF_PACKET)}")
    return EXIT_OK


def cmd_ddc_edid(args: argparse.Namespace, ctx: _Context) -> int:
    bus = args.bus if args.bus is not None else ctx.monitor.i2c_bus
    data = bytes(_ddc_module().read_edid(bus))
    print(data.hex().upper())
    try:
        pnp, product, name = edid_identity(data)
    except ValueError:
        print(f"/dev/i2c-{bus}: not a valid EDID (bad header)", file=sys.stderr)
        return EXIT_OK
    verdict = "Apple Studio Display 17\" CRT" if is_studio_display_edid(data) else "not the Apple Studio Display"
    print(f"/dev/i2c-{bus}: {pnp} 0x{product:04X} {name or '(unnamed)'}: {verdict}", file=sys.stderr)
    return EXIT_OK


# --- power and controls --------------------------------------------------------------------------

def cmd_power(args: argparse.Namespace, ctx: _Context) -> int:
    monitor = ctx.monitor
    action = args.action
    if action == "read":
        state = monitor.read_power()
        if state is None:
            print("power: unknown")
            return EXIT_ERROR
        print(f"power: {state.name} ({int(state)})")
        return EXIT_OK
    if action in ("on", "off"):
        monitor.set_power(action == "on")
        print(f"power {action} sent (DDC/CI on /dev/i2c-{monitor.i2c_bus})")
        return EXIT_OK
    state = monitor.toggle_power()
    word = "on" if state == PowerState.ON else "off"
    print(f"power {word} sent (DDC/CI on /dev/i2c-{monitor.i2c_bus})")
    return EXIT_OK


def cmd_get(args: argparse.Namespace, ctx: _Context) -> int:
    key = args.control
    if key.strip().lower() == "all":
        values = ctx.monitor.read_all()
        width = max(len(c.label) for c in CONTROLS)
        print(f"{'Control':<{width}}  Code  Value  Max")
        for control in CONTROLS:
            print(f"{control.label:<{width}}  0x{control.code:02X}  {values[control.code]:>5}  {control.max_value:>3}")
        return EXIT_OK
    control = _control_arg(key)
    print(f"{control.label} (0x{control.code:02X}) = {ctx.monitor.read_control(control.code)}")
    return EXIT_OK


def cmd_set(args: argparse.Namespace, ctx: _Context) -> int:
    control = _control_arg(args.control)
    written = ctx.monitor.write_control(control.code, args.value)
    note = f" (clamped from {args.value})" if written != args.value else ""
    print(f"{control.label} (0x{control.code:02X}) = {written}{note}")
    return EXIT_OK


def cmd_defaults(args: argparse.Namespace, ctx: _Context) -> int:
    ctx.monitor.write_factory_defaults()
    print(f"factory defaults written ({len(CONTROLS)} controls); run `adc-crt save` to keep them")
    return EXIT_OK


def cmd_degauss(args: argparse.Namespace, ctx: _Context) -> int:
    ctx.monitor.degauss()
    print("degauss sent")
    return EXIT_OK


def cmd_save(args: argparse.Namespace, ctx: _Context) -> int:
    ctx.monitor.save()
    print("settings saved to the monitor's NVRAM")
    return EXIT_OK


def cmd_vsync(args: argparse.Namespace, ctx: _Context) -> int:
    hz = ctx.monitor.read_vsync_hz()
    if hz is None:
        print("vsync: unknown")
        return EXIT_ERROR
    print(f"vsync: {hz:.2f} Hz")
    return EXIT_OK


# --- desktop integration (modules written separately, imported lazily) -----------------------

def cmd_gui(args: argparse.Namespace, ctx: _Context) -> int:
    _export_overrides(args)
    try:
        from . import gui
    except ImportError as exc:
        _err(f"cannot start the GUI ({exc}); {_GUI_DEPS}")
        return EXIT_ERROR
    except ValueError as exc:  # gi.require_version("Gtk", "3.0"): "Namespace Gtk not available"
        if "Namespace" not in str(exc):
            raise
        _err(f"cannot start the GUI ({exc}); {_GUI_DEPS}")
        return EXIT_ERROR
    return gui.main()


def cmd_buttons(args: argparse.Namespace, ctx: _Context) -> int:
    _export_overrides(args)
    from . import buttons
    return buttons.main()


def cmd_poweron(args: argparse.Namespace, ctx: _Context) -> int:
    _export_overrides(args)
    from . import poweron
    return poweron.main(wait_seconds=args.wait)


def cmd_keybinding(args: argparse.Namespace, ctx: _Context) -> int:
    from . import keybinding
    return keybinding.main(args)


# --- parser ---------------------------------------------------------------------------------------

_EPILOG = """\
examples:
  adc-crt doctor                 check devices, permissions and DDC bus (read-only)
  adc-crt power toggle           read power over USB HID, send the opposite over DDC/CI
  adc-crt get all                show every control
  adc-crt set h-size 72          change a control (adc-crt save keeps it after power-off)
  adc-crt hid read 0x12          raw HID feature report
  adc-crt ddc buses              DDC/CI bus candidates

Codes are decimal (32) or hex with 0x (0x20). Control names: """ + ", ".join(c.slug for c in CONTROLS) + "."


def build_parser() -> argparse.ArgumentParser:
    # The global options are also accepted after the subcommand. default=SUPPRESS keeps a
    # subcommand that does not repeat them from resetting the values parsed before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bus", type=_bus_arg, metavar="N", default=argparse.SUPPRESS,
                        help="I2C bus number for DDC/CI (default: config, then auto-detect)")
    common.add_argument("--hidraw", metavar="PATH", default=argparse.SUPPRESS,
                        help="hidraw device node (default: config, then auto-detect)")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description='Control an Apple Studio Display 17" CRT (ADC) through a USB HID + DDC/CI adapter.',
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--bus", type=_bus_arg, metavar="N", default=None,
                        help="I2C bus number for DDC/CI (default: config, then auto-detect)")
    parser.add_argument("--hidraw", metavar="PATH", default=None,
                        help="hidraw device node (default: config, then auto-detect)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable, help_text: str, target: Any = sub) -> argparse.ArgumentParser:
        p = target.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add("doctor", cmd_doctor, "read-only checklist of devices, permissions and DDC bus, with fixes")
    add("self-test", cmd_self_test, "offline check of constants, packets, parsing and the EDID fixture")

    hid = sub.add_parser("hid", parents=[common], help="raw USB HID feature reports",
                         description="Raw USB HID feature reports (report ID == code).")
    hid_sub = hid.add_subparsers(dest="hid_command", metavar="ACTION")
    hid_sub.required = True
    add("list", cmd_hid_list, "list the monitor's hidraw nodes and the hiddev node", hid_sub)
    p = add("read", cmd_hid_read, "read one feature report", hid_sub)
    p.add_argument("code", type=_byte_arg, metavar="CODE", help="report code, e.g. 0x12 or 18")
    p = add("write", cmd_hid_write, "write one feature report: CODE followed by payload bytes (raw, no clamping)", hid_sub)
    p.add_argument("code", type=_byte_arg, metavar="CODE", help="report code, e.g. 0x12")
    p.add_argument("bytes", type=_byte_arg, nargs="+", metavar="BYTE", help="payload bytes, e.g. 95 0")

    ddc = sub.add_parser("ddc", parents=[common], help="DDC/CI (I2C) buses and EDID",
                         description="DDC/CI (I2C) bus discovery and EDID.")
    ddc_sub = ddc.add_subparsers(dest="ddc_command", metavar="ACTION")
    ddc_sub.required = True
    add("buses", cmd_ddc_buses, "list DDC/CI bus candidates (* = the one used)", ddc_sub)
    add("edid", cmd_ddc_edid, "read the EDID over I2C (hex on stdout, identity on stderr)", ddc_sub)

    p = add("power", cmd_power, "read power over HID, or set it over DDC/CI")
    p.add_argument("action", nargs="?", default="read", choices=["read", "on", "off", "toggle"],
                   help="read (default), on, off, or toggle (ON -> off, anything else -> on)")

    p = add("get", cmd_get, "read a control, or all of them")
    p.add_argument("control", nargs="?", default="all", metavar="NAME|CODE|all", help="default: all")
    p = add("set", cmd_set, "write a control (clamped to its range; `adc-crt save` keeps it)")
    p.add_argument("control", metavar="NAME|CODE")
    p.add_argument("value", type=_int_arg, metavar="VALUE")
    add("defaults", cmd_defaults, "write all 11 factory defaults (not saved until `adc-crt save`)")
    add("degauss", cmd_degauss, "degauss the CRT")
    add("save", cmd_save, "store the current settings in the monitor's NVRAM")
    add("vsync", cmd_vsync, "read the vertical refresh rate")
    add("gui", cmd_gui, "open the GTK calibration window")
    add("buttons", cmd_buttons, "listen for the front-panel button and toggle the GUI")
    p = add("poweron", cmd_poweron, "boot-time power on (used by adc-crt-poweron.service)")
    p.add_argument("--wait", type=_seconds_arg, default=30, metavar="SECONDS",
                   help="how long to wait for the monitor to appear (default: 30)")
    p = add("keybinding", cmd_keybinding, "print or install a window-manager keybinding for power toggle")
    p.add_argument("wm", choices=["labwc"], help="window manager")
    p.add_argument("--install", action="store_true", help="add it to your labwc rc.xml (with a backup)")
    return parser


def _exit_status(code: Any) -> int:
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return EXIT_ERROR


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / --version (0) or a usage error (2)
        return _exit_status(exc.code)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    ctx = _Context(args)
    try:
        result = func(args, ctx)
        return EXIT_OK if result is None else int(result)
    except UsageError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except MonitorError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except OSError as exc:  # HidError / DdcError from direct transport calls
        _err(_with_hint(str(exc)))
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
