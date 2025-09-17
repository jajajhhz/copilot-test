import os
from dataclasses import dataclass


def _get_env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _get_env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        return default


def _get_env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default


@dataclass
class Config:
    http_host: str
    http_port: int
    camera_device: object  # int index or string path
    width: int
    height: int
    fps: int
    connect_timeout_sec: float
    frame_stale_sec: float
    backoff_initial_ms: int
    backoff_max_ms: int
    jpeg_quality: int
    log_level: str
    mjpeg_boundary: str

    @staticmethod
    def from_env() -> "Config":
        http_host = _get_env_str("HTTP_HOST", "0.0.0.0")
        http_port = _get_env_int("HTTP_PORT", 8000)

        cam_dev_raw = _get_env_str("CAMERA_DEVICE", "0")
        try:
            camera_device = int(cam_dev_raw)
        except Exception:
            camera_device = cam_dev_raw  # treat as path like /dev/video0

        width = _get_env_int("WIDTH", 640)
        height = _get_env_int("HEIGHT", 480)
        fps = _get_env_int("FPS", 30)

        connect_timeout_sec = _get_env_float("CONNECT_TIMEOUT_SEC", 5.0)
        frame_stale_sec = _get_env_float("FRAME_STALE_SEC", 5.0)
        backoff_initial_ms = _get_env_int("BACKOFF_INITIAL_MS", 200)
        backoff_max_ms = _get_env_int("BACKOFF_MAX_MS", 5000)

        jpeg_quality = _get_env_int("JPEG_QUALITY", 80)
        log_level = _get_env_str("LOG_LEVEL", "INFO")

        mjpeg_boundary = "frame"

        return Config(
            http_host=http_host,
            http_port=http_port,
            camera_device=camera_device,
            width=width,
            height=height,
            fps=fps,
            connect_timeout_sec=connect_timeout_sec,
            frame_stale_sec=frame_stale_sec,
            backoff_initial_ms=backoff_initial_ms,
            backoff_max_ms=backoff_max_ms,
            jpeg_quality=jpeg_quality,
            log_level=log_level,
            mjpeg_boundary=mjpeg_boundary,
        )
