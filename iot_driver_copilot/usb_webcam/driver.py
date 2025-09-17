import os
import sys
import time
import signal
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

try:
    import cv2  # OpenCV for UVC camera access and JPEG encoding
except ImportError as e:
    print("Error: OpenCV (cv2) is required. Install with: pip install opencv-python", file=sys.stderr)
    raise


class Config:
    def __init__(self):
        # Required environment variables (no defaults)
        self.http_host = self._require("HTTP_HOST")
        self.http_port = self._require_int("HTTP_PORT")

        self.camera_path = os.environ.get("CAMERA_PATH")
        self.camera_index = os.environ.get("CAMERA_INDEX")
        if not self.camera_path and self.camera_index is None:
            raise ValueError("Either CAMERA_PATH or CAMERA_INDEX must be set")
        if self.camera_index is not None:
            try:
                self.camera_index = int(self.camera_index)
            except ValueError:
                raise ValueError("CAMERA_INDEX must be an integer")

        self.backoff_initial_ms = self._require_int("BACKOFF_INITIAL_MS")
        self.backoff_max_ms = self._require_int("BACKOFF_MAX_MS")
        self.backoff_multiplier = self._require_float("BACKOFF_MULTIPLIER")
        if self.backoff_multiplier < 1.0:
            raise ValueError("BACKOFF_MULTIPLIER must be >= 1.0")

        self.stale_timeout_sec = self._require_float("STALE_TIMEOUT_SEC")
        self.loop_sleep_ms = self._require_int("LOOP_SLEEP_MS")
        self.stream_min_interval_ms = self._require_int("STREAM_MIN_INTERVAL_MS")
        self.jpeg_quality = self._require_int("JPEG_QUALITY")
        if not (1 <= self.jpeg_quality <= 100):
            raise ValueError("JPEG_QUALITY must be between 1 and 100")

        self.log_level = self._require("LOG_LEVEL").upper()
        if self.log_level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

        # Optional camera configuration
        self.frame_width = self._optional_int("FRAME_WIDTH")
        self.frame_height = self._optional_int("FRAME_HEIGHT")
        self.frame_rate = self._optional_float("FRAME_RATE")

    def _require(self, key: str) -> str:
        v = os.environ.get(key)
        if v is None or v == "":
            raise ValueError(f"Missing required environment variable: {key}")
        return v

    def _require_int(self, key: str) -> int:
        v = self._require(key)
        try:
            return int(v)
        except ValueError:
            raise ValueError(f"{key} must be an integer")

    def _require_float(self, key: str) -> float:
        v = self._require(key)
        try:
            return float(v)
        except ValueError:
            raise ValueError(f"{key} must be a float")

    def _optional_int(self, key: str) -> Optional[int]:
        v = os.environ.get(key)
        if v is None or v == "":
            return None
        try:
            return int(v)
        except ValueError:
            raise ValueError(f"{key} must be an integer if provided")

    def _optional_float(self, key: str) -> Optional[float]:
        v = os.environ.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except ValueError:
            raise ValueError(f"{key} must be a float if provided")


