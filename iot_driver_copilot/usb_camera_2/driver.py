import os
import io
import threading
import time
from typing import Optional, Tuple

from flask import Flask, Response, request, jsonify, send_file

import cv2
import numpy as np

# ========== Config from environment variables ==========
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DEFAULT_CAMERA_ID = int(os.environ.get("CAMERA_ID", "0"))
DEFAULT_RESOLUTION = (
    int(os.environ.get("CAMERA_WIDTH", "640")),
    int(os.environ.get("CAMERA_HEIGHT", "480")),
)
DEFAULT_FORMAT = os.environ.get("CAMERA_FORMAT", "MJPEG")  # JPEG, PNG, MP4, MJPEG
DEFAULT_FRAME_RATE = int(os.environ.get("CAMERA_FRAME_RATE", "15"))

# ========== Camera Manager ==========

class CameraManager:
    def __init__(self):
        self.cameras = self._enumerate_cameras()
        self.active_cam_id = self.cameras[0] if self.cameras else 0
        self.cap = None
        self.lock = threading.Lock()
        self.is_streaming = False
        self.format = DEFAULT_FORMAT.upper()
        self.resolution = DEFAULT_RESOLUTION
        self.frame_rate = DEFAULT_FRAME_RATE
        self.last_frame = None
        self.stream_thread = None
        self.stop_stream_flag = threading.Event()
        self._init_camera(self.active_cam_id)

    def _enumerate_cameras(self, max_tested=10):
        ids = []
        for idx in range(max_tested):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            if cap.read()[0]:
                ids.append(idx)
            cap.release()
        if not ids:
            ids = [0]  # fallback for default
        return ids

    def switch_camera(self, cam_id: int):
        with self.lock:
            if self.cap:
                self.cap.release()
            self._init_camera(cam_id)

    def _init_camera(self, cam_id: int):
        self.cap = cv2.VideoCapture(cam_id, cv2.CAP_DSHOW if os.name == 'nt' else 0)
        # Try to set resolution
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self.cap.set(cv2.CAP_PROP_FPS, self.frame_rate)
        self.active_cam_id = cam_id
        time.sleep(0.2)  # Let camera warm up

    def start_camera(self, resolution=None, fmt=None, frame_rate=None):
        with self.lock:
            if resolution:
                self.resolution = tuple(resolution)
            if frame_rate:
                self.frame_rate = frame_rate
            if fmt:
                self.format = fmt.upper()
            self.switch_camera(self.active_cam_id)
            self.is_streaming = True
            self.stop_stream_flag.clear()

    def stop_camera(self):
        with self.lock:
            self.is_streaming = False
            self.stop_stream_flag.set()
            if self.cap:
                self.cap.release()
                self.cap = None

    def set_resolution(self, width: int, height: int):
        with self.lock:
            self.resolution = (width, height)
            if self.cap:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def set_format(self, fmt: str):
        with self.lock:
            self.format = fmt.upper()

    def get_frame(self) -> Optional[np.ndarray]:
        with self.lock:
            if self.cap is None:
                self._init_camera(self.active_cam_id)
            ret, frame = self.cap.read()
            if not ret:
                return None
            self.last_frame = frame
            return frame

    def capture_image(self, fmt: str = 'JPEG', resolution: Tuple[int, int] = None) -> Tuple[bytes, str]:
        frame = self.get_frame()
        if resolution:
            frame = cv2.resize(frame, resolution)
        fmt = fmt.upper()
        if fmt == 'PNG':
            ret, buf = cv2.imencode('.png', frame)
            content_type = 'image/png'
        else:
            ret, buf = cv2.imencode('.jpg', frame)
            content_type = 'image/jpeg'
        if not ret:
            raise RuntimeError("Failed to encode image")
        return buf.tobytes(), content_type

    def stream_generator(self, fmt='MJPEG', resolution=None):
        fmt = fmt.upper()
        while not self.stop_stream_flag.is_set():
            frame = self.get_frame()
            if frame is None:
                continue
            if resolution:
                frame = cv2.resize(frame, resolution)
            if fmt == 'PNG':
                ret, buf = cv2.imencode('.png', frame)
                img_bytes = buf.tobytes()
                content_type = 'image/png'
            else:
                ret, buf = cv2.imencode('.jpg', frame)
                img_bytes = buf.tobytes()
                content_type = 'image/jpeg'
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + img_bytes + b'\r\n')
            time.sleep(1.0 / self.frame_rate)

    def record_video(self, duration: float, fmt='MP4', resolution=None, frame_rate=None) -> Tuple[bytes, str]:
        fmt = fmt.upper()
        if not resolution:
            resolution = self.resolution
        if not frame_rate:
            frame_rate = self.frame_rate
        ext = '.mp4' if fmt == 'MP4' else '.avi'
        fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'MP4' else 'MJPG'))
        temp_file = 'temp_record' + ext
        writer = cv2.VideoWriter(temp_file, fourcc, frame_rate, resolution)
        t_end = time.time() + min(duration, 60)
        frames_written = 0
        while time.time() < t_end:
            frame = self.get_frame()
            if frame is None:
                continue
            if resolution:
                frame = cv2.resize(frame, resolution)
            writer.write(frame)
            frames_written += 1
            time.sleep(1.0 / frame_rate)
        writer.release()
        with open(temp_file, 'rb') as f:
            video_bytes = f.read()
        os.remove(temp_file)
        if fmt == 'MP4':
            return video_bytes, 'video/mp4'
        else:
            return video_bytes, 'video/x-motion-jpeg'

    def list_cameras(self):
        return self.cameras

