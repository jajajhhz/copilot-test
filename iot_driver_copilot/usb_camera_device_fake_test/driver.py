import os
import threading
import io
import time
from flask import Flask, Response, jsonify, send_file, stream_with_context

import cv2

# Configuration from environment variables
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8080'))
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))  # USB camera index

app = Flask(__name__)

# Global streaming state
streaming = False
stream_thread = None
camera_lock = threading.Lock()
last_frame = None
last_frame_time = 0
bitrate = 0  # bits/sec, updated dynamically

# Utility to start camera capture thread
class CameraStreamer(threading.Thread):
    def __init__(self, camera_index):
        super().__init__()
        self.camera_index = camera_index
        self.running = False
        self.capture = None
        self.fps = 0
        self.last_bitrate = 0

    def run(self):
        global last_frame, last_frame_time, bitrate
        self.running = True
        self.capture = cv2.VideoCapture(self.camera_index)
        if not self.capture.isOpened():
            self.running = False
            return
        frame_count = 0
        byte_count = 0
        start_time = time.time()
        while self.running:
            ret, frame = self.capture.read()
            if not ret:
                continue
            # Encode as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            jpg_bytes = jpeg.tobytes()
            with camera_lock:
                last_frame = jpg_bytes
                last_frame_time = time.time()
            byte_count += len(jpg_bytes)
            frame_count += 1
            # Update bitrate every second
            elapsed = time.time() - start_time
            if elapsed >= 1.0:
                self.fps = frame_count / elapsed
                self.last_bitrate = int(byte_count * 8 / elapsed)
                bitrate = self.last_bitrate
                frame_count = 0
                byte_count = 0
                start_time = time.time()
        if self.capture:
            self.capture.release()

    def stop(self):
        self.running = False

    def get_bitrate(self):
        return self.last_bitrate

    def get_fps(self):
        return self.fps

camera_streamer = None

def start_streaming():
    global streaming, camera_streamer
    if streaming:
        return False
    camera_streamer = CameraStreamer(CAMERA_INDEX)
    camera_streamer.setDaemon(True)
    camera_streamer.start()
    streaming = True
    return True

def stop_streaming():
    global streaming, camera_streamer
    if not streaming:
        return False
    if camera_streamer:
        camera_streamer.stop()
        camera_streamer = None
    streaming = False
    return True

def gen_mjpeg():
    global last_frame
    while streaming:
        with camera_lock:
            frame = last_frame
        if frame is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        else:
            time.sleep(0.05)

@app.route('/stream', methods=['GET'])
def get_stream_status():
    status = {
        "streaming": streaming,
        "bitrate": bitrate if streaming else 0,
        "stream_url": f"http://{os.environ.get('HTTP_HOST', 'localhost')}:{HTTP_PORT}/camera/stream" if streaming else None,
        "fps": camera_streamer.get_fps() if streaming and camera_streamer else 0,
        "device": "USB camera device fake test"
    }
    return jsonify(status)

@app.route('/camera/start', methods=['POST'])
@app.route('/stream/start', methods=['POST'])
def start_camera_stream():
    if start_streaming():
        return jsonify({"status": "streaming started", "stream_url": f"http://{os.environ.get('HTTP_HOST', 'localhost')}:{HTTP_PORT}/camera/stream"}), 200
    return jsonify({"status": "already streaming", "stream_url": f"http://{os.environ.get('HTTP_HOST', 'localhost')}:{HTTP_PORT}/camera/stream"}), 200

@app.route('/camera/stop', methods=['POST'])
@app.route('/stream/stop', methods=['POST'])
def stop_camera_stream():
    if stop_streaming():
        return jsonify({"status": "streaming stopped"}), 200
    return jsonify({"status": "not streaming"}), 200

@app.route('/camera/stream', methods=['GET'])
def mjpeg_stream():
    if not streaming:
        return jsonify({"error": "stream is not active"}), 503
    return Response(stream_with_context(gen_mjpeg()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    if not streaming:
        # Try to grab one frame from camera directly
        capture = cv2.VideoCapture(CAMERA_INDEX)
        ret, frame = capture.read()
        capture.release()
        if not ret:
            return jsonify({"error": "failed to capture image"}), 500
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            return jsonify({"error": "failed to encode image"}), 500
        jpg_bytes = jpeg.tobytes()
    else:
        with camera_lock:
            jpg_bytes = last_frame
        if jpg_bytes is None:
            return jsonify({"error": "no frame available"}), 500
    # Return image as JPEG file
    return Response(jpg_bytes, mimetype='image/jpeg', headers={'Content-Disposition': 'inline; filename="capture.jpg"'})

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)