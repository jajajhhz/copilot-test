import os
import cv2
import threading
import time
import tempfile
import shutil
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

def get_env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default

def get_env_str(name, default):
    return os.environ.get(name, default)

# Configuration from environment variables
HTTP_HOST = get_env_str("HTTP_HOST", "0.0.0.0")
HTTP_PORT = get_env_int("HTTP_PORT", 8080)
DEFAULT_CAMERA_ID = get_env_int("CAMERA_ID", 0)
DEFAULT_WIDTH = get_env_int("CAMERA_WIDTH", 640)
DEFAULT_HEIGHT = get_env_int("CAMERA_HEIGHT", 480)
DEFAULT_FPS = get_env_int("CAMERA_FPS", 20)

class USBCamera:
    def __init__(self, cam_id=DEFAULT_CAMERA_ID, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS):
        self.cam_id = cam_id
        self.width = width
        self.height = height
        self.fps = fps
        self.camera = None
        self.streaming = False
        self.streaming_lock = threading.Lock()
        self.recording = False
        self.recording_lock = threading.Lock()
        self.recording_thread = None
        self.record_stop_event = threading.Event()
        self.last_frame = None
        self.last_frame_lock = threading.Lock()

    def _open_camera(self):
        cap = cv2.VideoCapture(self.cam_id)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        return cap

    def start(self):
        with self.streaming_lock:
            if self.streaming:
                return True, "Camera already started"
            self.camera = self._open_camera()
            if self.camera is None or not self.camera.isOpened():
                self.camera = None
                return False, "Unable to open camera"
            self.streaming = True
        return True, "Camera started"

    def stop(self):
        with self.streaming_lock:
            if self.camera is not None:
                self.camera.release()
            self.camera = None
            self.streaming = False
        with self.recording_lock:
            if self.recording:
                self.record_stop_event.set()
        return True, "Camera stopped"

    def switch_camera(self, cam_id):
        with self.streaming_lock:
            self.stop()
            self.cam_id = cam_id
            self.camera = self._open_camera()
            if self.camera is None or not self.camera.isOpened():
                self.camera = None
                return False, "Unable to switch to camera id {}".format(cam_id)
            self.streaming = True
        return True, "Switched to camera id {}".format(cam_id)

    def get_frame(self, img_format='jpeg'):
        with self.streaming_lock:
            if not self.streaming or self.camera is None:
                return None, "Camera not started"
            ret, frame = self.camera.read()
            if not ret:
                return None, "Failed to read frame"
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if img_format.lower() == 'jpeg' else []
            ext = '.jpg' if img_format.lower() == 'jpeg' else '.png'
            ret, img = cv2.imencode(ext, frame, encode_param)
            if not ret:
                return None, "Failed to encode image"
            # Store frame for stream/record
            with self.last_frame_lock:
                self.last_frame = frame
            return img.tobytes(), None

    def gen_stream(self):
        while True:
            with self.streaming_lock:
                if not self.streaming or self.camera is None:
                    break
                ret, frame = self.camera.read()
                if not ret:
                    continue
                with self.last_frame_lock:
                    self.last_frame = frame
                ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ret:
                    continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / self.fps)

    def record(self, duration=None, out_format="mp4", fps=None, resolution=None):
        with self.recording_lock:
            if self.recording:
                return None, "Already recording"
            if not self.streaming or self.camera is None:
                return None, "Camera not started"
            self.recording = True
            self.record_stop_event.clear()
            temp_dir = tempfile.mkdtemp()
            filename = os.path.join(temp_dir, 'video.' + out_format)
            if resolution is not None:
                width, height = resolution
            else:
                width, height = self.width, self.height
            if fps is None:
                fps = self.fps
            if out_format == "mp4":
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            elif out_format == "avi":
                fourcc = cv2.VideoWriter_fourcc(*'XVID')
            else:
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                filename = filename.rsplit('.',1)[0]+'.mp4'

            out = cv2.VideoWriter(filename, fourcc, fps, (int(width), int(height)))
            frames_to_capture = int(fps * duration) if duration else None

            def record_loop():
                frame_count = 0
                try:
                    while True:
                        if not self.streaming or self.camera is None or self.record_stop_event.is_set():
                            break
                        ret, frame = self.camera.read()
                        if not ret:
                            continue
                        out.write(frame)
                        frame_count += 1
                        if frames_to_capture and frame_count >= frames_to_capture:
                            break
                        time.sleep(1.0 / fps)
                finally:
                    out.release()
                    self.recording = False

            rec_thread = threading.Thread(target=record_loop)
            self.recording_thread = rec_thread
            rec_thread.start()
            rec_thread.join()
            if not os.path.exists(filename):
                shutil.rmtree(temp_dir)
                self.recording = False
                return None, "Recording failed"
            return filename, temp_dir

    def get_last_frame(self):
        with self.last_frame_lock:
            return self.last_frame