# ========== Flask HTTP API ==========

app = Flask(__name__)
cam_mgr = CameraManager()

@app.route('/cam/start', methods=['POST'])
def start_camera():
    params = request.args
    data = request.get_json(silent=True) or {}
    width = int(params.get('width', data.get('width', cam_mgr.resolution[0])))
    height = int(params.get('height', data.get('height', cam_mgr.resolution[1])))
    fmt = params.get('format', data.get('format', cam_mgr.format))
    frame_rate = int(params.get('frame_rate', data.get('frame_rate', cam_mgr.frame_rate)))
    cam_id = int(params.get('cam_id', data.get('cam_id', cam_mgr.active_cam_id)))
    if cam_id not in cam_mgr.list_cameras():
        return jsonify({"error": f"Camera ID {cam_id} not found"}), 404
    cam_mgr.active_cam_id = cam_id
    cam_mgr.start_camera(resolution=(width, height), fmt=fmt, frame_rate=frame_rate)
    return jsonify({"status": "started", "cam_id": cam_id, "resolution": [width, height], "format": fmt, "frame_rate": frame_rate})

@app.route('/cam/stop', methods=['POST'])
def stop_camera():
    cam_mgr.stop_camera()
    return jsonify({"status": "stopped"})

@app.route('/cam/capture', methods=['GET'])
def capture_image():
    fmt = request.args.get('format', cam_mgr.format)
    width = request.args.get('width', cam_mgr.resolution[0])
    height = request.args.get('height', cam_mgr.resolution[1])
    try:
        width, height = int(width), int(height)
    except ValueError:
        width, height = cam_mgr.resolution
    img_bytes, content_type = cam_mgr.capture_image(fmt, (width, height))
    return Response(img_bytes, mimetype=content_type)

@app.route('/cam/stream', methods=['GET'])
def stream_video():
    fmt = request.args.get('format', cam_mgr.format)
    width = request.args.get('width', cam_mgr.resolution[0])
    height = request.args.get('height', cam_mgr.resolution[1])
    try:
        width, height = int(width), int(height)
    except ValueError:
        width, height = cam_mgr.resolution
    cam_mgr.start_camera(resolution=(width, height), fmt=fmt)
    return Response(cam_mgr.stream_generator(fmt, (width, height)),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/cam/record', methods=['POST'])
def record_video():
    data = request.get_json(force=True)
    duration = float(data.get('duration', 10))
    duration = min(duration, 60)
    fmt = data.get('format', cam_mgr.format)
    width = int(data.get('width', cam_mgr.resolution[0]))
    height = int(data.get('height', cam_mgr.resolution[1]))
    frame_rate = int(data.get('frame_rate', cam_mgr.frame_rate))
    cam_mgr.start_camera(resolution=(width, height), fmt=fmt, frame_rate=frame_rate)
    video_bytes, content_type = cam_mgr.record_video(duration, fmt, (width, height), frame_rate)
    return Response(video_bytes, mimetype=content_type,
                    headers={"Content-Disposition": f"attachment; filename=recording.{fmt.lower()}"})

@app.route('/cam/res', methods=['PUT'])
def set_resolution():
    data = request.get_json(force=True)
    width = int(data.get('width', cam_mgr.resolution[0]))
    height = int(data.get('height', cam_mgr.resolution[1]))
    cam_mgr.set_resolution(width, height)
    return jsonify({"status": "ok", "resolution": [width, height]})

@app.route('/cam/form', methods=['PUT'])
def set_format():
    data = request.get_json(force=True)
    fmt = data.get('format', cam_mgr.format)
    cam_mgr.set_format(fmt)
    return jsonify({"status": "ok", "format": fmt})

@app.route('/cam/list', methods=['GET'])
def list_cameras():
    return jsonify({"cameras": cam_mgr.list_cameras(), "active": cam_mgr.active_cam_id})

@app.route('/cam/switch', methods=['POST'])
def switch_camera():
    data = request.get_json(force=True)
    cam_id = int(data.get('cam_id', cam_mgr.active_cam_id))
    if cam_id not in cam_mgr.list_cameras():
        return jsonify({"error": f"Camera ID {cam_id} not found"}), 404
    cam_mgr.switch_camera(cam_id)
    return jsonify({"status": "ok", "active_cam_id": cam_id})

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)