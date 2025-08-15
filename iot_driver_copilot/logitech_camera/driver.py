import os
import io
import cv2
import threading
import time
import json
from flask import Flask, Response, jsonify, request

# Environment Variables
HTTP_HOST = os.getenv('SHIFU_HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.getenv('SHIFU_HTTP_PORT', '8080'))
CAMERA_INDEX = int(os.getenv('SHIFU_CAMERA_INDEX', '0'))
FRAME_WIDTH = int(os.getenv('SHIFU_FRAME_WIDTH', '640'))
FRAME_HEIGHT = int(os.getenv('SHIFU_FRAME_HEIGHT', '480'))
FRAME_RATE = int(os.getenv('SHIFU_FRAME_RATE', '15'))
IMAGE_FORMAT = os.getenv('SHIFU_IMAGE_FORMAT', 'jpeg').lower()  # 'jpeg' or 'png'

app = Flask(__name__)

# Device Info
DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"]
}

# Internal State
stream_active = threading.Event()
stream_lock = threading.Lock()
camera_capture = None
last_frame = None
last_frame_time = None


def get_camera():
    global camera_capture
    if camera_capture is None:
        camera_capture = cv2.VideoCapture(CAMERA_INDEX)
        camera_capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        camera_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        camera_capture.set(cv2.CAP_PROP_FPS, FRAME_RATE)
    return camera_capture


def release_camera():
    global camera_capture
    if camera_capture is not None:
        camera_capture.release()
        camera_capture = None


def encode_image(frame, fmt):
    if fmt == 'png':
        ret, buf = cv2.imencode('.png', frame)
        mime_type = 'image/png'
    else:
        ret, buf = cv2.imencode('.jpg', frame)
        mime_type = 'image/jpeg'
    return (buf.tobytes(), mime_type) if ret else (None, None)


def gen_mjpeg_stream():
    global last_frame, last_frame_time
    camera = get_camera()
    while stream_active.is_set():
        ret, frame = camera.read()
        if not ret:
            continue
        last_frame = frame
        last_frame_time = time.time()
        img_bytes, _ = encode_image(frame, 'jpeg')
        if img_bytes is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
        time.sleep(1.0 / FRAME_RATE)


@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify({
        "device_name": DEVICE_INFO["device_name"],
        "device_model": DEVICE_INFO["device_model"],
        "manufacturer": DEVICE_INFO["manufacturer"],
        "device_type": DEVICE_INFO["device_type"],
        "supported_formats": DEVICE_INFO["supported_formats"],
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
        "frame_rate": FRAME_RATE
    })


@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    with stream_lock:
        if not stream_active.is_set():
            stream_active.set()
        return jsonify({"status": "streaming", "message": "Camera streaming started."}), 200


@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    with stream_lock:
        if stream_active.is_set():
            stream_active.clear()
            release_camera()
        return jsonify({"status": "stopped", "message": "Camera streaming stopped."}), 200


@app.route('/camera/capture', methods=['POST'])
@app.route('/capture', methods=['POST'])
def capture_image():
    fmt = IMAGE_FORMAT
    camera = get_camera()
    ret, frame = camera.read()
    if not ret:
        return jsonify({"error": "Failed to capture image"}), 500
    img_bytes, mime_type = encode_image(frame, fmt)
    if img_bytes is None:
        return jsonify({"error": "Image encoding failed"}), 500
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    return Response(img_bytes, mimetype=mime_type, headers={
        'X-Image-Timestamp': timestamp,
        'X-Image-Format': fmt.upper()
    })


@app.route('/camera/stream')
def mjpeg_stream():
    if not stream_active.is_set():
        return jsonify({"error": "Stream not active. Start stream first."}), 400
    return Response(gen_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)