import os
import io
import cv2
import time
import threading
from datetime import datetime
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

# Environment Variables
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))  # USB camera index (default 0)
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "480"))
IMAGE_FORMAT = os.environ.get("IMAGE_FORMAT", "jpeg").lower()  # jpeg or png

app = Flask(__name__)

# Globals for streaming control
streaming = False
streaming_lock = threading.Lock()
camera = None

def initialize_camera():
    global camera
    if camera is None:
        cam = cv2.VideoCapture(CAMERA_INDEX)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        if not cam.isOpened():
            raise RuntimeError("Unable to open camera at index %d" % CAMERA_INDEX)
        camera = cam
    return camera

def release_camera():
    global camera
    if camera is not None:
        camera.release()
        camera = None

def get_image(format='jpeg'):
    cam = initialize_camera()
    ret, frame = cam.read()
    if not ret:
        raise RuntimeError("Failed to capture image from camera")
    img_ext = '.jpg' if format == 'jpeg' else '.png'
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 95] if format == 'jpeg' else [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    ret2, img = cv2.imencode(img_ext, frame, encode_param)
    if not ret2:
        raise RuntimeError("Failed to encode image")
    return img.tobytes(), format

def mjpeg_stream_gen():
    global streaming
    cam = initialize_camera()
    try:
        while True:
            with streaming_lock:
                if not streaming:
                    break
            ret, frame = cam.read()
            if not ret:
                continue
            ret2, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ret2:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
    finally:
        pass  # Do not release camera here, may be reused

@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify({
        "device_name": "Logitech Camera",
        "device_model": "Logitech Camera",
        "manufacturer": "Logitech",
        "device_type": "Camera",
        "supported_formats": ["MJPEG", "YUYV", "H.264"],
        "image_formats": ["JPEG", "PNG"],
        "streaming_endpoint": "/stream/start (POST), /camera/stream/start (POST)",
        "capture_endpoint": "/capture (POST), /camera/capture (POST)"
    })

@app.route('/camera/capture', methods=['POST'])
@app.route('/capture', methods=['POST'])
def capture_image():
    try:
        fmt = request.args.get("format", IMAGE_FORMAT)
        img_bytes, fmt = get_image(fmt)
        now = datetime.utcnow().isoformat() + "Z"
        filename = f"capture_{int(time.time())}.{fmt}"
        buf = io.BytesIO(img_bytes)
        buf.seek(0)
        return send_file(
            buf,
            mimetype=f"image/{fmt}",
            as_attachment=True,
            download_name=filename,
            etag=now,
            last_modified=time.time(),
            add_etags=True,
            conditional=True
        ), 200, {
            "X-Image-Format": fmt,
            "X-Timestamp": now
        }
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    global streaming
    with streaming_lock:
        if streaming:
            return jsonify({"status": "already streaming", "url": "/stream/video"}), 200
        streaming = True
    # Do not start the actual stream in background; the client will GET /stream/video
    return jsonify({"status": "streaming started", "stream_url": "/stream/video"}), 200

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    global streaming
    with streaming_lock:
        if not streaming:
            return jsonify({"status": "not streaming"}), 200
        streaming = False
    time.sleep(0.2)
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/stream/video', methods=['GET'])
def stream_video():
    global streaming
    with streaming_lock:
        if not streaming:
            return jsonify({"error": "stream not started. POST /stream/start first."}), 400
    return Response(stream_with_context(mjpeg_stream_gen()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "message": "Logitech Camera Driver",
        "endpoints": [
            {"path": "/camera/info", "method": "GET", "description": "Camera information"},
            {"path": "/camera/capture", "method": "POST", "description": "Capture image"},
            {"path": "/capture", "method": "POST", "description": "Capture image"},
            {"path": "/stream/start", "method": "POST", "description": "Start streaming"},
            {"path": "/camera/stream/start", "method": "POST", "description": "Start streaming"},
            {"path": "/stream/stop", "method": "POST", "description": "Stop streaming"},
            {"path": "/camera/stream/stop", "method": "POST", "description": "Stop streaming"},
            {"path": "/stream/video", "method": "GET", "description": "MJPEG video stream"}
        ]
    })

@app.teardown_appcontext
def cleanup(exception=None):
    release_camera()

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)