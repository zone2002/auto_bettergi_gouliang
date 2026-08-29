"""Recover temporary system state after the BetterGI runner exits."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

from system_utils import restore_saved_state


def wait_for_exit(pid: int) -> None:
    while psutil.pid_exists(pid):
        time.sleep(0.5)


def main() -> int:
    mode, parent_pid = sys.argv[1], int(sys.argv[2])
    wait_for_exit(parent_pid)
    if mode == "watch":
        return 0 if restore_saved_state(Path(sys.argv[3])) else 1
    if mode == "hibernate":
        time.sleep(10)
        subprocess.run(["shutdown", "/h"], check=False)
        return 0
    raise ValueError(f"Unknown helper mode: {mode}")


if __name__ == "__main__":
    sys.exit(main())