# Discover available cameras
def list_cameras(max_tested=10):
    found = []
    for cam_id in range(max_tested):
        cap = cv2.VideoCapture(cam_id)
        if cap is not None and cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            found.append({
                "camera_id": cam_id,
                "resolution": {"width": width, "height": height},
                "device_name": "USB Camera {}".format(cam_id)
            })
            cap.release()
    return found

camera = USBCamera()

@app.route("/camera/start", methods=["POST"])
def start_camera():
    width = get_env_int("CAMERA_WIDTH", DEFAULT_WIDTH)
    height = get_env_int("CAMERA_HEIGHT", DEFAULT_HEIGHT)
    fps = get_env_int("CAMERA_FPS", DEFAULT_FPS)
    # Optionally allow override via GET/POST args
    req_json = request.get_json(silent=True)
    if req_json:
        width = int(req_json.get('width', width))
        height = int(req_json.get('height', height))
        fps = int(req_json.get('fps', fps))
    camera.width = width
    camera.height = height
    camera.fps = fps
    ok, msg = camera.start()
    status = 200 if ok else 500
    return jsonify({"success": ok, "message": msg}), status

@app.route("/camera/stop", methods=["POST"])
def stop_camera():
    ok, msg = camera.stop()
    status = 200 if ok else 500
    return jsonify({"success": ok, "message": msg}), status

@app.route("/camera/switch", methods=["POST"])
def switch_camera():
    req_json = request.get_json(force=True)
    cam_id = req_json.get("camera_id")
    if cam_id is None:
        return jsonify({"success": False, "message": "Missing camera_id"}), 400
    try:
        cam_id = int(cam_id)
    except Exception:
        return jsonify({"success": False, "message": "camera_id must be integer"}), 400
    ok, msg = camera.switch_camera(cam_id)
    status = 200 if ok else 500
    return jsonify({"success": ok, "message": msg}), status

@app.route("/cameras", methods=["GET"])
def cameras_list():
    cams = list_cameras()
    return jsonify({"cameras": cams})

@app.route("/camera/capture", methods=["GET"])
def capture_frame():
    img_format = request.args.get("format", "jpeg")
    img, err = camera.get_frame(img_format=img_format)
    if img is None:
        return jsonify({"success": False, "message": err}), 500
    mime = "image/jpeg" if img_format.lower() == "jpeg" else "image/png"
    return Response(img, mimetype=mime,
                    headers={"Content-Disposition": "inline; filename=capture.{}".format("jpg" if img_format.lower() == "jpeg" else "png")})

@app.route("/camera/stream", methods=["GET"])
def stream_video():
    if not camera.streaming:
        ok, msg = camera.start()
        if not ok:
            return jsonify({"success": False, "message": msg}), 500
    return Response(camera.gen_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/record", methods=["POST"])
def record_video():
    req_json = request.get_json(silent=True)
    duration = None
    out_format = "mp4"
    fps = None
    resolution = None
    if req_json:
        duration = req_json.get("duration")
        if duration is not None:
            try:
                duration = float(duration)
            except Exception:
                return jsonify({"success": False, "message": "Invalid duration"}), 400
        out_format = req_json.get("format", "mp4")
        fps = req_json.get("fps")
        if fps is not None:
            try:
                fps = int(fps)
            except Exception:
                return jsonify({"success": False, "message": "Invalid fps"}), 400
        width = req_json.get("width")
        height = req_json.get("height")
        if width and height:
            try:
                resolution = (int(width), int(height))
            except Exception:
                return jsonify({"success": False, "message": "Invalid resolution"}), 400
    filename, extra = camera.record(duration=duration, out_format=out_format, fps=fps, resolution=resolution)
    if filename is None:
        return jsonify({"success": False, "message": extra}), 500
    # Send the file then cleanup
    try:
        return send_file(filename, as_attachment=True,
                         download_name=os.path.basename(filename),
                         mimetype="video/mp4" if out_format=="mp4" else "video/x-msvideo")
    finally:
        try:
            shutil.rmtree(extra)
        except Exception:
            pass

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)