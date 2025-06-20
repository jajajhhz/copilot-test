import os
import cv2
import threading
import time
import io
import json
import base64
from flask import Flask, Response, request, jsonify, send_file

app = Flask(__name__)

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))

# Supported formats
SUPPORTED_FORMATS = ['JPEG', 'MJPEG', 'YUV']

# Camera control
camera = None
camera_lock = threading.Lock()
streaming = False
stream_thread = None

# For video streaming
def open_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(CAMERA_INDEX)
    return camera

def release_camera():
    global camera
    if camera is not None:
        camera.release()
        camera = None

def generate_mjpeg():
    while streaming:
        with camera_lock:
            cam = open_camera()
            ret, frame = cam.read()
        if not ret:
            break
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.04)  # ~25 FPS

def generate_yuv():
    while streaming:
        with camera_lock:
            cam = open_camera()
            ret, frame = cam.read()
        if not ret:
            break
        yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
        buf = yuv.tobytes()
        yield buf
        time.sleep(0.04)

def generate_jpeg():
    with camera_lock:
        cam = open_camera()
        ret, frame = cam.read()
    if not ret:
        return None
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return None
    return jpeg.tobytes()

@app.route('/stream/start', methods=['POST'])
def start_stream():
    global streaming, stream_thread
    if streaming:
        return jsonify({'status': 'already streaming', 'stream_url': '/stream'}), 200
    with camera_lock:
        open_camera()
    streaming = True
    return jsonify({'status': 'stream started', 'stream_url': '/stream'}), 200

@app.route('/stream/stop', methods=['POST'])
def stop_stream():
    global streaming
    if not streaming:
        return jsonify({'status': 'not streaming'}), 200
    streaming = False
    time.sleep(0.1)
    with camera_lock:
        release_camera()
    return jsonify({'status': 'stream stopped'}), 200

@app.route('/capture', methods=['POST', 'GET'])
def capture_image():
    img_bytes = generate_jpeg()
    if img_bytes is None:
        return jsonify({'status': 'error', 'message': 'Failed to capture image.'}), 500
    if request.method == 'GET':
        # Return as JSON (base64)
        encoded = base64.b64encode(img_bytes).decode('utf-8')
        metadata = {
            "format": "JPEG",
            "bytes": len(img_bytes),
            "timestamp": int(time.time())
        }
        return jsonify({
            "status": "success",
            "image": encoded,
            "metadata": metadata
        }), 200
    else:
        # POST: return raw JPEG image
        return Response(img_bytes, mimetype='image/jpeg',
                        headers={'Content-Disposition': 'inline; filename="capture.jpg"'})

@app.route('/stream', methods=['GET'])
def video_stream():
    fmt = request.args.get('format', 'MJPEG').upper()
    if fmt not in SUPPORTED_FORMATS:
        return jsonify({'status': 'error', 'message': f'Format {fmt} not supported.'}), 400
    if not streaming:
        return jsonify({'status': 'error', 'message': 'Stream not started.'}), 400
    if fmt == 'MJPEG':
        return Response(generate_mjpeg(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    elif fmt == 'JPEG':
        img_bytes = generate_jpeg()
        if img_bytes is None:
            return jsonify({'status': 'error', 'message': 'Failed to capture image.'}), 500
        return Response(img_bytes, mimetype='image/jpeg')
    elif fmt == 'YUV':
        return Response(generate_yuv(), mimetype='application/octet-stream')
    else:
        return jsonify({'status': 'error', 'message': 'Unsupported format.'}), 400

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)