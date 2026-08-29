#!/usr/bin/env python
"""Run BetterGI once per day according to .env or process environment settings.

Required settings:
    NORMAL_GROUPS=Group A,Group B
    MODE=normal
    BASE_TIME=04:03
    DAILY_OFFSET_MINUTES=3
    CYCLE_DAYS=10

Only when MODE=test:
    TEST_GROUPS=Test Group
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from dotenv import dotenv_values
from pycaw.pycaw import AudioUtilities

from system_utils import read_brightness, restore_saved_state, set_brightness

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
LOG_PATH = ROOT / "run_script.log"
BRIGHTNESS_STATE_PATH = ROOT / ".brightness-state.json"
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
NO_WINDOW = subprocess.CREATE_NO_WINDOW
GAME_START_TIMEOUT_SECONDS = 10 * 60
TEST_MODE_DELAY_SECONDS = 10


@dataclass(frozen=True)
class Config:
    groups: list[str]
    mode: str
    base_hour: int
    base_minute: int
    daily_offset_minutes: int
    cycle_days: int
    bettergi_directory: Path


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("bettergi_runner")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    for handler in (
        logging.StreamHandler(),
        RotatingFileHandler(LOG_PATH, encoding="utf-8", maxBytes=1_000_000, backupCount=3),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def is_administrator() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def load_environment() -> dict[str, str]:
    values = {key: value for key, value in dotenv_values(ENV_PATH).items() if value is not None}
    values.update(os.environ)
    return values


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = map(int, value.split(":"))
    except ValueError as error:
        raise ValueError("BASE_TIME must use HH:MM format") from error
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("BASE_TIME must use HH:MM format")
    return hour, minute


def positive_int(values: dict[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as error:
        raise ValueError(f"{key} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def nonnegative_int(values: dict[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as error:
        raise ValueError(f"{key} must be a non-negative integer") from error
    if value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def load_config() -> Config:
    values = load_environment()
    mode = values.get("MODE", "normal").strip().lower()
    if mode not in {"normal", "test"}:
        raise ValueError("MODE must be either 'normal' or 'test'")
    groups_key = "TEST_GROUPS" if mode == "test" else "NORMAL_GROUPS"
    groups = [name.strip() for name in values.get(groups_key, "").split(",") if name.strip()]
    if not groups:
        raise ValueError(f"{groups_key} must contain one or more comma-separated group names")
    hour, minute = parse_time(values.get("BASE_TIME", ""))
    return Config(
        groups=groups,
        mode=mode,
        base_hour=hour,
        base_minute=minute,
        daily_offset_minutes=nonnegative_int(values, "DAILY_OFFSET_MINUTES"),
        cycle_days=positive_int(values, "CYCLE_DAYS"),
        bettergi_directory=Path(values.get("BETTERGI_DIRECTORY", r"C:\Program Files\BetterGI")),
    )


def scheduled_time(config: Config, now: datetime) -> datetime:
    """Calculate one daily run time from the whole Unix-day cycle."""
    unix_day = int(now.timestamp() // 86_400)

    def target(day_offset: int) -> datetime:
        minutes = config.daily_offset_minutes * ((unix_day + day_offset) % config.cycle_days)
        return (now + timedelta(days=day_offset)).replace(
            hour=config.base_hour, minute=config.base_minute, second=0, microsecond=0
        ) + timedelta(minutes=minutes)

    today = target(0)
    return today if today > now else target(1)


def wait_for_schedule(config: Config, logger: logging.Logger, started_at: datetime) -> None:
    if config.mode == "test":
        target = started_at + timedelta(seconds=TEST_MODE_DELAY_SECONDS)
    else:
        target = scheduled_time(config, datetime.now(LOCAL_TIMEZONE))
    seconds = max(0, (target - datetime.now(LOCAL_TIMEZONE)).total_seconds())
    logger.info("Run mode: %s", config.mode)
    logger.info("Scheduled run time: %s", target.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Waiting %.0f seconds.", seconds)
    time.sleep(seconds)


class RunRecovery:
    """Save and restore brightness and mute state for one BetterGI run."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.active = False

    def prepare(self, was_muted: bool) -> None:
        monitors = read_brightness()
        state = {"monitors": monitors, "was_muted": was_muted}
        BRIGHTNESS_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        launch_helper("watch")
        if monitors:
            set_brightness(0)
            self.logger.info("System brightness set to minimum.")
        else:
            self.logger.warning("No WMI-controlled display brightness was found.")
        self.active = True

    def restore(self) -> None:
        if not self.active:
            return
        if restore_saved_state(BRIGHTNESS_STATE_PATH):
            self.active = False
            self.logger.info("Restored system brightness and mute state.")


