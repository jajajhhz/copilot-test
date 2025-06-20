import os
import io
import cv2
import threading
import time
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# Environment config
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8000'))
HTTP_DEBUG = os.environ.get('HTTP_DEBUG', 'false').lower() == 'true'
DEFAULT_CAMERA_INDEX = int(os.environ.get('DEFAULT_CAMERA_INDEX', '0'))
FRAME_WIDTH = int(os.environ.get('FRAME_WIDTH', '640'))
FRAME_HEIGHT = int(os.environ.get('FRAME_HEIGHT', '480'))
FRAME_RATE = int(os.environ.get('FRAME_RATE', '24'))

camera_lock = threading.Lock()
camera_index = DEFAULT_CAMERA_INDEX
capture = None
streaming = False
stream_thread = None
frame_buffer = None
frame_time = 0

def list_usb_cameras(max_devices=10):
    cameras = []
    for idx in range(max_devices):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW if os.name == 'nt' else cv2.CAP_ANY)
        if cap is not None and cap.isOpened():
            cameras.append({
                "index": idx,
                "name": f"Camera {idx}",
            })
            cap.release()
    return cameras

def open_camera(index):
    global capture
    if capture is not None:
        capture.release()
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == 'nt' else cv2.CAP_ANY)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FRAME_RATE)
    if not cap.isOpened():
        return None
    return cap

def stream_frames():
    global capture, streaming, frame_buffer, frame_time
    while streaming:
        ret, frame = capture.read()
        if not ret:
            continue
        # JPEG compress for browser
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        with camera_lock:
            frame_buffer = jpeg.tobytes()
            frame_time = time.time()
        time.sleep(1.0 / FRAME_RATE)

def get_latest_frame():
    with camera_lock:
        return frame_buffer

@app.route('/cameras', methods=['GET'])
def api_list_cameras():
    cameras = list_usb_cameras()
    return jsonify({"cameras": cameras})

@app.route('/stream/start', methods=['POST'])
def api_stream_start():
    global capture, streaming, stream_thread, camera_index

    req = request.get_json(silent=True)
    cam_idx = req.get('index') if req and 'index' in req else camera_index
    width = req.get('width') if req and 'width' in req else FRAME_WIDTH
    height = req.get('height') if req and 'height' in req else FRAME_HEIGHT
    fps = req.get('fps') if req and 'fps' in req else FRAME_RATE

    with camera_lock:
        if streaming:
            return jsonify({"message": "Stream already running", "stream_url": "/stream"}), 200
        cap = open_camera(cam_idx)
        if cap is None:
            return jsonify({"error": "Unable to open camera"}), 400
        camera_index = cam_idx
        capture = cap
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, fps)
        streaming = True
        stream_thread = threading.Thread(target=stream_frames, daemon=True)
        stream_thread.start()
    return jsonify({"message": "Stream started", "stream_url": "/stream"}), 200

@app.route('/stream/stop', methods=['POST'])
def api_stream_stop():
    global streaming, stream_thread, capture
    with camera_lock:
        if not streaming:
            return jsonify({"message": "Stream not running"}), 200
        streaming = False
        if stream_thread:
            stream_thread.join(timeout=2)
            stream_thread = None
        if capture is not None:
            capture.release()
            capture = None
    return jsonify({"message": "Stream stopped"}), 200

@app.route('/stream', methods=['GET'])
def api_stream():
    def gen():
        while streaming:
            frame = get_latest_frame()
            if frame is not None:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.05)
    if not streaming:
        return jsonify({"error": "Stream not started"}), 400
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
def api_capture():
    global capture
    if not streaming or capture is None:
        return jsonify({"error": "Stream not started"}), 400
    ret, frame = capture.read()
    if not ret:
        return jsonify({"error": "Capture failed"}), 500
    _, img_encoded = cv2.imencode('.jpg', frame)
    return Response(img_encoded.tobytes(), mimetype='image/jpeg',
                    headers={"Content-Disposition": "inline; filename=capture.jpg"})

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=HTTP_DEBUG, threaded=True)