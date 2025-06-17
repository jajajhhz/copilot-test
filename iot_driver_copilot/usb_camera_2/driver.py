import os
import io
import cv2
import time
import json
import threading
import platform
from flask import Flask, Response, request, jsonify, send_file

app = Flask(__name__)

# Environment Variables
CAMERA_IDS = os.getenv("CAMERA_IDS")  # comma-separated, e.g. "0,1,2"
DEFAULT_CAMERA_ID = int(os.getenv("DEFAULT_CAMERA_ID", "0"))
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
DEFAULT_WIDTH = int(os.getenv("DEFAULT_RES_WIDTH", "640"))
DEFAULT_HEIGHT = int(os.getenv("DEFAULT_RES_HEIGHT", "480"))
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
SUPPORTED_FORMATS = ["JPEG", "PNG", "MP4", "MJPEG"]

# State
class CameraState:
    def __init__(self):
        self.cam_id = DEFAULT_CAMERA_ID
        self.cap = None
        self.is_running = False
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.fps = DEFAULT_FPS
        self.format = "MJPEG"
        self.lock = threading.Lock()
        self.recording = False
        self.record_thread = None
        self.record_stop_event = threading.Event()
        self.device_list = self.enumerate_cameras()
        if self.cam_id not in self.device_list:
            if self.device_list:
                self.cam_id = self.device_list[0]
            else:
                self.cam_id = 0

    def enumerate_cameras(self, max_test=10):
        ids = []
        if CAMERA_IDS:
            ids = [int(x) for x in CAMERA_IDS.split(",")]
            return ids
        for i in range(max_test):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if platform.system() == "Windows" else 0)
            if cap is not None and cap.isOpened():
                ids.append(i)
                cap.release()
        return ids

    def start_camera(self, cam_id=None, width=None, height=None, fps=None, fmt=None):
        with self.lock:
            if self.is_running:
                return True
            cam_id = cam_id if cam_id is not None else self.cam_id
            if cam_id not in self.device_list:
                return False
            self.cap = cv2.VideoCapture(cam_id, cv2.CAP_DSHOW if platform.system() == "Windows" else 0)
            if not self.cap.isOpened():
                self.cap = None
                return False
            self.cam_id = cam_id
            self.width = int(width or self.width)
            self.height = int(height or self.height)
            self.fps = int(fps or self.fps)
            self.format = fmt.upper() if fmt and fmt.upper() in SUPPORTED_FORMATS else self.format
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.is_running = True
            return True

    def stop_camera(self):
        with self.lock:
            if self.cap:
                self.cap.release()
            self.cap = None
            self.is_running = False
            self.record_stop_event.set()
            self.recording = False

    def switch_camera(self, cam_id):
        with self.lock:
            if cam_id not in self.device_list:
                return False
            self.stop_camera()
            return self.start_camera(cam_id)

    def set_resolution(self, width, height):
        with self.lock:
            self.width = int(width)
            self.height = int(height)
            if self.cap and self.is_running:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

    def set_format(self, fmt):
        with self.lock:
            fmt = fmt.upper()
            if fmt in SUPPORTED_FORMATS:
                self.format = fmt
                return True
            return False

    def get_frame(self):
        with self.lock:
            if not self.cap or not self.is_running:
                return None
            ret, frame = self.cap.read()
            if not ret:
                return None
            return frame

camera_state = CameraState()

# Helper
def encode_image(frame, fmt="JPEG"):
    fmt = fmt.upper()
    if fmt == "PNG":
        ret, buf = cv2.imencode('.png', frame)
        mime = "image/png"
    else:
        ret, buf = cv2.imencode('.jpg', frame)
        mime = "image/jpeg"
    if not ret:
        return None, None
    return buf.tobytes(), mime

def mjpeg_stream_generator():
    while camera_state.is_running:
        frame = camera_state.get_frame()
        if frame is not None:
            img, mime = encode_image(frame, "JPEG")
            if img:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + img + b'\r\n')
        else:
            time.sleep(0.05)

def mp4_record_thread(duration, width, height, fps, out_path):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    start_time = time.time()
    while (time.time() - start_time) < duration and not camera_state.record_stop_event.is_set():
        frame = camera_state.get_frame()
        if frame is not None:
            frame = cv2.resize(frame, (width, height))
            out.write(frame)
        else:
            time.sleep(0.01)
    out.release()
    camera_state.recording = False

def mjpeg_record_thread(duration, width, height, fps, out_path):
    fourcc = cv2.VideoWriter_fourcc(*'MJPG')
    out = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    start_time = time.time()
    while (time.time() - start_time) < duration and not camera_state.record_stop_event.is_set():
        frame = camera_state.get_frame()
        if frame is not None:
            frame = cv2.resize(frame, (width, height))
            out.write(frame)
        else:
            time.sleep(0.01)
    out.release()
    camera_state.recording = False

