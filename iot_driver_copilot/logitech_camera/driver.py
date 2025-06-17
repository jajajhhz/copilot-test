import os
import cv2
import threading
import tempfile
import time
import glob
import json
from flask import Flask, Response, request, jsonify, send_file, abort

# ======== Configuration via Environment Variables ========
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DEFAULT_CAMERA_ID = int(os.environ.get("CAMERA_ID", "0"))
DEFAULT_WIDTH = int(os.environ.get("CAMERA_WIDTH", "640"))
DEFAULT_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "480"))
DEFAULT_FPS = int(os.environ.get("CAMERA_FPS", "20"))
DEFAULT_FORMAT = os.environ.get("CAMERA_FORMAT", "MJPEG")  # Not strictly used, for compatibility

app = Flask(__name__)

# ======== Camera Manager Class ========
class CameraManager:
    def __init__(self):
        self.camera_id = DEFAULT_CAMERA_ID
        self.width = DEFAULT_WIDTH
        self.height = DEFAULT_HEIGHT
        self.fps = DEFAULT_FPS
        self.cap = None
        self.lock = threading.RLock()
        self.streaming = False
        self.recording = False
        self.record_thread = None
        self.record_stop_event = threading.Event()
        self.last_frame = None

    def _init_camera(self, camera_id=None, width=None, height=None, fps=None):
        camera_id = camera_id if camera_id is not None else self.camera_id
        width = width if width is not None else self.width
        height = height if height is not None else self.height
        fps = fps if fps is not None else self.fps
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera with id {camera_id}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        # Validate that settings were applied
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w != width or actual_h != height:
            # Just warn, some drivers will not set correctly
            pass
        return cap

    def start(self, camera_id=None, width=None, height=None, fps=None):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
            self.cap = self._init_camera(camera_id, width, height, fps)
            self.camera_id = camera_id if camera_id is not None else self.camera_id
            self.width = width if width is not None else self.width
            self.height = height if height is not None else self.height
            self.fps = fps if fps is not None else self.fps
            self.streaming = True

    def stop(self):
        with self.lock:
            self.streaming = False
            self.recording = False
            self.record_stop_event.set()
            if self.record_thread and self.record_thread.is_alive():
                self.record_thread.join(timeout=2)
            if self.cap is not None:
                self.cap.release()
                self.cap = None

    def switch_camera(self, camera_id):
        self.start(camera_id=camera_id)

    def get_frame(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                raise RuntimeError("Camera is not started.")
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError("Failed to read frame from camera.")
            self.last_frame = frame
            return frame

    def stream_generator(self):
        while True:
            with self.lock:
                if not self.streaming or self.cap is None:
                    break
                ret, frame = self.cap.read()
                if not ret:
                    continue
                self.last_frame = frame
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / self.fps)

    def capture_image(self, fmt='jpeg'):
        frame = self.get_frame()
        encode_format = '.png' if fmt.lower() == 'png' else '.jpg'
        ret, buf = cv2.imencode(encode_format, frame)
        if not ret:
            raise RuntimeError(f"Failed to encode frame as {fmt}")
        mime = 'image/png' if fmt.lower() == 'png' else 'image/jpeg'
        return buf.tobytes(), mime

    def record_video(self, duration=5, fmt='mp4', fps=None, width=None, height=None):
        fps = fps if fps is not None else self.fps
        width = width if width is not None else self.width
        height = height if height is not None else self.height
        codec = 'mp4v' if fmt.lower() == 'mp4' else 'xvid'
        ext = 'mp4' if fmt.lower() == 'mp4' else 'avi'
        temp_fd, temp_path = tempfile.mkstemp(suffix=f'.{ext}')
        os.close(temp_fd)
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(temp_path, fourcc, fps, (width, height))
        if not writer.isOpened():
            os.remove(temp_path)
            raise RuntimeError("Failed to open video writer.")

        self.recording = True
        self.record_stop_event.clear()
        start_time = time.time()
        try:
            while time.time() - start_time < duration and not self.record_stop_event.is_set():
                with self.lock:
                    if self.cap is None or not self.cap.isOpened():
                        break
                    ret, frame = self.cap.read()
                    if not ret:
                        continue
                    frame_resized = cv2.resize(frame, (width, height))
                    writer.write(frame_resized)
                time.sleep(1.0 / fps)
        finally:
            writer.release()
            self.recording = False
        return temp_path, ext

    def stop_recording(self):
        self.record_stop_event.set()


camera_manager = CameraManager()


# ======== API Endpoints ========

@app.route('/camera/start', methods=['POST'])
def start_camera():
    try:
        params = request.get_json(silent=True) or {}
        camera_id = int(params.get("camera_id", DEFAULT_CAMERA_ID))
        width = int(params.get("width", DEFAULT_WIDTH))
        height = int(params.get("height", DEFAULT_HEIGHT))
        fps = int(params.get("fps", DEFAULT_FPS))
        camera_manager.start(camera_id=camera_id, width=width, height=height, fps=fps)
        return jsonify({"status": "success", "message": f"Camera started (id={camera_id}, {width}x{height}@{fps}fps)"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    try:
        camera_manager.stop()
        return jsonify({"status": "success", "message": "Camera stopped"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/camera/switch', methods=['POST'])
def switch_camera():
    try:
        data = request.get_json(force=True)
        camera_id = int(data.get("camera_id"))
        camera_manager.switch_camera(camera_id)
        return jsonify({"status": "success", "message": f"Switched to camera {camera_id}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/cameras', methods=['GET'])
def list_cameras():
    try:
        # Try to find available camera devices (platform-dependent)
        cameras = []
        tested = set()
        # On Mac, /dev/video* is not available, so we scan 0..9
        max_test = 10
        for i in range(max_test):
            if i in tested:
                continue
            cap = cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                cameras.append({
                    "id": i,
                    "name": f"Camera {i}",
                    "model": "Unknown",
                    "manufacturer": "Unknown"
                })
                tested.add(i)
                cap.release()
        return jsonify({"cameras": cameras}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/camera/stream', methods=['GET'])
def camera_stream():
    try:
        # Stream as multipart/x-mixed-replace (MJPEG)
        return Response(camera_manager.stream_generator(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/camera/capture', methods=['GET'])
def camera_capture():
    try:
        fmt = request.args.get("format", "jpeg").lower()
        img, mime = camera_manager.capture_image(fmt=fmt)
        return Response(img, mimetype=mime)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/camera/record', methods=['POST'])
def camera_record():
    lock = camera_manager.lock
    with lock:
        if camera_manager.recording:
            return jsonify({"status": "error", "message": "Recording already in progress"}), 409
        try:
            params = request.get_json(silent=True) or {}
            duration = float(params.get("duration", 5))
            fmt = params.get("format", "mp4").lower()
            fps = int(params.get("fps", camera_manager.fps))
            width = int(params.get("width", camera_manager.width))
            height = int(params.get("height", camera_manager.height))
            temp_path, ext = camera_manager.record_video(duration=duration, fmt=fmt, fps=fps, width=width, height=height)
            return_data = None
            with open(temp_path, "rb") as f:
                return_data = f.read()
            os.remove(temp_path)
            mime = 'video/mp4' if ext == 'mp4' else 'video/x-msvideo'
            return Response(return_data, mimetype=mime,
                            headers={"Content-Disposition": f"attachment; filename=record.{ext}"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 400

# ======== Run Server ========
if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)