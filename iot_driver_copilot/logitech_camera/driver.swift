import os
import cv2
import threading
import time
import tempfile
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

CAMERA_LOCK = threading.Lock()
CAMERA_MANAGER_LOCK = threading.Lock()

DEFAULT_RESOLUTION = (
    int(os.environ.get("CAMERA_RES_WIDTH", 640)),
    int(os.environ.get("CAMERA_RES_HEIGHT", 480)),
)
DEFAULT_FPS = int(os.environ.get("CAMERA_FPS", 20))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

class CameraSession:
    def __init__(self, camera_id=0, resolution=DEFAULT_RESOLUTION, fps=DEFAULT_FPS):
        self.camera_id = camera_id
        self.resolution = resolution
        self.fps = fps
        self.cap = None
        self.active = False

    def start(self):
        with CAMERA_LOCK:
            if self.cap is not None:
                return
            self.cap = cv2.VideoCapture(self.camera_id)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            if not self.cap.isOpened():
                self.cap = None
                raise RuntimeError("Failed to open camera {}".format(self.camera_id))
            self.active = True

    def stop(self):
        with CAMERA_LOCK:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
                self.active = False

    def read(self):
        with CAMERA_LOCK:
            if self.cap is None or not self.cap.isOpened():
                raise RuntimeError("Camera not started")
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError("Failed to capture frame")
            return frame

    def is_active(self):
        return self.cap is not None and self.cap.isOpened()

class CameraManager:
    def __init__(self):
        self.sessions = {}
        self.selected_id = None
        self.list_cameras()

    def list_cameras(self):
        cameras = []
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                cameras.append(i)
                cap.release()
        return cameras

    def get_status(self):
        info = []
        for cam_id in self.list_cameras():
            session = self.sessions.get(cam_id)
            info.append({
                "camera_id": cam_id,
                "active": bool(session and session.is_active())
            })
        return info

    def start_camera(self, camera_id=None):
        with CAMERA_MANAGER_LOCK:
            if camera_id is None:
                camera_id = 0
            if camera_id not in self.list_cameras():
                raise RuntimeError("Camera ID {} not found".format(camera_id))
            if camera_id not in self.sessions:
                self.sessions[camera_id] = CameraSession(camera_id)
            self.sessions[camera_id].start()
            self.selected_id = camera_id

    def stop_camera(self):
        with CAMERA_MANAGER_LOCK:
            if self.selected_id is not None and self.selected_id in self.sessions:
                self.sessions[self.selected_id].stop()
                self.selected_id = None

    def select_camera(self, camera_id):
        with CAMERA_MANAGER_LOCK:
            if camera_id not in self.list_cameras():
                raise RuntimeError("Camera ID {} not found".format(camera_id))
            self.stop_camera()
            self.start_camera(camera_id)

    def get_active_session(self):
        if self.selected_id is not None and self.selected_id in self.sessions:
            return self.sessions[self.selected_id]
        return None

camera_manager = CameraManager()

def mjpeg_stream(session):
    try:
        while session.is_active():
            frame = session.read()
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / session.fps)
    except Exception:
        pass

@app.route("/cameras", methods=["GET"])
def list_cameras():
    return jsonify({
        "cameras": camera_manager.get_status()
    })

@app.route("/camera/start", methods=["POST"])
def start_camera():
    try:
        args = request.json if request.is_json else {}
        camera_id = args.get("camera_id", 0)
        camera_manager.start_camera(camera_id)
        return jsonify({"status": "started", "camera_id": camera_manager.selected_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/camera/stop", methods=["POST"])
def stop_camera():
    try:
        camera_manager.stop_camera()
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/cameras/select", methods=["PUT"])
def select_camera():
    try:
        data = request.json
        if not data or "camera_id" not in data:
            return jsonify({"error": "Missing camera_id"}), 400
        camera_manager.select_camera(int(data["camera_id"]))
        return jsonify({"status": "switched", "camera_id": camera_manager.selected_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    session = camera_manager.get_active_session()
    if not session or not session.is_active():
        return jsonify({"error": "No camera started"}), 400
    return Response(mjpeg_stream(session),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/capture", methods=["GET"])
def camera_capture():
    try:
        session = camera_manager.get_active_session()
        if not session or not session.is_active():
            return jsonify({"error": "No camera started"}), 400
        frame = session.read()
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            raise Exception("Failed to encode image")
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        temp.write(jpeg.tobytes())
        temp.close()
        meta = {
            "width": frame.shape[1],
            "height": frame.shape[0],
            "channels": frame.shape[2] if len(frame.shape) > 2 else 1,
            "camera_id": session.camera_id
        }
        resp = send_file(temp.name, mimetype="image/jpeg")
        resp.headers['X-Camera-Meta'] = str(meta)
        os.unlink(temp.name)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/camera/record", methods=["POST"])
def camera_record():
    try:
        session = camera_manager.get_active_session()
        if not session or not session.is_active():
            return jsonify({"error": "No camera started"}), 400
        data = request.json
        duration = int(data.get("duration", 5)) if data else 5
        ext = data.get("format", "mp4")
        fourcc = cv2.VideoWriter_fourcc(*("mp4v" if ext == "mp4" else "XVID"))
        temp = tempfile.NamedTemporaryFile(delete=False, suffix="." + ext)
        out = cv2.VideoWriter(temp.name, fourcc, session.fps, session.resolution)
        start_time = time.time()
        try:
            while time.time() - start_time < duration:
                with CAMERA_LOCK:
                    frame = session.read()
                    out.write(frame)
                time.sleep(1.0 / session.fps)
        finally:
            out.release()
        resp = send_file(temp.name, mimetype="video/mp4" if ext == "mp4" else "video/avi")
        os.unlink(temp.name)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)