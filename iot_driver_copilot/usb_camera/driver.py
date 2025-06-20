import os
import cv2
import threading
import time
import io
import base64
from flask import Flask, Response, jsonify, request, send_file, make_response

app = Flask(__name__)

# Load configuration from environment variables
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "480"))
CAPTURE_FORMAT = os.environ.get("CAPTURE_FORMAT", "JPEG")  # JPEG, MJPEG, YUV

camera_lock = threading.Lock()
camera = None
streaming = False
stream_thread = None
last_frame = None
last_frame_lock = threading.Lock()

def open_camera():
    global camera
    if camera is None or not camera.isOpened():
        cam = cv2.VideoCapture(CAMERA_INDEX)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        if not cam.isOpened():
            return None
        camera = cam
    return camera

def release_camera():
    global camera
    if camera is not None:
        camera.release()
        camera = None

def camera_stream_worker():
    global last_frame, streaming
    cam = open_camera()
    if cam is None:
        streaming = False
        return
    while streaming:
        ret, frame = cam.read()
        if not ret:
            continue
        with last_frame_lock:
            last_frame = frame
        time.sleep(0.03)  # ~30 fps

def start_streaming():
    global streaming, stream_thread
    with camera_lock:
        if streaming:
            return
        streaming = True
        stream_thread = threading.Thread(target=camera_stream_worker, daemon=True)
        stream_thread.start()

def stop_streaming():
    global streaming, stream_thread
    with camera_lock:
        streaming = False
        if stream_thread is not None:
            stream_thread.join(timeout=1)
            stream_thread = None
        release_camera()

def get_frame(format="JPEG"):
    with last_frame_lock:
        frame = last_frame.copy() if last_frame is not None else None
    if frame is None:
        return None, None
    if format.upper() == "YUV":
        yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
        return yuv.tobytes(), 'application/octet-stream'
    ext = '.jpg' if format.upper() == "JPEG" else '.jpg'
    ret, buf = cv2.imencode(ext, frame)
    if not ret:
        return None, None
    return buf.tobytes(), 'image/jpeg'

def gen_mjpeg():
    while streaming:
        frame_bytes, content_type = get_frame("JPEG")
        if frame_bytes is None:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.03)

@app.route('/stream/start', methods=['POST'])
def api_stream_start():
    start_streaming()
    stream_url = f'http://{HTTP_HOST}:{HTTP_PORT}/stream?format=mjpeg'
    return jsonify({
        "status": "started",
        "stream_url": stream_url
    }), 200

@app.route('/stream/stop', methods=['POST'])
def api_stream_stop():
    stop_streaming()
    return jsonify({"status": "stopped"}), 200

@app.route('/capture', methods=['POST'])
def api_capture_post():
    start_streaming()
    time.sleep(0.1)
    frame_bytes, content_type = get_frame(CAPTURE_FORMAT)
    if frame_bytes is None:
        return jsonify({"error": "Could not capture image"}), 500
    buf = io.BytesIO(frame_bytes)
    buf.seek(0)
    resp = make_response(send_file(buf, mimetype=content_type, as_attachment=False, download_name="capture.jpg"))
    resp.headers['Content-Disposition'] = 'inline; filename="capture.jpg"'
    return resp

@app.route('/capture', methods=['GET'])
def api_capture_get():
    start_streaming()
    time.sleep(0.1)
    frame_bytes, content_type = get_frame(CAPTURE_FORMAT)
    if frame_bytes is None:
        return jsonify({"error": "Could not capture image"}), 500
    img_b64 = base64.b64encode(frame_bytes).decode('utf-8')
    return jsonify({
        "image_base64": img_b64,
        "format": CAPTURE_FORMAT,
        "content_type": content_type,
        "status": "success"
    }), 200

@app.route('/stream', methods=['GET'])
def api_stream():
    req_format = request.args.get('format', CAPTURE_FORMAT).upper()
    if req_format == "MJPEG":
        start_streaming()
        return Response(gen_mjpeg(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    elif req_format == "JPEG":
        start_streaming()
        time.sleep(0.1)
        frame_bytes, content_type = get_frame("JPEG")
        if frame_bytes is None:
            return jsonify({"error": "Could not retrieve frame"}), 500
        buf = io.BytesIO(frame_bytes)
        buf.seek(0)
        resp = make_response(send_file(buf, mimetype=content_type, as_attachment=False, download_name="frame.jpg"))
        resp.headers['Content-Disposition'] = 'inline; filename="frame.jpg"'
        return resp
    elif req_format == "YUV":
        start_streaming()
        time.sleep(0.1)
        frame_bytes, content_type = get_frame("YUV")
        if frame_bytes is None:
            return jsonify({"error": "Could not retrieve frame"}), 500
        resp = make_response(frame_bytes)
        resp.headers['Content-Type'] = 'application/octet-stream'
        resp.headers['Content-Disposition'] = 'inline; filename="frame.yuv"'
        return resp
    else:
        return jsonify({
            "error": "Invalid format",
            "supported_formats": ["JPEG", "MJPEG", "YUV"]
        }), 400

@app.route('/')
def api_root():
    return jsonify({
        "device": "Generic USB Camera",
        "manufacturer": "Generic",
        "model": "Generic USB Camera",
        "streaming": streaming,
        "endpoints": [
            {"method": "POST", "path": "/stream/start", "description": "Initiates video streaming."},
            {"method": "POST", "path": "/stream/stop", "description": "Stops video streaming."},
            {"method": "POST", "path": "/capture", "description": "Captures a still image (JPEG)."},
            {"method": "GET", "path": "/capture", "description": "Captures a still image (Base64 JSON)."},
            {"method": "GET", "path": "/stream", "description": "Fetch live video stream. Format: JPEG, MJPEG, YUV"},
        ]
    })

if __name__ == '__main__':
    try:
        app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
    finally:
        stop_streaming()