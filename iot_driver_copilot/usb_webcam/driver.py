import os
import sys
import time
import signal
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from typing import Optional, Tuple

import cv2  # OpenCV for UVC capture and JPEG encoding

from config import load_config, Config


class FrameBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame_bytes: Optional[bytes] = None
        self._timestamp: float = 0.0
        self._seq: int = 0

    def update(self, frame_bytes: bytes, ts: float):
        with self._cond:
            self._frame_bytes = frame_bytes
            self._timestamp = ts
            self._seq += 1
            self._cond.notify_all()

    def wait_for_new(self, last_seq: int, timeout: Optional[float] = None) -> Tuple[Optional[bytes], float, int]:
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout=timeout)
            return self._frame_bytes, self._timestamp, self._seq

    def get_latest(self) -> Tuple[Optional[bytes], float, int]:
        with self._lock:
            return self._frame_bytes, self._timestamp, self._seq


class CaptureWorker(threading.Thread):
    def __init__(self, cfg: Config, buffer: FrameBuffer, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.buffer = buffer
        self.stop_event = stop_event
        self.cap: Optional[cv2.VideoCapture] = None

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        device = self.cfg.webcam_device
        cap = None
        try:
            if isinstance(device, int):
                cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
            else:
                cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        except Exception as e:
            logging.error("Failed to initialize VideoCapture: %s", e)
            return None

        if not cap or not cap.isOpened():
            if cap:
                cap.release()
            return None

        # Apply requested properties if provided
        if self.cfg.webcam_width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.cfg.webcam_width))
        if self.cfg.webcam_height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.cfg.webcam_height))
        if self.cfg.webcam_fps is not None:
            cap.set(cv2.CAP_PROP_FPS, float(self.cfg.webcam_fps))
        # Reduce buffering/latency if backend supports it
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        # Log actual settings for visibility
        try:
            actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            logging.info("UVC connected: %sx%s @ %.2f fps", int(actual_w), int(actual_h), actual_fps)
        except Exception:
            logging.info("UVC connected (unable to query properties)")

        return cap

    def run(self):
        backoff = self.cfg.reconnect_base_backoff
        last_ok_time = 0.0

        while not self.stop_event.is_set():
            if self.cap is None:
                self.cap = self._open_capture()
                if self.cap is None:
                    logging.warning(
                        "Unable to open webcam device '%s'. Retrying in %.2fs",
                        str(self.cfg.webcam_device), backoff,
                    )
                    if self.stop_event.wait(backoff):
                        break
                    backoff = min(backoff * 2.0, self.cfg.reconnect_max_backoff)
                    continue
                else:
                    logging.info("Webcam device opened successfully")
                    backoff = self.cfg.reconnect_base_backoff

            # Capture loop
            try:
                ret, frame = self.cap.read()
            except Exception as e:
                logging.error("Error reading frame: %s", e)
                ret = False
                frame = None

            if not ret or frame is None:
                logging.warning("Frame read failed. Reinitializing device...")
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
                if self.stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2.0, self.cfg.reconnect_max_backoff)
                continue

            now = time.time()
            last_ok_time = now

            # Encode JPEG with configured quality
            try:
                encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.jpeg_quality)]
                ok, enc = cv2.imencode('.jpg', frame, encode_params)
                if not ok:
                    logging.warning("JPEG encoding failed for a frame")
                    continue
                self.buffer.update(enc.tobytes(), now)
            except Exception as e:
                logging.error("JPEG encoding error: %s", e)
                continue

            # Watchdog: if no frame published for too long, force reopen (best effort)
            if self.cfg.capture_timeout_sec and (now - last_ok_time) > self.cfg.capture_timeout_sec:
                logging.warning("No frames for %.2fs, reopening device", self.cfg.capture_timeout_sec)
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None

        # Cleanup on stop
        if self.cap is not None:
            try:
                self.cap.release()
                logging.info("Webcam device released")
            except Exception:
                pass


class RequestHandler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    buffer: FrameBuffer = None  # type: ignore
    stop_event: threading.Event = None  # type: ignore

    server_version = "UVC-HTTP-Driver/1.0"

    def log_message(self, format: str, *args):
        logging.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == "/frame":
            self._handle_frame()
            return
        if path == "/stream":
            self._handle_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _wait_for_frame(self, timeout: float) -> Optional[Tuple[bytes, float, int]]:
        # Wait for a frame up to timeout seconds
        end = time.time() + max(0.0, timeout)
        last_seq = -1
        while time.time() < end and not self.stop_event.is_set():
            frame, ts, seq = self.buffer.get_latest()
            if frame is not None:
                return frame, ts, seq
            remaining = end - time.time()
            if remaining <= 0:
                break
            self.buffer.wait_for_new(last_seq, timeout=min(remaining, 0.5))
        return None

    def _handle_frame(self):
        timeout = max(self.cfg.capture_timeout_sec, 0.5)
        got = self._wait_for_frame(timeout)
        if not got:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "No frame available")
            return
        frame_bytes, ts, _ = got
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame_bytes)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("X-Timestamp", time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts)))
            self.end_headers()
            self.wfile.write(frame_bytes)
        except (BrokenPipeError, ConnectionResetError):
            logging.warning("Client disconnected during /frame response")
        except Exception as e:
            logging.error("Error serving /frame: %s", e)

    def _handle_stream(self):
        boundary = "frame"
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.end_headers()
        except Exception as e:
            logging.error("Failed to start stream response: %s", e)
            return

        last_seq = -1
        while not self.stop_event.is_set():
            # Wait for a new frame or timeout to keep-alive
            frame, ts, seq = self.buffer.get_latest()
            if seq == last_seq or frame is None:
                # Wait for notification up to 1s
                self.buffer.wait_for_new(last_seq, timeout=1.0)
                frame, ts, seq = self.buffer.get_latest()
                if frame is None:
                    continue
                if seq == last_seq:
                    # No new frame; still push the latest to keep stream flowing
                    pass

            last_seq = seq
            part_headers = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n"
                f"X-Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts))}\r\n\r\n"
            ).encode("ascii")
            try:
                self.wfile.write(part_headers)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                logging.info("Stream client disconnected")
                break
            except Exception as e:
                logging.error("Error while streaming: %s", e)
                break


def main():
    cfg = load_config()

    logging.basicConfig(
        level=cfg.log_level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    stop_event = threading.Event()
    buffer = FrameBuffer()

    worker = CaptureWorker(cfg, buffer, stop_event)
    worker.start()

    RequestHandler.cfg = cfg
    RequestHandler.buffer = buffer
    RequestHandler.stop_event = stop_event

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    def handle_signal(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        stop_event.set()
        # Shutdown HTTP server gracefully
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info("HTTP server listening on %s:%d", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        httpd.server_close()
        worker.join(timeout=5.0)
        logging.info("Driver stopped")


if __name__ == "__main__":
    main()