@app.route("/cam/start", methods=["POST"])
def cam_start():
    params = request.args if request.args else {}
    data = request.get_json(silent=True) or {}
    cam_id = int(params.get("cam_id", data.get("cam_id", camera_state.cam_id)))
    width = int(params.get("width", data.get("width", camera_state.width)))
    height = int(params.get("height", data.get("height", camera_state.height)))
    fps = int(params.get("fps", data.get("fps", camera_state.fps)))
    fmt = params.get("format", data.get("format", camera_state.format))
    ok = camera_state.start_camera(cam_id, width, height, fps, fmt)
    if ok:
        return jsonify({"status": "started", "cam_id": cam_id, "width": width, "height": height, "fps": fps, "format": camera_state.format})
    else:
        return jsonify({"status": "error", "message": "Failed to start camera."}), 500

@app.route("/cam/stop", methods=["POST"])
def cam_stop():
    camera_state.stop_camera()
    return jsonify({"status": "stopped"})

@app.route("/cam/capture", methods=["GET"])
def cam_capture():
    fmt = request.args.get("format", camera_state.format).upper()
    width = int(request.args.get("width", camera_state.width))
    height = int(request.args.get("height", camera_state.height))
    frame = camera_state.get_frame()
    if frame is None:
        return jsonify({"status": "error", "message": "Camera not running or no frame available."}), 500
    if frame.shape[1] != width or frame.shape[0] != height:
        frame = cv2.resize(frame, (width, height))
    img, mime = encode_image(frame, fmt)
    if img is None:
        return jsonify({"status": "error", "message": "Frame encode error."}), 500
    return Response(img, mimetype=mime, headers={"Content-Disposition": f"inline; filename=capture.{fmt.lower()}"})

@app.route("/cam/stream", methods=["GET"])
def cam_stream():
    fmt = request.args.get("format", camera_state.format).upper()
    width = int(request.args.get("width", camera_state.width))
    height = int(request.args.get("height", camera_state.height))
    fps = int(request.args.get("fps", camera_state.fps))
    if fmt == "MJPEG":
        def generator():
            while camera_state.is_running:
                frame = camera_state.get_frame()
                if frame is not None:
                    if frame.shape[1] != width or frame.shape[0] != height:
                        frame = cv2.resize(frame, (width, height))
                    img, mime = encode_image(frame, "JPEG")
                    if img:
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + img + b'\r\n')
                else:
                    time.sleep(0.03)
        return Response(generator(), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return jsonify({"status": "error", "message": "Only MJPEG stream is supported for HTTP streaming."}), 400

@app.route("/cam/record", methods=["POST"])
def cam_record():
    if camera_state.recording:
        return jsonify({"status": "error", "message": "Already recording."}), 409
    data = request.get_json(force=True)
    duration = float(data.get("duration", 5.0))
    duration = max(0.1, min(duration, 60.0))
    width = int(data.get("width", camera_state.width))
    height = int(data.get("height", camera_state.height))
    fps = int(data.get("fps", camera_state.fps))
    fmt = data.get("format", camera_state.format).upper()
    if fmt not in ("MP4", "MJPEG"):
        return jsonify({"status": "error", "message": "Unsupported format for recording."}), 400
    ext = ".mp4" if fmt == "MP4" else ".avi"
    out_path = f"/tmp/record_{int(time.time())}{ext}"
    camera_state.record_stop_event.clear()
    camera_state.recording = True
    if fmt == "MP4":
        t = threading.Thread(target=mp4_record_thread, args=(duration, width, height, fps, out_path), daemon=True)
    else:
        t = threading.Thread(target=mjpeg_record_thread, args=(duration, width, height, fps, out_path), daemon=True)
    camera_state.record_thread = t
    t.start()
    t.join()
    if os.path.exists(out_path):
        resp = send_file(out_path, mimetype="video/mp4" if fmt == "MP4" else "video/x-msvideo", as_attachment=True)
        os.remove(out_path)
        return resp
    else:
        return jsonify({"status": "error", "message": "Recording failed."}), 500

@app.route("/cam/res", methods=["PUT"])
def cam_res():
    data = request.get_json(force=True)
    width = int(data.get("width", camera_state.width))
    height = int(data.get("height", camera_state.height))
    camera_state.set_resolution(width, height)
    return jsonify({"status": "ok", "width": camera_state.width, "height": camera_state.height})

@app.route("/cam/form", methods=["PUT"])
def cam_form():
    data = request.get_json(force=True)
    fmt = data.get("format", camera_state.format).upper()
    if not camera_state.set_format(fmt):
        return jsonify({"status": "error", "message": "Unsupported format."}), 400
    return jsonify({"status": "ok", "format": camera_state.format})

@app.route("/cam/devices", methods=["GET"])
def cam_devices():
    return jsonify({"devices": camera_state.device_list, "current": camera_state.cam_id})

@app.route("/cam/switch", methods=["POST"])
def cam_switch():
    data = request.get_json(force=True)
    cam_id = int(data.get("cam_id", camera_state.cam_id))
    if camera_state.switch_camera(cam_id):
        return jsonify({"status": "ok", "cam_id": cam_id})
    else:
        return jsonify({"status": "error", "message": "Camera id not found."}), 404

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)