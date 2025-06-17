import os
import io
import threading
import time
from flask import Flask, Response, request, jsonify, send_file, abort
import cv2
import numpy as np

# Environment variables for configuration
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
DEFAULT_WIDTH = int(os.environ.get("CAMERA_WIDTH", 640))
DEFAULT_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", 480))
DEFAULT_FPS = int(os.environ.get("CAMERA_FPS", 30))
DEFAULT_FORMAT = os.environ.get("CAMERA_FORMAT", "MJPEG").upper()  # JPEG, PNG, MP4, MJPEG
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

# Threading lock for camera access
camera_lock = threading.Lock()

class CameraManager:
    def __init__(self):
        self.cap = None
        self.running = False
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.fps = DEFAULT_FPS
        self.format = DEFAULT_FORMAT
        self.last_frame = None
        self.hotplug_thread = threading.Thread(target=self._monitor_camera_hotplug, daemon=True)
        self.hotplug_thread.start()
        self._enumerate_usb_cameras()

    def start(self, width=None, height=None, fps=None, fmt=None):
        with camera_lock:
            if self.running:
                return True
            index = CAMERA_INDEX
            self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            if width:
                self.width = int(width)
            if height:
                self.height = int(height)
            if fps:
                self.fps = int(fps)
            if fmt:
                self.format = fmt.upper()
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                self.running = False
                return False
            self.running = True
            return True

    def stop(self):
        with camera_lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.running = False

    def is_opened(self):
        with camera_lock:
            return self.cap is not None and self.cap.isOpened()

    def set_resolution(self, width, height):
        with camera_lock:
            self.width = int(width)
            self.height = int(height)
            if self.cap:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

    def set_format(self, fmt):
        with camera_lock:
            self.format = fmt.upper()

    def get_supported_resolutions(self):
        # Usually requires device-specific extension or probing, here are common resolutions
        return [
            {"width": 640, "height": 480},
            {"width": 1280, "height": 720},
            {"width": 1920, "height": 1080}
        ]

    def read_frame(self):
        with camera_lock:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    self.last_frame = frame
                    return frame
            return None

    def _monitor_camera_hotplug(self):
        # On Linux, we can monitor /dev/video*; on Windows/MacOS, it's more complex.
        # Here, we re-enumerate periodically (every 5s) and reset camera if disconnected.
        while True:
            time.sleep(5)
            if self.running and not self.is_opened():
                self.stop()

    def _enumerate_usb_cameras(self):
        # Try indices 0-5 for available cameras; cross-platform
        self.available_cameras = []
        for idx in range(5):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            if cap is not None and cap.read()[0]:
                self.available_cameras.append(idx)
                cap.release()

camera_manager = CameraManager()
app = Flask(__name__)

def gen_mjpeg_stream():
    while camera_manager.is_opened():
        frame = camera_manager.read_frame()
        if frame is None:
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / camera_manager.fps)

