import os
import cv2
import threading
import time
import io
import datetime
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

app = Flask(__name__)

# Environment Variables
CAMERA_INDEX = int(os.environ.get("DEVICE_CAMERA_INDEX", 0))  # Default to /dev/video0
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8000))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", 640))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", 480))
CAPTURE_IMAGE_FORMAT = os.environ.get("CAPTURE_IMAGE_FORMAT", "jpeg").lower()  # jpeg or png
STREAM_FPS = float(os.environ.get("STREAM_FPS", 15))

DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264 (converted)"],
    "data_points": ["video stream", "image capture"],
}

class CameraStream:
    def __init__(self, camera_index, width, height, fps):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self.vcap = None
        self.lock = threading.Lock()
        self.streaming = False
        self.thread = None
        self.frame = None
        self.last_access = time.time()
        self.stop_event = threading.Event()

    def start(self):
        with self.lock:
            if not self.streaming:
                self.vcap = cv2.VideoCapture(self.camera_index)
                self.vcap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.vcap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.vcap.set(cv2.CAP_PROP_FPS, self.fps)
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._update, daemon=True)
                self.streaming = True
                self.thread.start()
                return True
            return False

    def _update(self):
        while not self.stop_event.is_set():
            ret, frame = self.vcap.read()
            if not ret:
                continue
            with self.lock:
                self.frame = frame
                self.last_access = time.time()
            time.sleep(1.0 / self.fps)
        self.vcap.release()
        with self.lock:
            self.streaming = False
            self.frame = None

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        with self.lock:
            if self.streaming:
                self.stop_event.set()
                self.thread.join(timeout=2)
                self.streaming = False
                self.frame = None
                return True
            return False

    def is_streaming(self):
        with self.lock:
            return self.streaming

camera_stream = CameraStream(CAMERA_INDEX, FRAME_WIDTH, FRAME_HEIGHT, STREAM_FPS)

def gen_mjpeg_stream():
    while camera_stream.is_streaming():
        frame = camera_stream.get_frame()
        if frame is not None:
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            jpg_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpg_bytes + b'\r\n')
        else:
            time.sleep(0.01)

@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify(DEVICE_INFO)

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    started = camera_stream.start()
    if started or camera_stream.is_streaming():
        return jsonify({"status": "streaming", "message": "Camera stream started.", "stream_url": "/video_feed"}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to start camera stream."}), 500

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    stopped = camera_stream.stop()
    if stopped or not camera_stream.is_streaming():
        return jsonify({"status": "stopped", "message": "Camera stream stopped."}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to stop camera stream."}), 500

@app.route('/video_feed')
def video_feed():
    if not camera_stream.is_streaming():
        return jsonify({"status": "error", "message": "Camera stream is not started."}), 400
    return Response(gen_mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    was_streaming = camera_stream.is_streaming()
    if not was_streaming:
        camera_stream.start()
        # wait for camera warmup
        time.sleep(0.3)
    frame = camera_stream.get_frame()
    if frame is None:
        if not was_streaming:
            camera_stream.stop()
        return jsonify({"status": "error", "message": "Failed to capture frame from camera."}), 500

    # Encode frame
    ext = '.jpg' if CAPTURE_IMAGE_FORMAT == 'jpeg' else '.png'
    mimetype = 'image/jpeg' if CAPTURE_IMAGE_FORMAT == 'jpeg' else 'image/png'
    ret, buf = cv2.imencode(ext, frame)
    if not ret:
        if not was_streaming:
            camera_stream.stop()
        return jsonify({"status": "error", "message": "Failed to encode image."}), 500
    img_bytes = buf.tobytes()
    timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
    img_filename = f"capture_{timestamp.replace(':', '').replace('.', '')}{ext}"

    meta = {
        "format": CAPTURE_IMAGE_FORMAT,
        "timestamp": timestamp,
        "filename": img_filename,
        "content_type": mimetype,
        "size_bytes": len(img_bytes),
    }

    if 'download' in request.args and request.args.get('download').lower() == "true":
        file_obj = io.BytesIO(img_bytes)
        file_obj.seek(0)
        if not was_streaming:
            camera_stream.stop()
        return send_file(
            file_obj,
            mimetype=mimetype,
            as_attachment=True,
            download_name=img_filename
        )

    if not was_streaming:
        camera_stream.stop()
    return Response(img_bytes, mimetype=mimetype, headers={
        "X-Image-Meta": str(meta)
    })

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)