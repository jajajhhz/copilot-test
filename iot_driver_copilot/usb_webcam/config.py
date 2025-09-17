import os
from dataclasses import dataclass


def _get_env_str(name: str, default: str = None):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _get_env_int(name: str, default: int = None):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _get_env_float(name: str, default: float = None):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


@dataclass
class Config:
    http_host: str
    http_port: int
    device: str
    width: int
    height: int
    fps: float
    jpeg_quality: int
    read_timeout: float
    backoff_base: float
    backoff_max: float


def load_config() -> Config:
    http_host = _get_env_str("HTTP_HOST", "0.0.0.0")
    http_port = _get_env_int("HTTP_PORT", 8000)

    device = _get_env_str("CAM_DEVICE", "0")

    width = _get_env_int("CAM_WIDTH", None)
    height = _get_env_int("CAM_HEIGHT", None)
    fps = _get_env_float("CAM_FPS", None)

    jpeg_quality = _get_env_int("CAM_JPEG_QUALITY", 80)
    if jpeg_quality < 1:
        jpeg_quality = 1
    if jpeg_quality > 100:
        jpeg_quality = 100

    read_timeout = _get_env_float("CAM_READ_TIMEOUT_SEC", 5.0)
    if read_timeout is None or read_timeout <= 0:
        read_timeout = 5.0

    backoff_base_ms = _get_env_int("CAM_BACKOFF_BASE_MS", 500)
    backoff_max_ms = _get_env_int("CAM_BACKOFF_MAX_MS", 10000)
    backoff_base = max(0.1, (backoff_base_ms or 500) / 1000.0)
    backoff_max = max(backoff_base, (backoff_max_ms or 10000) / 1000.0)

    return Config(
        http_host=http_host,
        http_port=http_port,
        device=device,
        width=width,
        height=height,
        fps=fps,
        jpeg_quality=jpeg_quality,
        read_timeout=read_timeout,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
    )
