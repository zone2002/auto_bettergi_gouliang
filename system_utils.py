"""Windows system state helpers shared by the runner and its recovery helper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import wmi
from pycaw.pycaw import AudioUtilities


def _wmi() -> Any:
    return wmi.WMI(namespace="wmi")


def read_brightness() -> list[dict[str, Any]]:
    return [
        {"InstanceName": monitor.InstanceName, "Brightness": int(monitor.CurrentBrightness)}
        for monitor in _wmi().WmiMonitorBrightness()
        if monitor.Active
    ]


def set_brightness(value: int, instance_name: str | None = None) -> None:
    active_names = {monitor["InstanceName"] for monitor in read_brightness()}
    targets = {instance_name} if instance_name is not None else active_names
    methods = {
        method.InstanceName: method
        for method in _wmi().WmiMonitorBrightnessMethods()
        if method.InstanceName in targets
    }
    missing = targets - methods.keys()
    if missing:
        raise RuntimeError(f"Brightness control not found: {sorted(missing)}")
    for method in methods.values():
        method.WmiSetBrightness(Timeout=1, Brightness=value)


def set_mute(muted: bool) -> None:
    speakers = AudioUtilities.GetSpeakers()
    if speakers is None:
        raise RuntimeError("No default Windows speaker endpoint is available.")
    speakers.EndpointVolume.SetMute(int(muted), None)


def restore_saved_state(path: Path) -> bool:
    """Restore brightness and mute state. Keep the file if any restore fails."""
    if not path.is_file():
        return True
    state = json.loads(path.read_text(encoding="utf-8"))
    try:
        for monitor in state["monitors"]:
            set_brightness(int(monitor["Brightness"]), monitor["InstanceName"])
        set_mute(bool(state["was_muted"]))
    except Exception:
        return False
    path.unlink(missing_ok=True)
    return True
