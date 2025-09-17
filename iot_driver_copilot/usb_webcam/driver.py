import os
import time
import threading
import logging
import signal
import atexit
from typing import Optional, Tuple

from flask import Flask, Response, jsonify, make_response

import cv2

import config as cfg


class FrameBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame_bytes: Optional[bytes] = None
        self._timestamp: float = 0.0
        self._stopped = False

    def update(self, frame_bytes: bytes, ts: float):
        with self._cond:
            self._frame_bytes = frame_bytes
            self._timestamp = ts
            self._cond.notify_all()

    def get_latest(self) -> Optional[Tuple[bytes, float]]:
        with self._lock:
            if self._frame_bytes is None:
                return None
            return self._frame_bytes, self._timestamp

    def wait_for_newer(self, after_ts: float, timeout: Optional[float]) -> Optional[Tuple[bytes, float]]:
        end = None if timeout is None else (time.time() + timeout)
        with self._cond:
            while not self._stopped:
                if self._timestamp > after_ts and self._frame_bytes is not None:
                    return self._frame_bytes, self._timestamp
                if timeout is None:
                    self._cond.wait()
                else:
                    remaining = end - time.time()
                    if remaining <= 0:
                        return None
                    self._cond.wait(remaining)
            return None

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify_all()


class CameraWorker:
    def __init__(self, conf: cfg.Config, buffer: FrameBuffer):
        self.conf = conf
        self.buffer = buffer
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cap: Optional[cv2.VideoCapture] = None
        self._connected = False
        self._last_update_log = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="CameraWorker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self.buffer.stop()
        if self._thread:
            self._thread.join(timeout=5)
        self._release_cap()

    def _open_cap(self) -> bool:
        device = self.conf.camera_device
        cap = None
        try:
            if isinstance(device, int):
                cap = cv2.VideoCapture(device)
            else:
                cap = cv2.VideoCapture(device)
        except Exception as e:
            logging.error(f"Error creating VideoCapture: {e}")
            return False

        if not cap or not cap.isOpened():
            if cap:
                cap.release()
            logging.warning("Failed to open camera device")
            return False

        # Apply settings if provided
        if self.conf.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.conf.width))
        if self.conf.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.conf.height))
        if self.conf.fps:
            cap.set(cv2.CAP_PROP_FPS, float(self.conf.fps))

        # Warmup / verify we can read within timeout
        start = time.time()
        ok = False
        while time.time() - start < self.conf.connect_timeout_sec and not self._stop_event.is_set():
            ret, frame = cap.read()
            if ret and frame is not None:
                ok = True
                break
            time.sleep(0.05)
        if not ok:
            logging.error("Camera open timed out waiting for frame")
            cap.release()
            return False

        self._cap = cap
        self._connected = True
        logging.info("Camera connected")
        return True

    def _release_cap(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._connected:
            logging.info("Camera disconnected")
        self._connected = False

    def _run(self):
        backoff = self.conf.backoff_initial_ms / 1000.0
        last_frame_time = 0.0
        while not self._stop_event.is_set():
            # Ensure camera is open
            if self._cap is None or not self._connected:
                if self._stop_event.is_set():
                    break
                if self._open_cap():
                    backoff = self.conf.backoff_initial_ms / 1000.0
                else:
                    logging.warning(f"Retrying camera open in {backoff:.2f}s")
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, self.conf.backoff_max_ms / 1000.0)
                    continue

            # Read a frame
            ret, frame = (False, None)
            try:
                ret, frame = self._cap.read()
            except Exception as e:
                logging.error(f"Error reading frame: {e}")
                ret = False

            now = time.time()
            if ret and frame is not None:
                # Encode JPEG once for both endpoints
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.conf.jpeg_quality)]
                    ok, buf = cv2.imencode(".jpg", frame, encode_params)
                    if ok:
                        frame_bytes = buf.tobytes()
                        self.buffer.update(frame_bytes, now)
                        last_frame_time = now
                        # Periodic last-update log (no more than 1 log per 5 seconds)
                        if now - self._last_update_log >= 5.0:
                            logging.info(f"Last frame at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
                            self._last_update_log = now
                    else:
                        logging.warning("JPEG encoding failed")
                except Exception as e:
                    logging.error(f"JPEG encode error: {e}")

                # Pace loop roughly to FPS if provided
                if self.conf.fps and self.conf.fps > 0:
                    target_dt = 1.0 / float(self.conf.fps)
                    elapsed = time.time() - now
                    if elapsed < target_dt:
                        self._stop_event.wait(target_dt - elapsed)
            else:
                # Failed read; check staleness and possibly reconnect
                if last_frame_time == 0:
                    last_frame_time = now
                if now - last_frame_time >= self.conf.frame_stale_sec:
                    logging.warning("Frame stale; attempting to reconnect camera")
                    self._release_cap()
                    # Exponential backoff on reconnect
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, self.conf.backoff_max_ms / 1000.0)
                else:
                    # Short wait before next read attempt
                    self._stop_event.wait(0.05)

        # Cleanup
        self._release_cap()


def create_app(conf: cfg.Config, cam: CameraWorker, buffer: FrameBuffer) -> Flask:
    app = Flask(__name__)

    @app.route("/frame", methods=["GET"])
    def frame():
        item = buffer.get_latest()
        if not item:
            resp = make_response("no frame available yet", 503)
            resp.headers["Content-Type"] = "text/plain"
            return resp
        frame_bytes, ts = item
        resp = make_response(frame_bytes)
        resp.headers["Content-Type"] = "image/jpeg"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Timestamp"] = str(ts)
        return resp

    @app.route("/stream", methods=["GET"])
    def stream():
        def generate():
            boundary = conf.mjpeg_boundary.encode()
            last_ts = 0.0
            while True:
                item = buffer.wait_for_newer(last_ts, timeout=10.0)
                if item is None:
                    # Keep connection alive even if no new frames
                    yield b"--" + boundary + b"\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n\r\n"
                    continue
                frame_bytes, ts = item
                last_ts = ts
                headers = (
                    b"--" + boundary + b"\r\n" +
                    b"Content-Type: image/jpeg\r\n" +
                    f"Content-Length: {len(frame_bytes)}\r\n".encode() +
                    b"Cache-Control: no-store\r\n\r\n"
                )
                try:
                    yield headers + frame_bytes + b"\r\n"
                except GeneratorExit:
                    break
                except Exception:
                    break
        return Response(generate(), mimetype=f"multipart/x-mixed-replace; boundary={conf.mjpeg_boundary}")

    return app


def setup_logging(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main():
    conf = cfg.Config.from_env()
    setup_logging(conf.log_level)

    buffer = FrameBuffer()
    cam = CameraWorker(conf, buffer)

    cam.start()

    def shutdown_handler(signum, frame):
        logging.info(f"Received signal {signum}, shutting down...")
        cam.stop()
        # Flask dev server will exit on signal automatically

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    atexit.register(cam.stop)

    app = create_app(conf, cam, buffer)
    logging.info(f"Starting HTTP server on {conf.http_host}:{conf.http_port}")
    app.run(host=conf.http_host, port=conf.http_port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