class VideoWorker:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="VideoWorker", daemon=True)
        self._lock = threading.Lock()
        self._last_jpeg: Optional[bytes] = None
        self._last_ts: float = 0.0
        self._cap = None  # type: Optional[cv2.VideoCapture]

    def start(self):
        self.log.info("Starting video worker loop")
        self._thread.start()

    def stop(self):
        self.log.info("Stopping video worker loop")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self.log.info("Video worker stopped")

    def get_latest_frame(self) -> Tuple[Optional[bytes], float]:
        with self._lock:
            if self._last_jpeg is None:
                return None, 0.0
            return self._last_jpeg, self._last_ts

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        if self.cfg.camera_path:
            cap = cv2.VideoCapture(self.cfg.camera_path)
            source_desc = f"path={self.cfg.camera_path}"
        else:
            cap = cv2.VideoCapture(int(self.cfg.camera_index))
            source_desc = f"index={self.cfg.camera_index}"
        if not cap or not cap.isOpened():
            self.log.warning("Failed to open camera (%s)", source_desc)
            try:
                if cap:
                    cap.release()
            except Exception:
                pass
            return None

        # Optional: apply camera parameters if provided
        if self.cfg.frame_width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.cfg.frame_width))
        if self.cfg.frame_height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.cfg.frame_height))
        if self.cfg.frame_rate is not None:
            cap.set(cv2.CAP_PROP_FPS, float(self.cfg.frame_rate))

        # Log actual settings
        actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        self.log.info("Camera opened (%s) at %.0fx%.0f @ %.2f FPS", source_desc, actual_w, actual_h, actual_fps)
        return cap

    def _run(self):
        backoff_ms = max(self.cfg.backoff_initial_ms, 0)
        last_success_ts = 0.0

        while not self._stop_event.is_set():
            # Ensure capture opened
            if self._cap is None:
                self._cap = self._open_capture()
                if self._cap is None:
                    # Backoff on connection failure
                    delay = min(backoff_ms, self.cfg.backoff_max_ms)
                    self.log.warning("Retrying camera open in %d ms (backoff)", delay)
                    self._sleep_ms(delay)
                    backoff_ms = int(min(self.cfg.backoff_max_ms, max(1, backoff_ms) * self.cfg.backoff_multiplier))
                    continue
                else:
                    self.log.info("Connected to camera")
                    backoff_ms = self.cfg.backoff_initial_ms
                    last_success_ts = time.time()

            # Read a frame
            ret, frame = self._cap.read()
            now = time.time()

            if ret and frame is not None:
                # Encode to JPEG
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.jpeg_quality)]
                    ok, buf = cv2.imencode('.jpg', frame, encode_params)
                    if ok:
                        data = buf.tobytes()
                        with self._lock:
                            self._last_jpeg = data
                            self._last_ts = now
                        last_success_ts = now
                    else:
                        self.log.warning("JPEG encode failed for captured frame")
                except Exception as e:
                    self.log.error("JPEG encode error: %s", e)
            else:
                # Read failed
                self.log.warning("Camera read failed")

            # Detect stale capture (timeout)
            if last_success_ts > 0 and (now - last_success_ts) > self.cfg.stale_timeout_sec:
                self.log.error("Capture stale for %.2fs, reconnecting camera", now - last_success_ts)
                try:
                    if self._cap is not None:
                        self._cap.release()
                except Exception:
                    pass
                self._cap = None
                # Exponential backoff before next open
                delay = min(backoff_ms, self.cfg.backoff_max_ms)
                self._sleep_ms(delay)
                backoff_ms = int(min(self.cfg.backoff_max_ms, max(1, backoff_ms) * self.cfg.backoff_multiplier))
                continue

            # Small loop sleep to avoid busy spin
            self._sleep_ms(self.cfg.loop_sleep_ms)

    def _sleep_ms(self, ms: int):
        # Sleep in small increments to allow responsive stop
        remaining = ms / 1000.0
        step = 0.05
        while remaining > 0 and not self._stop_event.is_set():
            t = min(step, remaining)
            time.sleep(t)
            remaining -= t


class RequestHandler(BaseHTTPRequestHandler):
    # These will be injected after class definition
    cfg: Config = None  # type: ignore
    worker: VideoWorker = None  # type: ignore
    log_adapter: logging.LoggerAdapter = None  # type: ignore

    server_version = "WebcamHTTPDriver/1.0"

    def log_message(self, format, *args):
        # Redirect BaseHTTPRequestHandler logging to our logger
        try:
            RequestHandler.log_adapter.info("%s - %s" % (self.address_string(), format % args))
        except Exception:
            pass

    def do_GET(self):
        if self.path == "/frame":
            self._handle_frame()
        elif self.path == "/stream":
            self._handle_stream()
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Not Found\n")

    def _handle_frame(self):
        jpeg, ts = self.worker.get_latest_frame()
        if jpeg is None:
            self.send_response(503)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(b"No frame available yet\n")
            return

        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpeg)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        try:
            self.wfile.write(jpeg)
        except BrokenPipeError:
            pass
        except Exception as e:
            RequestHandler.log_adapter.error("Error writing /frame response: %s", e)

    def _handle_stream(self):
        boundary = "frame"
        self.send_response(200)
        self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={boundary}')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()

        last_sent_ts = 0.0
        min_interval = RequestHandler.cfg.stream_min_interval_ms / 1000.0

        try:
            while True:
                jpeg, ts = self.worker.get_latest_frame()
                if jpeg is None:
                    time.sleep(0.05)
                    continue

                # Enforce minimum interval and avoid re-sending identical frames rapidly
                now = time.time()
                if ts <= last_sent_ts or (min_interval > 0 and (now - last_sent_ts) < min_interval):
                    time.sleep(0.01)
                    continue

                part_header = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode('ascii')

                try:
                    self.wfile.write(part_header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    last_sent_ts = ts if ts > 0 else now
                except BrokenPipeError:
                    break
                except Exception as e:
                    RequestHandler.log_adapter.error("Stream write error: %s", e)
                    break
        finally:
            try:
                # End boundary (not strictly necessary when connection closes)
                self.wfile.write(f"--{boundary}--\r\n".encode('ascii'))
            except Exception:
                pass


def setup_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("webcam_driver")
    logger.setLevel(getattr(logging, level))
    ch = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(fmt='%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%dT%H:%M:%S%z')
    ch.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(ch)
    return logger


def main():
    try:
        cfg = Config()
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)

    logger = setup_logging(cfg.log_level)
    logger.info("Starting USB Webcam HTTP driver")

    worker = VideoWorker(cfg, logger)
    worker.start()

    # Prepare HTTP server
    RequestHandler.cfg = cfg
    RequestHandler.worker = worker
    RequestHandler.log_adapter = logging.LoggerAdapter(logger, extra={})

    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    stopping = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        stopping.set()
        try:
            server.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        logger.info("HTTP server listening on %s:%d", cfg.http_host, cfg.http_port)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        worker.stop()
        logger.info("Driver terminated")


if __name__ == "__main__":
    main()
