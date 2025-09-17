import os
import logging
from dataclasses import dataclass
from typing import Optional, Union


@dataclass
class Config:
    http_host: str
    http_port: int
    webcam_device: Union[int, str]
    webcam_width: Optional[int]
    webcam_height: Optional[int]
    webcam_fps: Optional[int]
    jpeg_quality: int
    capture_timeout_sec: float
    reconnect_base_backoff: float
    reconnect_max_backoff: float
    log_level: int


def _get_int(name: str, default: Optional[int] = None) -> Optional[int]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be an integer")


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be a number")


def _get_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _parse_device(v: str) -> Union[int, str]:
    # Accept integer camera index (e.g., "0") or device path (e.g., "/dev/video0")
    try:
        return int(v)
    except ValueError:
        return v


def _parse_log_level(v: str) -> int:
    m = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return m.get(v.upper(), logging.INFO)


def load_config() -> Config:
    http_host = _get_str("HTTP_HOST", "0.0.0.0")
    http_port_raw = _get_str("HTTP_PORT", "8000")
    try:
        http_port = int(http_port_raw)
    except ValueError:
        raise ValueError("HTTP_PORT must be an integer")

    webcam_device = _parse_device(_get_str("WEBCAM_DEVICE", "0"))

    webcam_width = _get_int("WEBCAM_WIDTH", None)
    webcam_height = _get_int("WEBCAM_HEIGHT", None)
    webcam_fps = _get_int("WEBCAM_FPS", None)

    jpeg_quality = _get_int("JPEG_QUALITY", 80)
    if jpeg_quality is None or not (1 <= jpeg_quality <= 100):
        raise ValueError("JPEG_QUALITY must be between 1 and 100")

    capture_timeout_sec = _get_float("CAPTURE_TIMEOUT_SEC", 5.0)
    reconnect_base_backoff = _get_float("RECONNECT_BASE_BACKOFF_SEC", 0.5)
    reconnect_max_backoff = _get_float("RECONNECT_MAX_BACKOFF_SEC", 10.0)

    log_level = _parse_log_level(_get_str("LOG_LEVEL", "INFO"))

    return Config(
        http_host=http_host,
        http_port=http_port,
        webcam_device=webcam_device,
        webcam_width=webcam_width,
        webcam_height=webcam_height,
        webcam_fps=webcam_fps,
        jpeg_quality=jpeg_quality,
        capture_timeout_sec=capture_timeout_sec,
        reconnect_base_backoff=reconnect_base_backoff,
        reconnect_max_backoff=reconnect_max_backoff,
        log_level=log_level,
    )
