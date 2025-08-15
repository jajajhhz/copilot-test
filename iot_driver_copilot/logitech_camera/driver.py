import os
import cv2
import threading
import time
import io
import json
from datetime import datetime
from flask import Flask, Response, jsonify, send_file, request

# ---- Environment Variable Configuration ----
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CAPTURE_IMAGE_FORMAT = os.environ.get("CAPTURE_IMAGE_FORMAT", "jpeg").lower()  # jpeg or png
STREAM_FRAME_WIDTH = int(os.environ.get("STREAM_FRAME_WIDTH", "640"))
STREAM_FRAME_HEIGHT = int(os.environ.get("STREAM_FRAME_HEIGHT", "480"))
STREAM_FRAME_RATE = int(os.environ.get("STREAM_FRAME_RATE", "20"))

DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"],
}

# ---- Camera Streaming and Capture Logic ----

class CameraManager:
    def __init__(self, camera_index):
        self.camera_index = camera_index
        self.lock = threading.Lock()
        self.cap = None
        self.streaming = False
        self.frame = None
        self.thread = None
        self.stop_event = threading.Event()

    def start_capture(self):
        with self.lock:
            if self.cap is None:
                self.cap = cv2.VideoCapture(self.camera_index)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, STREAM_FRAME_WIDTH)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, STREAM_FRAME_HEIGHT)
                self.cap.set(cv2.CAP_PROP_FPS, STREAM_FRAME_RATE)
        return self.cap.isOpened()

    def release_capture(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None

    def get_frame(self):
        if self.cap is None:
            if not self.start_capture():
                return None
        ret, frame = self.cap.read()
        if not ret:
            return None
        return frame

    def capture_image(self, img_format="jpeg"):
        frame = self.get_frame()
        if frame is None:
            return None, None
        ext = '.jpg' if img_format == "jpeg" else '.png'
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if img_format == "jpeg" else [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
        ret, buffer = cv2.imencode(ext, frame, encode_param)
        if not ret:
            return None, None
        return buffer.tobytes(), ext[1:]

    def start_streaming(self):
        if self.streaming:
            return
        self.stop_event.clear()
        self.streaming = True
        if self.cap is None:
            self.start_capture()
        self.thread = threading.Thread(target=self.update_frames, daemon=True)
        self.thread.start()

    def update_frames(self):
        while self.streaming and not self.stop_event.is_set():
            frame = self.get_frame()
            with self.lock:
                self.frame = frame
            time.sleep(1.0 / STREAM_FRAME_RATE)

    def stop_streaming(self):
        self.streaming = False
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
        self.release_capture()

    def generate_mjpeg(self):
        while self.streaming:
            with self.lock:
                frame = self.frame
            if frame is None:
                time.sleep(0.05)
                continue
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / STREAM_FRAME_RATE)

camera_manager = CameraManager(CAMERA_INDEX)

# ---- Flask App ----
app = Flask(__name__)

@app.route("/camera/info", methods=["GET"])
def camera_info():
    return jsonify(DEVICE_INFO), 200

@app.route("/capture", methods=["POST"])
@app.route("/camera/capture", methods=["POST"])
def capture_image():
    img_format = CAPTURE_IMAGE_FORMAT
    if request.is_json:
        data = request.get_json()
        img_format = data.get('format', img_format)
    img_format = img_format.lower()
    if img_format not in ("jpeg", "png"):
        img_format = "jpeg"
    img_bytes, ext = camera_manager.capture_image(img_format)
    if img_bytes is None:
        return jsonify({"success": False, "error": "Failed to capture image"}), 500
    ts = datetime.utcnow().isoformat() + "Z"
    filename = f"capture_{ts.replace(':','-').replace('.','_')}.{ext}"
    return send_file(
        io.BytesIO(img_bytes),
        mimetype=f"image/{ext}",
        as_attachment=True,
        download_name=filename,
        headers={
            "X-Timestamp": ts,
            "X-Image-Format": ext
        }
    )

@app.route("/stream/start", methods=["POST"])
@app.route("/camera/stream/start", methods=["POST"])
def start_stream():
    camera_manager.start_streaming()
    return jsonify({
        "success": True,
        "status": "streaming",
        "stream_url": f"http://{HTTP_HOST}:{HTTP_PORT}/stream"
    }), 200

@app.route("/stream/stop", methods=["POST"])
@app.route("/camera/stream/stop", methods=["POST"])
def stop_stream():
    camera_manager.stop_streaming()
    return jsonify({
        "success": True,
        "status": "stopped"
    }), 200

@app.route("/stream", methods=["GET"])
def stream_video():
    if not camera_manager.streaming:
        camera_manager.start_streaming()
    return Response(
        camera_manager.generate_mjpeg(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)