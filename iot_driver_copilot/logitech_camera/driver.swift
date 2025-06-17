import os
import cv2
import threading
import time
import tempfile
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# Configuration from environment variables with defaults
CAMERA_DEFAULT_ID = int(os.environ.get("CAMERA_ID", 0))
DEFAULT_WIDTH = int(os.environ.get("CAMERA_WIDTH", 640))
DEFAULT_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", 480))
DEFAULT_FPS = int(os.environ.get("CAMERA_FPS", 20))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

class CameraSession:
    def __init__(self, camera_id=CAMERA_DEFAULT_ID, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS):
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.lock = threading.RLock()
        self.active = False
        self.last_frame = None

    def start(self):
        with self.lock:
            if self.active:
                return True
            cap = cv2.VideoCapture(self.camera_id)
            if not cap.isOpened():
                return False
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.cap = cap
            self.active = True
            return True

    def stop(self):
        with self.lock:
            if self.cap:
                self.cap.release()
                self.cap = None
            self.active = False

    def read(self):
        with self.lock:
            if self.cap and self.active:
                ret, frame = self.cap.read()
                if ret:
                    self.last_frame = frame
                    return frame
        return None

    def is_opened(self):
        with self.lock:
            return self.cap is not None and self.active and self.cap.isOpened()

    def select(self, camera_id):
        with self.lock:
            self.stop()
            self.camera_id = camera_id
            return self.start()

    def get_camera_id(self):
        with self.lock:
            return self.camera_id

    def get_resolution(self):
        return (self.width, self.height)

    def get_fps(self):
        return self.fps

    def __del__(self):
        self.stop()

# Global camera session (only one active at a time)
camera_session = CameraSession()

def list_cameras(max_test=10):
    cameras = []
    for i in range(max_test):
        cap = cv2.VideoCapture(i)
        if cap is not None and cap.isOpened():
            cameras.append({"camera_id": i, "status": "available" if i != camera_session.get_camera_id() or not camera_session.is_opened() else "active"})
            cap.release()
    return cameras

@app.route("/camera/start", methods=["POST"])
def camera_start():
    camera_id = request.args.get("camera_id", default=CAMERA_DEFAULT_ID, type=int)
    width = request.args.get("width", default=DEFAULT_WIDTH, type=int)
    height = request.args.get("height", default=DEFAULT_HEIGHT, type=int)
    fps = request.args.get("fps", default=DEFAULT_FPS, type=int)

    global camera_session
    with threading.RLock():
        camera_session.stop()
        camera_session = CameraSession(camera_id, width, height, fps)
        if camera_session.start():
            return jsonify({"result": "success", "camera_id": camera_id, "width": width, "height": height, "fps": fps}), 200
        else:
            return jsonify({"result": "error", "message": "Unable to start camera."}), 500

@app.route("/camera/stop", methods=["POST"])
def camera_stop():
    global camera_session
    camera_session.stop()
    return jsonify({"result": "success", "message": "Camera stopped."}), 200

@app.route("/cameras/select", methods=["PUT"])
def camera_select():
    data = request.get_json(force=True)
    camera_id = data.get("camera_id", None)
    if camera_id is None or not isinstance(camera_id, int):
        return jsonify({"result": "error", "message": "Missing or invalid 'camera_id'."}), 400
    global camera_session
    if not camera_session.select(camera_id):
        return jsonify({"result": "error", "message": f"Could not switch to camera {camera_id}."}), 500
    return jsonify({"result": "success", "camera_id": camera_id}), 200

@app.route("/cameras", methods=["GET"])
def cameras_list():
    cameras = list_cameras()
    return jsonify({"cameras": cameras}), 200

def generate_mjpeg(camera):
    while True:
        frame = camera.read()
        if frame is None:
            time.sleep(0.1)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / camera.get_fps())

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    if not camera_session.is_opened():
        return jsonify({"result": "error", "message": "Camera is not started."}), 400
    return Response(generate_mjpeg(camera_session),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/capture", methods=["GET"])
def camera_capture():
    frame = camera_session.read()
    if frame is None:
        return jsonify({"result": "error", "message": "No frame available. Is the camera started?"}), 400
    ext = request.args.get("format", "jpg").lower()
    if ext not in ["jpg", "jpeg", "png"]:
        ext = "jpg"
    ret, buf = cv2.imencode(f'.{ext}', frame)
    if not ret:
        return jsonify({"result": "error", "message": "Failed to encode frame."}), 500
    temp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
    temp.write(buf.tobytes())
    temp.flush()
    temp.close()
    metadata = {
        "camera_id": camera_session.get_camera_id(),
        "resolution": {"width": frame.shape[1], "height": frame.shape[0]},
        "format": ext,
        "timestamp": time.time()
    }
    def cleanup(filename):
        try:
            os.unlink(filename)
        except Exception:
            pass
    response = send_file(temp.name, mimetype=f"image/{'jpeg' if ext in ['jpg','jpeg'] else ext}", as_attachment=True)
    response.headers["X-Metadata"] = str(metadata)
    threading.Thread(target=cleanup, args=(temp.name,)).start()
    return response

@app.route("/camera/record", methods=["POST"])
def camera_record():
    if not camera_session.is_opened():
        return jsonify({"result": "error", "message": "Camera is not started."}), 400
    try:
        data = request.get_json(force=True)
        duration = float(data.get("duration", 5))
        ext = data.get("format", "mp4").lower()
        if ext not in ["mp4", "avi"]:
            ext = "mp4"
    except Exception:
        return jsonify({"result": "error", "message": "Invalid request body."}), 400

    fps = camera_session.get_fps()
    width, height = camera_session.get_resolution()
    fourcc = cv2.VideoWriter_fourcc(*('mp4v' if ext == "mp4" else 'XVID'))
    temp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
    out = cv2.VideoWriter(temp.name, fourcc, fps, (width, height))
    frames_to_capture = int(duration * fps)
    error_msg = None
    try:
        for _ in range(frames_to_capture):
            frame = camera_session.read()
            if frame is None:
                error_msg = "Camera frame not available during recording."
                break
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            out.write(frame)
            time.sleep(1.0 / fps)
    except Exception as e:
        error_msg = str(e)
    finally:
        out.release()
    if error_msg:
        os.unlink(temp.name)
        return jsonify({"result": "error", "message": error_msg}), 500
    def cleanup(filename):
        try:
            os.unlink(filename)
        except Exception:
            pass
    response = send_file(temp.name, mimetype=f"video/{'mp4' if ext == 'mp4' else 'x-msvideo'}", as_attachment=True)
    threading.Thread(target=cleanup, args=(temp.name,)).start()
    return response

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)