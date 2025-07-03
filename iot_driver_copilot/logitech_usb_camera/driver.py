import os
import threading
import time
import io
import uuid

from flask import Flask, Response, request, jsonify, send_file, abort
import cv2

# Environment Variables
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.getenv("FRAME_HEIGHT", "480"))
FRAME_RATE = int(os.getenv("FRAME_RATE", "20"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "90"))

app = Flask(__name__)

class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.capture = None
        self.active_session = None
        self.streaming_clients = 0
        self.running = False
        self.last_frame = None
        self.frame_thread = None

    def start_capture(self):
        with self.lock:
            if self.capture is not None and self.running:
                return self.active_session
            self.capture = cv2.VideoCapture(CAMERA_INDEX)
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            self.capture.set(cv2.CAP_PROP_FPS, FRAME_RATE)
            self.running = True
            self.active_session = str(uuid.uuid4())
            self.frame_thread = threading.Thread(target=self._update_frame, daemon=True)
            self.frame_thread.start()
            return self.active_session

    def stop_capture(self):
        with self.lock:
            self.running = False
            if self.capture:
                self.capture.release()
                self.capture = None
            self.active_session = None
            self.last_frame = None

    def _update_frame(self):
        while self.running:
            if self.capture:
                ret, frame = self.capture.read()
                if ret:
                    self.last_frame = frame
            time.sleep(1.0 / FRAME_RATE)

    def get_frame(self):
        with self.lock:
            if not self.running or self.last_frame is None:
                raise RuntimeError("Camera not running or no frame available")
            frame = self.last_frame.copy()
        return frame

    def capture_image(self, ext="jpeg", width=None, height=None):
        frame = self.get_frame()
        if width and height:
            frame = cv2.resize(frame, (width, height))
        encode_param = []
        if ext.lower() == "jpeg" or ext.lower() == "jpg":
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            ret, buf = cv2.imencode(".jpg", frame, encode_param)
        elif ext.lower() == "png":
            ret, buf = cv2.imencode(".png", frame)
        else:
            raise ValueError("Unsupported image format")
        if not ret:
            raise RuntimeError("Failed to encode image")
        return io.BytesIO(buf.tobytes()), ext.lower()

    def mjpeg_stream(self, width=None, height=None, quality=None):
        while self.running:
            try:
                frame = self.get_frame()
                if width and height:
                    frame = cv2.resize(frame, (width, height))
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality or JPEG_QUALITY]
                ret, jpeg = cv2.imencode('.jpg', frame, encode_param)
                if not ret:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                time.sleep(1.0 / FRAME_RATE)
            except Exception:
                break

camera_manager = CameraManager()

@app.route("/capture", methods=["POST", "DELETE"])
def capture_session():
    if request.method == "POST":
        session_id = camera_manager.start_capture()
        return jsonify({"status": "started", "session_id": session_id})
    elif request.method == "DELETE":
        camera_manager.stop_capture()
        return jsonify({"status": "stopped"})
    else:
        abort(405)

@app.route("/camera/start", methods=["POST"])
def camera_start():
    session_id = camera_manager.start_capture()
    return jsonify({"status": "started", "session_id": session_id})

@app.route("/camera/stop", methods=["POST"])
def camera_stop():
    camera_manager.stop_capture()
    return jsonify({"status": "stopped"})

@app.route("/frame", methods=["GET"])
def get_frame():
    if not camera_manager.running:
        abort(409, description="Camera is not capturing.")
    ext = request.args.get("format", "jpeg").lower()
    try:
        width = int(request.args.get("width")) if request.args.get("width") else None
        height = int(request.args.get("height")) if request.args.get("height") else None
        img_io, img_ext = camera_manager.capture_image(ext, width, height)
        mime = "image/jpeg" if img_ext in ["jpeg", "jpg"] else "image/png"
        return send_file(img_io, mimetype=mime)
    except Exception as e:
        abort(500, description=str(e))

@app.route("/camera/capture", methods=["POST"])
def camera_capture():
    if not camera_manager.running:
        abort(409, description="Camera is not capturing.")
    ext = request.args.get("format", "jpeg").lower()
    try:
        width = int(request.args.get("width")) if request.args.get("width") else None
        height = int(request.args.get("height")) if request.args.get("height") else None
        img_io, img_ext = camera_manager.capture_image(ext, width, height)
        mime = "image/jpeg" if img_ext in ["jpeg", "jpg"] else "image/png"
        return send_file(img_io, mimetype=mime)
    except Exception as e:
        abort(500, description=str(e))

@app.route("/stream", methods=["GET"])
@app.route("/camera/stream", methods=["GET"])
def stream_video():
    if not camera_manager.running:
        abort(409, description="Camera is not capturing.")
    width = int(request.args.get("width")) if request.args.get("width") else None
    height = int(request.args.get("height")) if request.args.get("height") else None
    quality = int(request.args.get("quality")) if request.args.get("quality") else None
    return Response(
        camera_manager.mjpeg_stream(width, height, quality),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)