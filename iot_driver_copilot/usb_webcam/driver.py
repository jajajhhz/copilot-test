import os
import sys
import time
import threading
import logging
import signal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple

import cv2

import config as cfg


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
)


class CameraWorker:
    def __init__(
        self,
        index: int,
        jpeg_quality: int,
        read_errors_before_reset: int,
        reconnect_initial_backoff_ms: int,
        reconnect_max_backoff_ms: int,
        frame_wait_timeout_sec: float,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[float] = None,
    ) -> None:
        self.index = index
        self.jpeg_quality = int(jpeg_quality)
        self.read_errors_before_reset = int(read_errors_before_reset)
        self.reconnect_initial_backoff_ms = int(reconnect_initial_backoff_ms)
        self.reconnect_max_backoff_ms = int(reconnect_max_backoff_ms)
        self.frame_wait_timeout_sec = float(frame_wait_timeout_sec)
        self.width = width
        self.height = height
        self.fps = fps

        self._cap: Optional[cv2.VideoCapture] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera-worker", daemon=True)

        self._cond = threading.Condition()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_ts: float = 0.0

    def start(self) -> None:
        logging.info("CameraWorker starting")
        self._thread.start()

    def stop(self) -> None:
        logging.info("CameraWorker stopping")
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=5.0)
        self._release_cap()
        logging.info("CameraWorker stopped")

    def _open_cap(self) -> bool:
        try:
            cap = cv2.VideoCapture(self.index, cv2.CAP_ANY)
            if not cap or not cap.isOpened():
                logging.error("Failed to open camera index %s", self.index)
                if cap:
                    cap.release()
                return False

            # Apply optional settings if provided via env
            if self.width is not None:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
            if self.height is not None:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
            if self.fps is not None:
                cap.set(cv2.CAP_PROP_FPS, float(self.fps))

            self._cap = cap
            logging.info("Connected to camera index %s", self.index)
            return True
        except Exception as e:
            logging.exception("Exception opening camera: %s", e)
            return False

    def _release_cap(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            logging.info("Camera released")

    def _run(self) -> None:
        backoff_ms = self.reconnect_initial_backoff_ms
        while not self._stop.is_set():
            if self._cap is None:
                if not self._open_cap():
                    # Exponential backoff on connect failure
                    logging.warning(
                        "Reconnect in %d ms (initial=%d, max=%d)",
                        backoff_ms, self.reconnect_initial_backoff_ms, self.reconnect_max_backoff_ms,
                    )
                    self._stop.wait(backoff_ms / 1000.0)
                    backoff_ms = min(backoff_ms * 2, self.reconnect_max_backoff_ms)
                    continue
                else:
                    backoff_ms = self.reconnect_initial_backoff_ms

            # Read frames
            consecutive_errors = 0
            while not self._stop.is_set() and self._cap is not None:
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    consecutive_errors += 1
                    if consecutive_errors >= self.read_errors_before_reset:
                        logging.error(
                            "Too many read errors (%d), resetting camera",
                            consecutive_errors,
                        )
                        self._release_cap()
                        break
                    else:
                        time.sleep(0.01)
                        continue

                # Got frame, reset error counter
                consecutive_errors = 0

                # Encode JPEG
                try:
                    ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                    if not ok:
                        logging.warning("Failed to encode frame to JPEG")
                        continue
                    jpeg_bytes = buf.tobytes()
                except Exception as e:
                    logging.exception("JPEG encoding error: %s", e)
                    continue

                ts = time.time()
                with self._cond:
                    self._latest_jpeg = jpeg_bytes
                    self._latest_ts = ts
                    self._cond.notify_all()

        # Ensure released on exit
        self._release_cap()

    def get_latest(self, wait_timeout: Optional[float] = None) -> Optional[Tuple[bytes, float]]:
        # Returns latest JPEG and timestamp, waiting up to wait_timeout if none available yet.
        timeout = self.frame_wait_timeout_sec if wait_timeout is None else wait_timeout
        end_time = time.monotonic() + timeout
        with self._cond:
            while self._latest_jpeg is None and not self._stop.is_set():
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            if self._latest_jpeg is None:
                return None
            return self._latest_jpeg, self._latest_ts

    def wait_for_next_after(self, after_ts: float, max_wait: float = 1.0) -> Optional[Tuple[bytes, float]]:
        # Wait until there is a frame with ts > after_ts, up to max_wait seconds.
        end_time = time.monotonic() + max_wait
        with self._cond:
            while self._latest_ts <= after_ts and not self._stop.is_set():
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            if self._latest_jpeg is None:
                return None
            return self._latest_jpeg, self._latest_ts


def make_handler(camera: CameraWorker, frame_wait_timeout_sec: float, stop_event: threading.Event):
    boundary = b"frameboundary"

    class Handler(BaseHTTPRequestHandler):
        server_version = "WebcamHTTP/1.0"

        def log_message(self, format: str, *args) -> None:
            logging.info("%s - - " + format, self.client_address[0], *args)

        def do_GET(self):
            try:
                if self.path == "/frame":
                    self._handle_frame()
                elif self.path == "/stream":
                    self._handle_stream()
                else:
                    self.send_error(404, "Not Found")
            except Exception as e:
                logging.exception("HTTP handler error: %s", e)
                # Try to prevent half-written responses
                try:
                    if not self.wfile.closed:
                        self.wfile.flush()
                except Exception:
                    pass

        def _handle_frame(self):
            res = camera.get_latest(wait_timeout=frame_wait_timeout_sec)
            if res is None:
                self.send_error(504, "Timeout waiting for frame")
                return
            jpeg_bytes, ts = res
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(jpeg_bytes)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.end_headers()
            try:
                self.wfile.write(jpeg_bytes)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                logging.warning("Client disconnected during /frame")

        def _handle_stream(self):
            self.send_response(200)
            self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={boundary.decode()}')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.end_headers()

            last_ts = 0.0
            while not stop_event.is_set():
                try:
                    res = camera.wait_for_next_after(last_ts, max_wait=1.0)
                    if res is None:
                        # Periodic keep-alive or continue waiting
                        continue
                    jpeg_bytes, ts = res
                    last_ts = ts

                    part_headers = [
                        b"--" + boundary + b"\r\n",
                        b"Content-Type: image/jpeg\r\n",
                        b"Content-Length: " + str(len(jpeg_bytes)).encode('ascii') + b"\r\n\r\n",
                    ]
                    for h in part_headers:
                        self.wfile.write(h)
                    self.wfile.write(jpeg_bytes)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    logging.info("/stream client disconnected")
                    break
                except Exception as e:
                    logging.exception("Error during /stream: %s", e)
                    break

    return Handler


def main() -> None:
    try:
        conf = cfg.load_config()
    except Exception as e:
        logging.error("Configuration error: %s", e)
        sys.exit(2)

    camera = CameraWorker(
        index=conf.camera_index,
        jpeg_quality=conf.jpeg_quality,
        read_errors_before_reset=conf.read_errors_before_reset,
        reconnect_initial_backoff_ms=conf.reconnect_initial_backoff_ms,
        reconnect_max_backoff_ms=conf.reconnect_max_backoff_ms,
        frame_wait_timeout_sec=conf.frame_wait_timeout_sec,
        width=conf.width,
        height=conf.height,
        fps=conf.fps,
    )
    camera.start()

    stop_event = threading.Event()

    handler_cls = make_handler(camera, conf.frame_wait_timeout_sec, stop_event)
    server = ThreadingHTTPServer((conf.http_host, conf.http_port), handler_cls)

    def handle_signal(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info("HTTP server listening on %s:%d", conf.http_host, conf.http_port)

    try:
        server.serve_forever()
    finally:
        logging.info("HTTP server stopping")
        stop_event.set()
        camera.stop()
        server.server_close()
        logging.info("Shutdown complete")


if __name__ == '__main__':
    main()
