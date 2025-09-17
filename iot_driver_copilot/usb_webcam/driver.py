import os
import sys
import time
import threading
import signal
import io
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver

try:
    import cv2  # OpenCV is required for UVC access
    import numpy as np  # used implicitly by cv2.imencode
except Exception as e:
    print(f"[FATAL] Missing dependency: {e}. Please install requirements with 'pip install -r requirements.txt'", flush=True)
    sys.exit(1)

from config import load_config


def log(msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    print(f"[{ts}] {msg}", flush=True)


class FrameBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame = None  # bytes (JPEG)
        self._ts = 0.0

    def set_frame(self, data: bytes, ts: float):
        with self._cond:
            self._frame = data
            self._ts = ts
            self._cond.notify_all()

    def get_latest(self, timeout: float):
        deadline = time.time() + timeout if timeout is not None and timeout > 0 else None
        with self._cond:
            while self._frame is None:
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None, 0.0
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()
            return self._frame, self._ts

    def wait_for_next(self, after_ts: float, timeout: float):
        deadline = time.time() + timeout if timeout is not None and timeout > 0 else None
        with self._cond:
            while True:
                if self._frame is not None and self._ts > after_ts:
                    return self._frame, self._ts
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None, 0.0
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()


class CameraWorker(threading.Thread):
    def __init__(self, cfg, buffer: FrameBuffer, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.buffer = buffer
        self.stop_event = stop_event
        self._cap = None

    def _open_capture(self):
        device = self.cfg.device
        cap = None
        backend = cv2.CAP_ANY
        try:
            # Try to prefer V4L2 on Linux if available
            if sys.platform.startswith('linux'):
                backend = cv2.CAP_V4L2
        except Exception:
            backend = cv2.CAP_ANY
        try:
            if isinstance(device, int):
                cap = cv2.VideoCapture(device, backend)
            else:
                cap = cv2.VideoCapture(device, backend)
        except Exception as e:
            log(f"[camera] Error creating VideoCapture: {e}")
            cap = None
        return cap

    def _configure_capture(self, cap):
        # Apply requested properties if provided
        if self.cfg.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.cfg.width))
        if self.cfg.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.cfg.height))
        if self.cfg.fps:
            cap.set(cv2.CAP_PROP_FPS, float(self.cfg.fps))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # Read back actuals
        try:
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
        except Exception:
            actual_w = actual_h = 0
            actual_fps = 0.0
        log(f"[camera] Opened device with resolution {actual_w}x{actual_h} @ {actual_fps:.2f}fps")

    def _release(self):
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None

    def run(self):
        backoff = self.cfg.backoff_base
        while not self.stop_event.is_set():
            try:
                log("[camera] Connecting to device...")
                self._cap = self._open_capture()
                if self._cap is None or not self._cap.isOpened():
                    raise RuntimeError("Unable to open camera device")
                self._configure_capture(self._cap)
                log("[camera] Connected.")
                backoff = self.cfg.backoff_base  # reset backoff on success

                last_log = 0.0
                while not self.stop_event.is_set():
                    ok, frame = self._cap.read()
                    if not ok or frame is None:
                        raise RuntimeError("Failed to read frame from camera")
                    ts = time.time()
                    # Encode JPEG
                    try:
                        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.jpeg_quality)]
                        ok, jpg = cv2.imencode('.jpg', frame, params)
                        if not ok:
                            raise RuntimeError("cv2.imencode returned False")
                        self.buffer.set_frame(jpg.tobytes(), ts)
                    except Exception as e:
                        log(f"[camera] JPEG encode error: {e}")
                        # Continue reading; if persistent, read loop may break on timeout logic
                        continue

                    # Optional periodic log for heartbeat
                    if ts - last_log >= 10.0:
                        last_log = ts
                        log(f"[camera] Frame updated at {time.strftime('%H:%M:%S', time.localtime(ts))}")

                    # Read timeout handling: if no frame pushed for too long (unlikely here), break
                    # Here, since we push every loop, we don't need explicit timeout check.

            except Exception as e:
                log(f"[camera] Error: {e}")
                self._release()
                if self.stop_event.is_set():
                    break
                sleep_s = min(self.cfg.backoff_max, backoff)
                log(f"[camera] Reconnecting in {sleep_s:.2f}s (backoff)")
                self.stop_event.wait(sleep_s)
                backoff = min(self.cfg.backoff_max, max(self.cfg.backoff_base, backoff * 2))
                continue

        self._release()
        log("[camera] Stopped.")


