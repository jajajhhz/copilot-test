import os
from dataclasses import dataclass
from typing import Optional


def _required_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _optional_int(name: str) -> Optional[int]:
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return int(val)


def _optional_float(name: str) -> Optional[float]:
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return float(val)


@dataclass
class Config:
    http_host: str
    http_port: int
    camera_index: int
    reconnect_initial_backoff_ms: int
    reconnect_max_backoff_ms: int
    read_errors_before_reset: int
    frame_wait_timeout_sec: float
    jpeg_quality: int
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]


def load_config() -> Config:
    # Required env variables
    http_host = _required_env("HTTP_HOST")
    http_port = int(_required_env("HTTP_PORT"))
    camera_index = int(_required_env("CAMERA_INDEX"))

    reconnect_initial_backoff_ms = int(_required_env("RECONNECT_INITIAL_BACKOFF_MS"))
    reconnect_max_backoff_ms = int(_required_env("RECONNECT_MAX_BACKOFF_MS"))
    read_errors_before_reset = int(_required_env("READ_ERRORS_BEFORE_RESET"))
    frame_wait_timeout_sec = float(_required_env("FRAME_WAIT_TIMEOUT_SEC"))
    jpeg_quality = int(_required_env("JPEG_QUALITY"))

    # Optional env variables (applied if present)
    width = _optional_int("WIDTH")
    height = _optional_int("HEIGHT")
    fps = _optional_float("FPS")

    return Config(
        http_host=http_host,
        http_port=http_port,
        camera_index=camera_index,
        reconnect_initial_backoff_ms=reconnect_initial_backoff_ms,
        reconnect_max_backoff_ms=reconnect_max_backoff_ms,
        read_errors_before_reset=read_errors_before_reset,
        frame_wait_timeout_sec=frame_wait_timeout_sec,
        jpeg_quality=jpeg_quality,
        width=width,
        height=height,
        fps=fps,
    )
