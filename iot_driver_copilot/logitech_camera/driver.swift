import os
import cv2
import threading
import time
import tempfile
import shutil
from flask import Flask, Response, jsonify, request, send_file, abort

app = Flask(__name__)

# Configuration from environment variables
CAMERA_ID = int(os.environ.get("CAMERA_ID", 0))
RESOLUTION_X = int(os.environ.get("RESOLUTION_X", 640))
RESOLUTION_Y = int(os.environ.get("RESOLUTION_Y", 480))
FRAME_RATE = float(os.environ.get("FRAME_RATE", 20.0))
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", 8080))
RECORD_CODEC = os.environ.get("RECORD_CODEC", "mp4v")
RECORD_FORMAT = os.environ.get("RECORD_FORMAT", "mp4")
CAMERA_LIST_MAX = int(os.environ.get("CAMERA_LIST_MAX", 10))

def available_cameras(max_devices=10):
    ids = []
    for i in range(max_devices):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ids.append(i)
            cap.release()
    return ids

class CameraHandler:
    def __init__(self):
        self.camera_id = CAMERA_ID
        self.cap = None
        self.lock = threading.RLock()
        self.streaming = False
        self.active = False

    def start(self, camera_id=None):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
            if camera_id is not None:
                self.camera_id = camera_id
            self.cap = cv2.VideoCapture(self.camera_id)
            if not self.cap.isOpened():
                self.cap = None
                self.active = False
                raise RuntimeError(f"Failed to open camera {self.camera_id}")
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, RESOLUTION_X)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, RESOLUTION_Y)
            self.cap.set(cv2.CAP_PROP_FPS, FRAME_RATE)
            self.active = True

    def stop(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.active = False

    def read_frame(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                raise RuntimeError("Camera not started")
            ret, frame = self.cap.read()
            if not ret or frame is None:
                raise RuntimeError("Failed to capture frame")
            return frame

    def capture_image(self, ext=".jpg"):
        frame = self.read_frame()
        ret, buf = cv2.imencode(ext, frame)
        if not ret:
            raise RuntimeError("Failed to encode image")
        return buf.tobytes(), frame

    def stream_generator(self):
        while True:
            with self.lock:
                if not self.active or self.cap is None or not self.cap.isOpened():
                    break
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    break
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    break
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / FRAME_RATE)

    def record_video(self, duration, codec, fmt):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                raise RuntimeError("Camera not started")
            ext = ".mp4" if fmt == "mp4" else ".avi"
            temp_dir = tempfile.mkdtemp()
            outfile = os.path.join(temp_dir, f"recording{ext}")

            fourcc = cv2.VideoWriter_fourcc(*codec)
            out = cv2.VideoWriter(
                outfile, fourcc, FRAME_RATE, (RESOLUTION_X, RESOLUTION_Y)
            )
            if not out.isOpened():
                shutil.rmtree(temp_dir)
                raise RuntimeError("Failed to open video writer")

            end_time = time.time() + duration
            try:
                while time.time() < end_time:
                    ret, frame = self.cap.read()
                    if not ret or frame is None:
                        break
                    out.write(frame)
                    time.sleep(1.0 / FRAME_RATE)
            finally:
                out.release()
            return outfile, temp_dir

    def active_status(self):
        with self.lock:
            return self.active

camera_handler = CameraHandler()

@app.route('/cameras', methods=['GET'])
def list_cameras():
    cam_ids = available_cameras(CAMERA_LIST_MAX)
    selected = camera_handler.camera_id
    status = [{"camera_id": i, "selected": (i == selected)} for i in cam_ids]
    return jsonify({"cameras": status})

@app.route('/camera/start', methods=['POST'])
def start_camera():
    data = request.get_json(silent=True)
    cam_id = data.get("camera_id") if (data and "camera_id" in data) else None
    try:
        camera_handler.start(camera_id=cam_id)
        return jsonify({"status": "success", "camera_id": camera_handler.camera_id})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    try:
        camera_handler.stop()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@app.route('/cameras/select', methods=['PUT'])
def select_camera():
    data = request.get_json()
    if not data or "camera_id" not in data:
        return jsonify({"status": "error", "error": "camera_id required"}), 400
    try:
        camera_handler.start(data["camera_id"])
        return jsonify({"status": "success", "camera_id": camera_handler.camera_id})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@app.route('/camera/stream', methods=['GET'])
def stream_camera():
    if not camera_handler.active_status():
        return jsonify({"status": "error", "error": "Camera not started"}), 400
    return Response(camera_handler.stream_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/capture', methods=['GET'])
def capture_frame():
    ext = request.args.get("ext", ".jpg")
    if ext.lower() not in [".jpg", ".jpeg", ".png"]:
        ext = ".jpg"
    try:
        img_bytes, frame = camera_handler.capture_image(ext)
        _, buffer = cv2.imencode(ext, frame)
        metadata = {
            "shape": list(frame.shape),
            "dtype": str(frame.dtype),
            "camera_id": camera_handler.camera_id
        }
        return Response(img_bytes, mimetype="image/jpeg",
                        headers={"X-Metadata": str(metadata)})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@app.route('/camera/record', methods=['POST'])
def record_video():
    if not camera_handler.active_status():
        return jsonify({"status": "error", "error": "Camera not started"}), 400
    data = request.get_json()
    duration = data.get("duration") if data and "duration" in data else 5
    try:
        outfile, temp_dir = camera_handler.record_video(
            duration,
            RECORD_CODEC if RECORD_FORMAT == "avi" else "mp4v",
            RECORD_FORMAT
        )
        filename = os.path.basename(outfile)
        resp = send_file(outfile, as_attachment=True, download_name=filename)
        # Clean up after sending
        @resp.call_on_close
        def cleanup():
            shutil.rmtree(temp_dir)
        return resp
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)