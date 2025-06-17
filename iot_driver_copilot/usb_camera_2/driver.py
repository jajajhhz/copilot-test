import os
import io
import cv2
import time
import threading
import numpy as np
from flask import Flask, Response, request, jsonify, send_file

app = Flask(__name__)

# Configuration from environment variables
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
CAMERA_IDS = os.environ.get("CAMERA_IDS", "")  # e.g., "0,1,2"
DEFAULT_CAMERA_ID = int(os.environ.get("DEFAULT_CAMERA_ID", "0"))
DEFAULT_WIDTH = int(os.environ.get("DEFAULT_WIDTH", "640"))
DEFAULT_HEIGHT = int(os.environ.get("DEFAULT_HEIGHT", "480"))
DEFAULT_FORMAT = os.environ.get("DEFAULT_FORMAT", "MJPEG")
MAX_RECORD_DURATION = int(os.environ.get("MAX_RECORD_DURATION", "60"))

SUPPORTED_FORMATS = ['JPEG', 'PNG', 'MP4', 'MJPEG']

def list_available_cameras(max_cameras=10):
    available = []
    for i in range(max_cameras):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            available.append(i)
            cap.release()
    return available

class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cameras = {}
        self.current_cam_id = None
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.format = DEFAULT_FORMAT.upper() if DEFAULT_FORMAT.upper() in SUPPORTED_FORMATS else "MJPEG"
        self.is_streaming = False
        self.is_recording = False
        self.recording_thread = None
        self.recording_file = None
        self.recording_stop_event = threading.Event()
        self.available_cameras = list_available_cameras()
        self.open_camera(self.available_cameras[0] if self.available_cameras else DEFAULT_CAMERA_ID)

    def list_cameras(self):
        return list_available_cameras()

    def open_camera(self, cam_id):
        with self.lock:
            if self.cameras.get(cam_id):
                self.current_cam_id = cam_id
                return True
            if cam_id in self.cameras:
                self.cameras[cam_id].release()
                del self.cameras[cam_id]
            cap = cv2.VideoCapture(cam_id)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cameras[cam_id] = cap
                self.current_cam_id = cam_id
                return True
            else:
                return False

    def get_current_camera(self):
        with self.lock:
            cam = self.cameras.get(self.current_cam_id)
            if cam and cam.isOpened():
                return cam
            # Try default
            if self.open_camera(DEFAULT_CAMERA_ID):
                return self.cameras[DEFAULT_CAMERA_ID]
            # Try any
            cams = self.list_cameras()
            for cam_id in cams:
                if self.open_camera(cam_id):
                    return self.cameras[cam_id]
            return None

    def set_resolution(self, width, height):
        with self.lock:
            self.width = width
            self.height = height
            cam = self.get_current_camera()
            if cam:
                cam.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cam.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            return True

    def set_format(self, fmt):
        fmt = fmt.upper()
        if fmt not in SUPPORTED_FORMATS:
            return False
        with self.lock:
            self.format = fmt
        return True

    def switch_camera(self, cam_id):
        with self.lock:
            return self.open_camera(cam_id)

    def start(self, **kwargs):
        width = int(kwargs.get('width', self.width))
        height = int(kwargs.get('height', self.height))
        fmt = kwargs.get('format', self.format).upper()
        cam_id = int(kwargs.get('camera_id', self.current_cam_id))
        self.set_resolution(width, height)
        if fmt in SUPPORTED_FORMATS:
            self.set_format(fmt)
        self.open_camera(cam_id)
        return True

    def stop(self):
        with self.lock:
            for cam in self.cameras.values():
                cam.release()
            self.cameras.clear()
            self.current_cam_id = None
            self.is_streaming = False
            self.is_recording = False

    def capture_frame(self, image_format=None, width=None, height=None):
        cam = self.get_current_camera()
        if cam is None:
            return None, "Camera not found"
        ret, frame = cam.read()
        if not ret:
            return None, "Failed to capture frame"
        if width and height:
            frame = cv2.resize(frame, (width, height))
        fmt = (image_format or self.format).upper()
        if fmt not in SUPPORTED_FORMATS:
            fmt = "JPEG"
        ext = '.jpg' if fmt == 'JPEG' else ('.png' if fmt == 'PNG' else '.jpg')
        ret, buf = cv2.imencode(ext, frame)
        if not ret:
            return None, "Failed to encode image"
        return buf.tobytes(), None

    def stream_generator(self, width=None, height=None, fmt=None):
        cam = self.get_current_camera()
        if cam is None:
            return
        fmt = (fmt or self.format).upper()
        if fmt not in ['MJPEG', 'JPEG']:
            fmt = 'MJPEG'
        while True:
            ret, frame = cam.read()
            if not ret:
                break
            if width and height:
                frame = cv2.resize(frame, (width, height))
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(0.04)  # ~25fps

    def record_video(self, duration, width=None, height=None, fmt=None):
        cam = self.get_current_camera()
        if cam is None:
            return None, "Camera not found"
        width = int(width or self.width)
        height = int(height or self.height)
        fmt = (fmt or self.format).upper()
        fourcc = cv2.VideoWriter_fourcc(*'mp4v') if fmt == 'MP4' else cv2.VideoWriter_fourcc(*'MJPG')
        suffix = '.mp4' if fmt == 'MP4' else '.avi'
        temp_filename = f"record_{int(time.time())}{suffix}"
        out = cv2.VideoWriter(temp_filename, fourcc, 20.0, (width, height))
        start_time = time.time()
        while time.time() - start_time < duration:
            ret, frame = cam.read()
            if not ret:
                break
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            out.write(frame)
        out.release()
        return temp_filename, None