class MJPEGHandler(BaseHTTPRequestHandler):
    buffer: FrameBuffer = None
    cfg = None
    stop_event: threading.Event = None

    server_version = "USBWebcamHTTP/1.0"

    def do_GET(self):
        if self.path == "/frame":
            self.handle_frame()
        elif self.path == "/stream":
            self.handle_stream()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def handle_frame(self):
        data, ts = self.buffer.get_latest(timeout=self.cfg.read_timeout)
        if data is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "No frame available")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
        except BrokenPipeError:
            pass
        except Exception as e:
            log(f"[http] /frame error: {e}")

    def handle_stream(self):
        boundary = "frame"
        # Ensure we have at least one frame available to start
        data, ts = self.buffer.get_latest(timeout=self.cfg.read_timeout)
        if data is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "No frame available to start stream")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
        except Exception as e:
            log(f"[http] Failed to send stream headers: {e}")
            return

        last_ts = 0.0
        # Write initial frame immediately
        try:
            part = self._mjpeg_part(boundary, data)
            self.wfile.write(part)
            last_ts = ts
        except BrokenPipeError:
            return
        except Exception as e:
            log(f"[http] /stream write error (initial): {e}")
            return

        while not self.stop_event.is_set():
            try:
                data, ts = self.buffer.wait_for_next(after_ts=last_ts, timeout=self.cfg.read_timeout)
                if data is None:
                    # No new frame within timeout, end stream so client can reconnect
                    break
                part = self._mjpeg_part(boundary, data)
                self.wfile.write(part)
                last_ts = ts
            except BrokenPipeError:
                break
            except Exception as e:
                log(f"[http] /stream write error: {e}")
                break

    def _mjpeg_part(self, boundary: str, data: bytes) -> bytes:
        headers = (
            f"--{boundary}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(data)}\r\n\r\n"
        ).encode('ascii')
        trailer = b"\r\n"
        return headers + data + trailer

    def log_message(self, format, *args):
        # Keep default logging concise
        log(f"[http] {self.client_address[0]} - {format % args}")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_device_env(device_str: str):
    if device_str is None:
        return 0
    ds = device_str.strip()
    if ds == "":
        return 0
    # If it's an integer index, return int
    try:
        return int(ds)
    except ValueError:
        return ds  # path string


def main():
    cfg = load_config()
    cfg.device = parse_device_env(cfg.device)

    buffer = FrameBuffer()
    stop_event = threading.Event()

    cam_worker = CameraWorker(cfg, buffer, stop_event)
    cam_worker.start()

    handler_cls = MJPEGHandler
    handler_cls.buffer = buffer
    handler_cls.cfg = cfg
    handler_cls.stop_event = stop_event

    httpd = ThreadedHTTPServer((cfg.http_host, cfg.http_port), handler_cls)

    def shutdown(signum=None, frame=None):
        log(f"[main] Shutting down (signal {signum})...")
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log(f"[main] HTTP server listening on http://{cfg.http_host}:{cfg.http_port}")
    log(f"[main] Endpoints: GET /frame, GET /stream")

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        try:
            httpd.server_close()
        except Exception:
            pass
        cam_worker.join(timeout=5.0)
        log("[main] Shutdown complete.")


if __name__ == "__main__":
    main()
