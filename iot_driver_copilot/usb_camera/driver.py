import os
import io
import threading
import time
import base64
from flask import Flask, Response, jsonify, request, stream_with_context
import cv2

# Configuration from environment variables
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', '8080'))
USB_CAMERA_INDEX = int(os.environ.get('USB_CAMERA_INDEX', '0'))  # Usually 0 for default USB camera

app = Flask(__name__)

# Shared state for streaming
stream_lock = threading.Lock()
streaming = False
camera = None
frame = None
last_frame_time = 0
frame_rate = int(os.environ.get('CAMERA_FRAME_RATE', '20'))  # FPS for MJPEG stream
stream_thread = None

def open_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(USB_CAMERA_INDEX)
    return camera

def release_camera():
    global camera
    if camera is not None and camera.isOpened():
        camera.release()
        camera = None

def generate_mjpeg():
    global streaming, frame, last_frame_time, camera
    while streaming:
        cam = open_camera()
        ret, img = cam.read()
        if not ret:
            continue
        ret, jpeg = cv2.imencode('.jpg', img)
        if not ret:
            continue
        frame = jpeg.tobytes()
        last_frame_time = time.time()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(1.0 / frame_rate)

def stream_thread_func():
    global streaming, frame, last_frame_time, camera
    cam = open_camera()
    while streaming:
        ret, img = cam.read()
        if not ret:
            continue
        ret, jpeg = cv2.imencode('.jpg', img)
        if not ret:
            continue
        with stream_lock:
            frame = jpeg.tobytes()
            last_frame_time = time.time()
        time.sleep(1.0 / frame_rate)
    release_camera()

@app.route('/stream/start', methods=['POST'])
def start_stream():
    global streaming, stream_thread
    if streaming:
        return jsonify({'status': 'already streaming', 'stream_url': '/stream'}), 200
    streaming = True
    stream_thread = threading.Thread(target=stream_thread_func, daemon=True)
    stream_thread.start()
    return jsonify({'status': 'stream started', 'stream_url': '/stream'}), 200

@app.route('/stream/stop', methods=['POST'])
def stop_stream():
    global streaming, stream_thread
    if not streaming:
        return jsonify({'status': 'not streaming'}), 200
    streaming = False
    if stream_thread is not None:
        stream_thread.join(timeout=2)
    return jsonify({'status': 'stream stopped'}), 200

@app.route('/stream', methods=['GET'])
def stream():
    fmt = request.args.get('format', 'mjpeg').lower()
    if not streaming:
        return jsonify({'error': 'stream is not started'}), 400
    if fmt in ['mjpeg', 'jpeg']:
        return Response(stream_with_context(generate_mjpeg()),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    elif fmt == 'yuv':
        def generate_yuv():
            while streaming:
                cam = open_camera()
                ret, img = cam.read()
                if not ret:
                    continue
                yuv = cv2.cvtColor(img, cv2.COLOR_BGR2YUV)
                yuv_bytes = yuv.tobytes()
                yield yuv_bytes
                time.sleep(1.0 / frame_rate)
        return Response(stream_with_context(generate_yuv()), mimetype='application/octet-stream')
    else:
        return jsonify({'error': 'unsupported format'}), 400

@app.route('/capture', methods=['POST', 'GET'])
def capture():
    cam = open_camera()
    ret, img = cam.read()
    if not ret:
        return jsonify({'error': 'failed to capture image'}), 500
    ret, jpeg = cv2.imencode('.jpg', img)
    if not ret:
        return jsonify({'error': 'failed to encode image'}), 500

    img_bytes = jpeg.tobytes()

    if request.method == 'POST':
        return Response(img_bytes, mimetype='image/jpeg',
                        headers={'Content-Disposition': 'inline; filename="capture.jpg"'})
    else:
        img_b64 = base64.b64encode(img_bytes).decode('ascii')
        return jsonify({
            'image': img_b64,
            'format': 'jpeg',
            'timestamp': time.time(),
            'width': img.shape[1],
            'height': img.shape[0]
        })

def shutdown_hook():
    global streaming
    streaming = False
    release_camera()

import atexit
atexit.register(shutdown_hook)

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)