@app.route("/cam/start", methods=["POST"])
def start_camera():
    params = request.args
    opt = request.get_json(silent=True) or {}
    width = params.get("width") or opt.get("width")
    height = params.get("height") or opt.get("height")
    fps = params.get("fps") or opt.get("fps")
    fmt = params.get("format") or opt.get("format")
    ok = camera_manager.start(width, height, fps, fmt)
    if ok:
        return jsonify({"status": "started", "width": camera_manager.width, "height": camera_manager.height, "fps": camera_manager.fps, "format": camera_manager.format}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to start camera"}), 500

@app.route("/cam/stop", methods=["POST"])
def stop_camera():
    camera_manager.stop()
    return jsonify({"status": "stopped"}), 200

@app.route("/cam/capture", methods=["GET"])
def capture_image():
    if not camera_manager.is_opened():
        abort(503, "Camera not started")
    width = request.args.get("width", camera_manager.width)
    height = request.args.get("height", camera_manager.height)
    fmt = request.args.get("format", "JPEG").upper()  # JPEG or PNG
    camera_manager.set_resolution(width, height)
    frame = camera_manager.read_frame()
    if frame is None:
        abort(500, "Failed to capture image")
    encode_param = '.jpg' if fmt == "JPEG" else '.png'
    ret, buffer = cv2.imencode(encode_param, frame)
    if not ret:
        abort(500, "Encoding failed")
    mimetype = "image/jpeg" if fmt == "JPEG" else "image/png"
    return Response(buffer.tobytes(), content_type=mimetype)

@app.route("/cam/res", methods=["PUT"])
def set_resolution():
    data = request.get_json(force=True)
    width = data.get("width")
    height = data.get("height")
    if not width or not height:
        abort(400, "width and height required")
    camera_manager.set_resolution(width, height)
    return jsonify({"status": "ok", "width": camera_manager.width, "height": camera_manager.height})

@app.route("/cam/form", methods=["PUT"])
def set_format():
    data = request.get_json(force=True)
    fmt = data.get("format")
    if not fmt or fmt.upper() not in ["JPEG", "PNG", "MP4", "MJPEG"]:
        abort(400, "Invalid format")
    camera_manager.set_format(fmt)
    return jsonify({"status": "ok", "format": camera_manager.format})

@app.route("/cam/stream", methods=["GET"])
def stream_camera():
    if not camera_manager.is_opened():
        abort(503, "Camera not started")
    width = request.args.get("width", camera_manager.width)
    height = request.args.get("height", camera_manager.height)
    fmt = request.args.get("format", camera_manager.format).upper()
    camera_manager.set_resolution(width, height)
    camera_manager.set_format(fmt)
    if fmt == "MJPEG":
        return Response(gen_mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")
    elif fmt in ["JPEG", "PNG"]:
        frame = camera_manager.read_frame()
        encode_param = '.jpg' if fmt == "JPEG" else '.png'
        ret, buffer = cv2.imencode(encode_param, frame)
        mimetype = "image/jpeg" if fmt == "JPEG" else "image/png"
        if not ret:
            abort(500, "Encoding failed")
        return Response(buffer.tobytes(), content_type=mimetype)
    elif fmt == "MP4":
        # Serve a short 10-second MP4 stream (simulate real-time streaming)
        duration = int(request.args.get("duration", 10))
        if duration > 60:
            duration = 60
        return Response(generate_mp4_stream(duration), mimetype="video/mp4")
    else:
        abort(400, "Unsupported format")

def generate_mp4_stream(duration_sec):
    # This is a generator that streams an MP4 video chunk (simulate)
    # We'll use cv2.VideoWriter to write to a bytes buffer
    fps = camera_manager.fps
    width = camera_manager.width
    height = camera_manager.height
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    buf = io.BytesIO()
    temp_filename = f"/tmp/cam_stream_{time.time()}.mp4"
    out = cv2.VideoWriter(temp_filename, fourcc, fps, (width, height))
    start_time = time.time()
    frame_count = 0
    while time.time() - start_time < duration_sec:
        frame = camera_manager.read_frame()
        if frame is not None:
            out.write(frame)
            frame_count += 1
        else:
            break
    out.release()
    with open(temp_filename, "rb") as f:
        data = f.read()
    os.remove(temp_filename)
    yield data

@app.route("/cam/record", methods=["POST"])
def record_video():
    if not camera_manager.is_opened():
        abort(503, "Camera not started")
    data = request.get_json(force=True)
    duration = int(data.get("duration", 5))
    if duration > 60:
        duration = 60
    width = data.get("width", camera_manager.width)
    height = data.get("height", camera_manager.height)
    fmt = data.get("format", "MP4").upper()
    camera_manager.set_resolution(width, height)
    if fmt == "MP4":
        ext = "mp4"
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        mimetype = "video/mp4"
    elif fmt == "MJPEG":
        ext = "avi"
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        mimetype = "video/x-motion-jpeg"
    else:
        abort(400, "Invalid format")
    temp_filename = f"/tmp/cam_record_{time.time()}.{ext}"
    out = cv2.VideoWriter(temp_filename, fourcc, camera_manager.fps, (camera_manager.width, camera_manager.height))
    start = time.time()
    while time.time() - start < duration:
        frame = camera_manager.read_frame()
        if frame is not None:
            out.write(frame)
        else:
            break
    out.release()
    return send_file(temp_filename, as_attachment=True, mimetype=mimetype, download_name=f"record.{ext}")

@app.route("/cam/devices", methods=["GET"])
def list_usb_cameras():
    camera_manager._enumerate_usb_cameras()
    return jsonify({"devices": camera_manager.available_cameras})

@app.route("/cam/support", methods=["GET"])
def supported_features():
    return jsonify({
        "resolutions": camera_manager.get_supported_resolutions(),
        "formats": ["JPEG", "PNG", "MP4", "MJPEG"]
    })

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)