class SystemState:
    """All temporary system changes made by one BetterGI run."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.recovery = RunRecovery(logger)
        self.awake = False

    def prepare(self) -> None:
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        if result == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        self.awake = True
        self.logger.info("Windows sleep, hibernation, and display power-off are disabled.")
        speakers = AudioUtilities.GetSpeakers()
        if speakers is None:
            raise RuntimeError("No default Windows speaker endpoint is available.")
        volume = speakers.EndpointVolume
        was_muted = bool(volume.GetMute())
        self.recovery.prepare(was_muted)
        volume.SetMute(1, None)
        self.logger.info("System audio muted for BetterGI run.")

    def restore(self) -> None:
        with suppress(Exception):
            self.recovery.restore()
        if self.awake:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.logger.info("Restored normal Windows power policy.")


def bettergi_processes() -> list[psutil.Process]:
    return [
        process for process in psutil.process_iter(["pid", "name"])
        if (process.info["name"] or "").lower() == "bettergi.exe"
    ]


def stop_bettergi_if_exist(logger: logging.Logger, wait_before_relaunch: bool = False) -> None:
    processes = bettergi_processes()
    for process in processes:
        logger.info("Terminating BetterGI.exe (pid=%s).", process.pid)
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.kill()
    if processes:
        _, alive = psutil.wait_procs(processes, timeout=10)
        if alive:
            logger.warning("BetterGI still running: %s", [process.pid for process in alive])
        elif wait_before_relaunch:
            time.sleep(10)


def start_bettergi(config: Config, logger: logging.Logger) -> None:
    executable = config.bettergi_directory / "BetterGI.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"BetterGI.exe not found: {executable}")
    command = [str(executable), "--startGroups", *config.groups]
    logger.info("Launching command: %r", command)
    subprocess.Popen(command)


def yuanshen_running() -> bool:
    return any(
        (process.info["name"] or "").lower() == "yuanshen.exe"
        for process in psutil.process_iter(["name"])
    )


def wait_for_yuanshen_to_close(logger: logging.Logger, start_deadline: float) -> bool:
    logger.info("Waiting for YuanShen.exe to start and then close.")
    seen_running = yuanshen_running()
    stopped_at: float | None = None
    while True:
        if yuanshen_running():
            seen_running, stopped_at = True, None
        elif seen_running:
            stopped_at = stopped_at or time.monotonic()
            if time.monotonic() - stopped_at >= 10:
                return True
        elif time.monotonic() >= start_deadline:
            logger.error("YuanShen.exe did not start within 10 minutes; ending this run.")
            return False
        time.sleep(1)


def launch_helper(mode: str) -> None:
    command = [sys.executable, str(ROOT / "recovery_helper.py"), mode, str(os.getpid())]
    if mode == "watch":
        command.append(str(BRIGHTNESS_STATE_PATH))
    subprocess.Popen(
        command,
        creationflags=NO_WINDOW,
    )


def main() -> int:
    started_at = datetime.now(LOCAL_TIMEZONE)
    logger = configure_logging()
    if not is_administrator():
        logger.error("Run this script as administrator.")
        return 1

    try:
        config = load_config()
    except Exception:
        logger.exception("Invalid configuration.")
        return 1

    logger.info("Loaded groups: %s", config.groups)
    logger.info(
        "Schedule: MODE=%s, BASE_TIME=%02d:%02d, DAILY_OFFSET_MINUTES=%d, CYCLE_DAYS=%d.",
        config.mode,
        config.base_hour, config.base_minute, config.daily_offset_minutes, config.cycle_days,
    )

    system = SystemState(logger)
    hibernate = True
    try:
        system.prepare()                     # Prepare: prevent sleep, mute audio, dim brightness.
        wait_for_schedule(config, logger, started_at)
        game_start_deadline = time.monotonic() + GAME_START_TIMEOUT_SECONDS
        stop_bettergi_if_exist(logger, True) # Prepare: remove an old BetterGI instance.
        start_bettergi(config, logger)       # Run BetterGI.
        game_started = wait_for_yuanshen_to_close(logger, game_start_deadline)
        stop_bettergi_if_exist(logger)
        return 0 if game_started else 1
    except KeyboardInterrupt:
        hibernate = False
        logger.info("Interrupted by user; Windows will not hibernate.")
        return 130
    except Exception:
        logger.exception("Runner failed.")
        return 1
    finally:
        system.restore()
        if hibernate:
            launch_helper("hibernate")
            logger.info("Runner is exiting; Windows will hibernate in 10 seconds.")


if __name__ == "__main__":
    sys.exit(main())
