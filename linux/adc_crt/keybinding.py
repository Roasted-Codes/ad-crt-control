"""labwc keyboard shortcut helper (``adc-crt keybinding labwc [--install]``).

Binds Ctrl+Alt+P to ``adc-crt power toggle`` in the user's ``~/.config/labwc/rc.xml``
($XDG_CONFIG_HOME aware). Edits are string-level insertions, so everything else in the file stays
byte-for-byte identical; the result is parsed before it is written, a timestamped backup is kept,
and a file that is not well-formed XML is never touched. No sed, no root.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, List, Optional, Tuple

# Ctrl+Alt+P. Never bind Ctrl+Alt+F1..F12: on Linux those switch virtual terminals, so the
# shortcut would strand the user on a text console (possibly with the CRT off as well).
KEY = "C-A-p"
COMMAND = "adc-crt power toggle"
ROOT_TAGS = ("labwc_config", "openbox_config")


def keybind_block(indent: str, with_default: bool = False) -> str:
    # labwc loads its built-in keybinds (Alt+Tab, ...) only when the configuration defines no
    # keybind at all, or when it contains <default />. Adding our single keybind to a config
    # without keybinds would silently drop them, so `with_default` keeps them.
    default = f"{indent}<default />\n" if with_default else ""
    return (default + f'{indent}<keybind key="{KEY}">\n'
            f'{indent}  <action name="Execute" command="{COMMAND}" />\n'
            f'{indent}</keybind>\n')


NEW_FILE_TEMPLATE = ('<?xml version="1.0"?>\n<labwc_config>\n  <keyboard>\n'
                     + keybind_block("    ", with_default=True) + "  </keyboard>\n</labwc_config>\n")


def rc_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "labwc", "rc.xml")


def system_rc_paths() -> List[str]:
    dirs = os.environ.get("XDG_CONFIG_DIRS", "") or "/etc/xdg"
    return [os.path.join(d, "labwc", "rc.xml") for d in dirs.split(":") if os.path.isabs(d)]


# --- string-level editing ------------------------------------------------------------------------

def _comment_spans(text: str) -> List[Tuple[int, int]]:
    spans, pos = [], 0
    while True:
        start = text.find("<!--", pos)
        if start < 0:
            return spans
        end = text.find("-->", start + 4)
        end = len(text) if end < 0 else end + 3
        spans.append((start, end))
        pos = end


def _find_outside_comments(text: str, needle: str, last: bool = False) -> int:
    spans = _comment_spans(text)
    found = -1
    index = text.find(needle)
    while index >= 0:
        if not any(start <= index < end for start, end in spans):
            if not last:
                return index
            found = index
        index = text.find(needle, index + 1)
    return found


def _insert_before(text: str, index: int, make_block: Any) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    prefix = text[line_start:index]
    if prefix.strip() == "":  # closing tag on its own line: match its indentation
        return text[:line_start] + make_block(prefix) + text[line_start:]
    return text[:index] + "\n" + make_block("") + text[index:]


def insert_keybind(text: str, with_default: bool = False) -> str:
    """Insert the keybind before the first real </keyboard>, or add a <keyboard> element."""
    close = _find_outside_comments(text, "</keyboard>")
    if close >= 0:
        return _insert_before(text, close,
                              lambda indent: keybind_block(indent + "  ", with_default))
    for root in ROOT_TAGS:
        close = _find_outside_comments(text, f"</{root}>", last=True)
        if close >= 0:
            return _insert_before(
                text, close,
                lambda indent: (f"{indent}  <keyboard>\n"
                                + keybind_block(indent + "    ", with_default)
                                + f"{indent}  </keyboard>\n"))
    raise ValueError("found neither </keyboard> nor a closing </labwc_config> tag")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _has_keybind(root: Any) -> bool:
    for keyboard in root:
        if _local(keyboard.tag) != "keyboard":
            continue
        for bind in keyboard:
            if _local(bind.tag) == "keybind" and bind.get("key") == KEY and any(
                    _local(a.tag) == "action" and a.get("command") == COMMAND for a in bind.iter()):
                return True
    return False


def _defines_keybinds(root: Any) -> bool:
    return any(_local(child.tag) in ("keybind", "default")
               for keyboard in root if _local(keyboard.tag) == "keyboard" for child in keyboard)


def _insert_validated(raw: bytes) -> bytes:
    """Return `raw` with the keybind added; ValueError if the input or the result is not valid.

    If the file defines no keybinds yet, labwc is currently using its built-in ones, so
    <default /> is added with ours to keep them.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"it is not well-formed XML ({exc})") from None
    with_default = not _defines_keybinds(root)
    # surrogateescape keeps any non-UTF-8 bytes exactly as they were.
    text = raw.decode("utf-8", "surrogateescape")
    new_raw = insert_keybind(text, with_default).encode("utf-8", "surrogateescape")
    try:
        root = ET.fromstring(new_raw)
    except ET.ParseError as exc:
        raise ValueError(f"the edited file would not be well-formed XML ({exc})") from None
    if not _has_keybind(root):
        raise ValueError("could not place the keybind inside <keyboard>")
    return new_raw


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _new_file_contents() -> Tuple[bytes, Optional[str]]:
    """Contents for a missing user rc.xml.

    labwc reads only the first rc.xml it finds unless started with --merge-config, and drops its
    default keybinds when a file defines any. So when a system rc.xml exists, start from a copy
    of it (plus our keybind) instead of a bare file that would hide the system configuration.
    """
    for candidate in system_rc_paths():
        try:
            with open(candidate, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        if COMMAND.encode() in raw:
            return raw, candidate
        try:
            return _insert_validated(raw), candidate
        except ValueError:
            continue
    return NEW_FILE_TEMPLATE.encode("utf-8"), None


def _reconfigure() -> None:
    labwc = shutil.which("labwc")
    ok = False
    if labwc:
        try:
            ok = subprocess.run([labwc, "--reconfigure"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    print("labwc reloaded its configuration." if ok else
          "Run `labwc --reconfigure` (or log out and back in) to activate it.")


def install(path: Optional[str] = None) -> int:
    target = os.path.realpath(path or rc_path())  # follow a dotfiles symlink, keep the link
    if not os.path.exists(target):
        data, source = _new_file_contents()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _atomic_write(target, data, 0o644)
        origin = f"a copy of {source}" if source else "a minimal labwc config"
        print(f"Created {target} ({origin}) with Ctrl+Alt+P -> `{COMMAND}`.")
        _reconfigure()
        return 0

    with open(target, "rb") as handle:
        raw = handle.read()
    if COMMAND.encode() in raw:
        print(f"Already installed: {target} already runs `{COMMAND}`; nothing changed.")
        return 0
    try:
        new_raw = _insert_validated(raw)
    except ValueError as exc:
        print(f"adc-crt keybinding: cannot edit {target}: {exc}. The file was left unchanged.",
              file=sys.stderr)
        return 1
    backup = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(target, backup)
    _atomic_write(target, new_raw, stat.S_IMODE(os.stat(target).st_mode))
    print(f"Added Ctrl+Alt+P -> `{COMMAND}` to {target} (backup: {backup}).")
    _reconfigure()
    return 0


def main(args: Any) -> int:
    wm = getattr(args, "wm", None) or "labwc"
    if wm != "labwc":
        print(f"adc-crt keybinding: {wm!r} is not supported; only labwc can be set up "
              f"automatically. In KDE Plasma or GNOME, add a custom shortcut that runs "
              f"`{COMMAND}` in System Settings.", file=sys.stderr)
        return 2
    path = rc_path()
    if getattr(args, "install", False):
        return install(path)
    print(f"Add this inside <keyboard> in {path}:\n\n{keybind_block('  ')}\n"
          "then run `labwc --reconfigure` (or log out and back in).\n"
          "`adc-crt keybinding labwc --install` does this for you and keeps a timestamped backup.")
    return 0
