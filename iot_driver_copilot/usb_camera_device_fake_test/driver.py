import os
import io
import threading
import time
from flask import Flask, Response, jsonify, send_file, url_for, request
import cv2

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8080'))

app = Flask(__name__)

# Camera and stream state
camera_lock = threading.Lock()
camera = None
streaming = False
stream_thread = None
latest_frame = None
stream_bitrate = 0
frame_count = 0
stream_start_time = 0

def open_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            camera = cv2.VideoCapture(CAMERA_INDEX)
            # Optional: set resolution or other camera parameters here

def close_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def generate_mjpeg_stream():
    global latest_frame, streaming, stream_bitrate, frame_count, stream_start_time
    open_camera()
    stream_start_time = time.time()
    frame_count = 0
    streaming = True
    try:
        while streaming:
            with camera_lock:
                if camera is None or not camera.isOpened():
                    break
                ret, frame = camera.read()
            if not ret:
                time.sleep(0.02)
                continue
            # Encode frame as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            latest_frame = frame_bytes
            frame_count += 1
            # Estimate bitrate every few seconds
            if frame_count % 30 == 0:
                elapsed = time.time() - stream_start_time
                if elapsed > 0:
                    stream_bitrate = int((frame_count * len(frame_bytes) * 8) / elapsed)
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.03)  # ~30 FPS
    finally:
        streaming = False

@app.route('/stream', methods=['GET'])
def get_stream_status():
    meta = {
        "streaming": streaming,
        "stream_url": url_for('camera_stream', _external=True),
        "bitrate_bps": stream_bitrate if streaming else 0,
        "frame_rate": (frame_count / (time.time() - stream_start_time)) if streaming and stream_start_time else 0,
        "camera_index": CAMERA_INDEX
    }
    return jsonify(meta)

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/start', methods=['POST'])
def start_stream():
    global streaming, stream_thread
    if streaming:
        return jsonify({"status": "already streaming", "stream_url": url_for('camera_stream', _external=True)}), 200
    open_camera()
    streaming = True
    return jsonify({
        "status": "stream started",
        "stream_url": url_for('camera_stream', _external=True),
        "camera_index": CAMERA_INDEX
    }), 200

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stop', methods=['POST'])
def stop_stream():
    global streaming
    streaming = False
    close_camera()
    return jsonify({"status": "stream stopped"}), 200

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    open_camera()
    with camera_lock:
        if camera is None or not camera.isOpened():
            return jsonify({"error": "Camera not available"}), 503
        ret, frame = camera.read()
    if not ret:
        return jsonify({"error": "Failed to capture image"}), 500
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return jsonify({"error": "Encoding failed"}), 500
    image_bytes = jpeg.tobytes()
    # Return as binary JPEG
    return Response(image_bytes, mimetype='image/jpeg',
                    headers={'Content-Disposition': 'inline; filename="capture.jpg"'})

@app.route('/camera/stream', methods=['GET'])
def camera_stream():
    if not streaming:
        return jsonify({"error": "Stream not started"}), 400
    return Response(generate_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def cleanup():
    close_camera()

import atexit
atexit.register(cleanup)

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)