camera_manager = CameraManager()

@app.route("/cam/start", methods=["POST"])
def cam_start():
    args = request.args
    cam_id = args.get("camera_id")
    width = args.get("width")
    height = args.get("height")
    fmt = args.get("format")
    ok = camera_manager.start(camera_id=cam_id, width=width, height=height, format=fmt)
    if ok:
        return jsonify({"success": True, "current_camera_id": camera_manager.current_cam_id}), 200
    else:
        return jsonify({"success": False, "error": "Failed to start camera"}), 500

@app.route("/cam/stop", methods=["POST"])
def cam_stop():
    camera_manager.stop()
    return jsonify({"success": True})

@app.route("/cam/capture", methods=["GET"])
def cam_capture():
    width = request.args.get("width", type=int)
    height = request.args.get("height", type=int)
    fmt = request.args.get("format", default=None, type=str)
    data, err = camera_manager.capture_frame(image_format=fmt, width=width, height=height)
    if data is None:
        return jsonify({"success": False, "error": err}), 500
    ext = 'jpg' if (fmt or camera_manager.format).upper() == 'JPEG' else 'png'
    return Response(data, mimetype=f"image/{ext}")

@app.route("/cam/stream", methods=["GET"])
def cam_stream():
    width = request.args.get("width", type=int)
    height = request.args.get("height", type=int)
    fmt = request.args.get("format", default="MJPEG", type=str)
    if fmt.upper() not in ['MJPEG', 'JPEG']:
        fmt = 'MJPEG'
    return Response(camera_manager.stream_generator(width=width, height=height, fmt=fmt),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/cam/record", methods=["POST"])
def cam_record():
    content = request.get_json(force=True)
    duration = min(int(content.get("duration", 10)), MAX_RECORD_DURATION)
    width = content.get("width", camera_manager.width)
    height = content.get("height", camera_manager.height)
    fmt = content.get("format", camera_manager.format)
    filename, err = camera_manager.record_video(duration, width=width, height=height, fmt=fmt)
    if filename is None:
        return jsonify({"success": False, "error": err}), 500
    def generate():
        with open(filename, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk
        os.remove(filename)
    if fmt.upper() == "MP4":
        mimetype = "video/mp4"
    else:
        mimetype = "video/x-msvideo"
    return Response(generate(), mimetype=mimetype,
                    headers={'Content-Disposition': f'attachment; filename={os.path.basename(filename)}'})

@app.route("/cam/res", methods=["PUT"])
def cam_res():
    content = request.get_json(force=True)
    width = int(content.get("width", camera_manager.width))
    height = int(content.get("height", camera_manager.height))
    camera_manager.set_resolution(width, height)
    return jsonify({"success": True, "width": width, "height": height})

@app.route("/cam/form", methods=["PUT"])
def cam_form():
    content = request.get_json(force=True)
    fmt = content.get("format", camera_manager.format)
    ok = camera_manager.set_format(fmt)
    if ok:
        return jsonify({"success": True, "format": camera_manager.format})
    else:
        return jsonify({"success": False, "error": "Unsupported format"}), 400

@app.route("/cam/list", methods=["GET"])
def cam_list():
    cams = camera_manager.list_cameras()
    return jsonify({"available_cameras": cams})

@app.route("/cam/switch", methods=["POST"])
def cam_switch():
    content = request.get_json(force=True)
    cam_id = int(content.get("camera_id"))
    ok = camera_manager.switch_camera(cam_id)
    if ok:
        return jsonify({"success": True, "current_camera_id": cam_id})
    else:
        return jsonify({"success": False, "error": "Failed to switch camera"}), 400

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)