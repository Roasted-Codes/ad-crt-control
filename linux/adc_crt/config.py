"""Configuration: which I2C bus and hidraw node to use, when auto-discovery is not wanted.

Precedence, per setting: environment (ADC_CRT_I2C_BUS, ADC_CRT_HIDRAW)
  > $XDG_CONFIG_HOME/adc-crt-control/config.ini (default ~/.config/...)
  > /etc/adc-crt-control.conf

    [ddc]
    bus = 1
    [hid]
    hidraw = /dev/hidraw3

An empty value means "not set here" and falls through to the next source. Every source that
exists is validated; a bad value raises ValueError naming the variable or file.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

ENV_I2C_BUS = "ADC_CRT_I2C_BUS"
ENV_HIDRAW = "ADC_CRT_HIDRAW"
SYSTEM_CONFIG_PATH = "/etc/adc-crt-control.conf"
APP_DIR = "adc-crt-control"
USER_CONFIG_NAME = "config.ini"


@dataclass
class Config:
    i2c_bus: Optional[int] = None
    hidraw: Optional[str] = None


_BUS_RE = re.compile(r"(\d+)|(?:/dev/)?i2c-(\d+)")
_HIDRAW_NAME_RE = re.compile(r"hidraw\d+")


def parse_i2c_bus(value: str, where: str) -> Optional[int]:
    """"1", "i2c-1" or "/dev/i2c-1" -> 1; "" -> None; anything else -> ValueError naming ``where``."""
    text = value.strip()
    if not text:
        return None
    match = _BUS_RE.fullmatch(text)
    if not match:
        raise ValueError(f"{where}: invalid I2C bus {value!r} — expected a bus number such as 1 "
                         "(or /dev/i2c-1)")
    return int(match.group(1) or match.group(2))


def parse_hidraw(value: str, where: str) -> Optional[str]:
    """"/dev/hidraw3" or "hidraw3" -> "/dev/hidraw3"; "" -> None; relative paths -> ValueError."""
    text = value.strip()
    if not text:
        return None
    if _HIDRAW_NAME_RE.fullmatch(text):
        return "/dev/" + text
    if os.path.isabs(text):
        return text
    raise ValueError(f"{where}: invalid hidraw device {value!r} — expected an absolute path such as "
                     "/dev/hidraw3")


def user_config_path(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME", "")
    if not base or not os.path.isabs(base):  # XDG spec: relative values are invalid, ignore them
        home = env.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".config")
    return os.path.join(base, APP_DIR, USER_CONFIG_NAME)


def _read_file(path: str) -> Config:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle, source=path)
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read config file {path}: {exc}") from exc
    except configparser.Error as exc:
        raise ValueError(f"invalid config file {path}: {exc}") from exc
    bus = parser.get("ddc", "bus", fallback="")
    hidraw = parser.get("hid", "hidraw", fallback="")
    return Config(
        i2c_bus=parse_i2c_bus(bus, f"{path}: [ddc] bus"),
        hidraw=parse_hidraw(hidraw, f"{path}: [hid] hidraw"),
    )


def _read_env(env: Mapping[str, str]) -> Config:
    return Config(
        i2c_bus=parse_i2c_bus(env.get(ENV_I2C_BUS, ""), f"environment variable {ENV_I2C_BUS}"),
        hidraw=parse_hidraw(env.get(ENV_HIDRAW, ""), f"environment variable {ENV_HIDRAW}"),
    )


def load_config(environ: Optional[Mapping[str, str]] = None, user_path: Optional[str] = None,
                system_path: str = SYSTEM_CONFIG_PATH) -> Config:
    """Merge the three sources (see module docstring). Parameters exist for tests."""
    env = os.environ if environ is None else environ
    if user_path is None:
        user_path = user_config_path(env)
    merged = Config()
    for layer in (_read_file(system_path), _read_file(user_path), _read_env(env)):  # low -> high
        if layer.i2c_bus is not None:
            merged.i2c_bus = layer.i2c_bus
        if layer.hidraw is not None:
            merged.hidraw = layer.hidraw
    return merged


def describe_sources(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Where configuration is looked for, for `adc-crt doctor` output."""
    env = os.environ if environ is None else environ
    return {"environment": f"{ENV_I2C_BUS}, {ENV_HIDRAW}", "user": user_config_path(env),
            "system": SYSTEM_CONFIG_PATH}
