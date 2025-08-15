import os
import io
import time
import threading
from datetime import datetime
from flask import Flask, Response, jsonify, request

import cv2

# Environment variables for configuration
SERVER_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.getenv('SERVER_PORT', '8080'))
CAMERA_INDEX = int(os.getenv('CAMERA_INDEX', '0'))
STREAM_FPS = int(os.getenv('STREAM_FPS', '10'))
CAPTURE_IMAGE_FORMAT = os.getenv('CAPTURE_IMAGE_FORMAT', 'jpeg').lower()  # 'jpeg' or 'png'

app = Flask(__name__)

class CameraStreamHandler:
    def __init__(self, camera_index=0, fps=10):
        self.camera_index = camera_index
        self.fps = fps
        self.capture = None
        self.is_streaming = False
        self.lock = threading.Lock()
        self.thread = None

    def start_stream(self):
        with self.lock:
            if not self.is_streaming:
                if self.capture is None:
                    self.capture = cv2.VideoCapture(self.camera_index)
                self.is_streaming = True
        return True

    def stop_stream(self):
        with self.lock:
            if self.is_streaming:
                self.is_streaming = False
                if self.capture is not None:
                    self.capture.release()
                    self.capture = None
        return True

    def get_frame(self, image_format='jpeg'):
        if self.capture is None:
            self.capture = cv2.VideoCapture(self.camera_index)
        ret, frame = self.capture.read()
        if not ret or frame is None:
            return None, None
        if image_format == 'png':
            ret, buffer = cv2.imencode('.png', frame)
            fmt = 'png'
        else:
            ret, buffer = cv2.imencode('.jpg', frame)
            fmt = 'jpeg'
        if not ret:
            return None, None
        return buffer.tobytes(), fmt

    def mjpeg_stream_generator(self):
        interval = 1.0 / self.fps
        while True:
            with self.lock:
                if not self.is_streaming:
                    break
                if self.capture is None:
                    self.capture = cv2.VideoCapture(self.camera_index)
            frame, fmt = self.get_frame('jpeg')
            if frame is None:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(interval)

camera_handler = CameraStreamHandler(camera_index=CAMERA_INDEX, fps=STREAM_FPS)

DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"]
}

@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify({
        "device_info": DEVICE_INFO,
        "status": "connected",
        "camera_index": CAMERA_INDEX
    })

@app.route('/camera/capture', methods=['POST'])
@app.route('/capture', methods=['POST'])
def capture_image():
    image_format = request.args.get('format', CAPTURE_IMAGE_FORMAT)
    frame, fmt = camera_handler.get_frame(image_format)
    if frame is None:
        return jsonify({"error": "Failed to capture image"}), 500
    timestamp = datetime.utcnow().isoformat() + 'Z'
    mimetype = 'image/png' if fmt == 'png' else 'image/jpeg'
    return Response(frame, mimetype=mimetype,
                    headers={
                        "X-Image-Format": fmt,
                        "X-Timestamp": timestamp
                    })

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    camera_handler.start_stream()
    return jsonify({"status": "streaming", "message": "Camera stream started."})

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    camera_handler.stop_stream()
    return jsonify({"status": "stopped", "message": "Camera stream stopped."})

@app.route('/camera/stream', methods=['GET'])
def stream_video():
    if not camera_handler.is_streaming:
        return jsonify({"error": "Stream not started"}), 400
    return Response(camera_handler.mjpeg_stream_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)