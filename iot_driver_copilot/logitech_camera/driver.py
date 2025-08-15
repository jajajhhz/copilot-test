import os
import io
import cv2
import time
import threading
from flask import Flask, Response, jsonify, send_file, request

# Environment Variables
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
CAMERA_DEVICE = int(os.getenv("CAMERA_DEVICE", "0"))
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.getenv("FRAME_HEIGHT", "480"))
FRAME_RATE = int(os.getenv("FRAME_RATE", "24"))
IMAGE_FORMAT = os.getenv("IMAGE_FORMAT", "jpeg").lower()  # jpeg or png

app = Flask(__name__)

# Camera and Streaming Management
class CameraManager:
    def __init__(self, device, width, height, fps):
        self.device_index = device
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.lock = threading.Lock()
        self.streaming = False
        self.last_frame = None
        self.thread = None
        self.thread_stop = threading.Event()

    def open_camera(self):
        if self.cap is None or not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.device_index)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)

    def release_camera(self):
        if self.cap and self.cap.isOpened():
            self.cap.release()
        self.cap = None

    def get_frame(self):
        self.open_camera()
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError("Failed to capture image from camera.")
        self.last_frame = frame
        return frame

    def encode_frame(self, frame, fmt):
        if fmt == "png":
            ret, buf = cv2.imencode('.png', frame)
            mime = "image/png"
        else:
            ret, buf = cv2.imencode('.jpg', frame)
            mime = "image/jpeg"
        if not ret:
            raise RuntimeError("Failed to encode image.")
        return buf.tobytes(), mime

    def capture_image(self, fmt):
        with self.lock:
            frame = self.get_frame()
            img_bytes, mime = self.encode_frame(frame, fmt)
            return img_bytes, mime

    def start_stream(self):
        if self.streaming:
            return
        self.streaming = True
        self.thread_stop.clear()
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._stream_worker, daemon=True)
            self.thread.start()

    def stop_stream(self):
        self.streaming = False
        self.thread_stop.set()
        self.release_camera()

    def _stream_worker(self):
        self.open_camera()
        while self.streaming and not self.thread_stop.is_set():
            try:
                ret, frame = self.cap.read()
                if not ret:
                    continue
                with self.lock:
                    self.last_frame = frame
                time.sleep(1.0 / max(self.fps, 1))
            except Exception:
                continue
        self.release_camera()

    def generate_mjpeg(self):
        while self.streaming:
            with self.lock:
                frame = self.last_frame
            if frame is not None:
                img_bytes, _ = self.encode_frame(frame, "jpeg")
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
            time.sleep(1.0 / max(self.fps, 1))

camera = CameraManager(CAMERA_DEVICE, FRAME_WIDTH, FRAME_HEIGHT, FRAME_RATE)

# API Endpoints

@app.route('/camera/info', methods=['GET'])
def camera_info():
    info = {
        "device_name": "Logitech Camera",
        "device_model": "Logitech Camera",
        "manufacturer": "Logitech",
        "device_type": "Camera",
        "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"],
        "commands": ["start stream", "stop stream", "capture image"],
    }
    return jsonify(info)

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    camera.start_stream()
    return jsonify({"status": "streaming started"}), 200

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    camera.stop_stream()
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/video_feed', methods=['GET'])
def video_feed():
    if not camera.streaming:
        camera.start_stream()
    return Response(camera.generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/capture', methods=['POST'])
@app.route('/capture', methods=['POST'])
def capture():
    fmt = request.args.get('format', IMAGE_FORMAT)
    if fmt not in ["jpeg", "png"]:
        fmt = IMAGE_FORMAT
    img_bytes, mime = camera.capture_image(fmt)
    ts = int(time.time() * 1000)
    ext = "jpg" if fmt == "jpeg" else "png"
    return send_file(
        io.BytesIO(img_bytes),
        mimetype=mime,
        as_attachment=True,
        download_name=f"capture_{ts}.{ext}",
        headers={
            "X-Timestamp": str(ts),
            "X-Image-Format": fmt
        }
    )

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)