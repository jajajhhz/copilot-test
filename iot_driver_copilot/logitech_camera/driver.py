```python
import os
import cv2
import threading
import time
import io
import datetime
from flask import Flask, Response, jsonify, request, send_file

# Configuration via environment variables
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
STREAM_FPS = float(os.environ.get("STREAM_FPS", "15"))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "480"))
CAPTURE_IMAGE_FORMAT = os.environ.get("CAPTURE_IMAGE_FORMAT", "jpeg").lower()  # jpeg or png

app = Flask(__name__)

# Camera and streaming control
camera_lock = threading.Lock()
camera = None
streaming = False
frame_buffer = None
stream_thread = None
stream_clients = set()
stream_stop_event = threading.Event()

DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"],
    "commands": [
        "start stream", "stop stream", "capture image"
    ]
}

def open_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

def close_camera():
    global camera
    with camera_lock:
        if camera is not None and camera.isOpened():
            camera.release()
            camera = None

def capture_frame():
    open_camera()
    with camera_lock:
        ret, frame = camera.read()
    if not ret or frame is None:
        raise RuntimeError("Failed to capture image from camera")
    return frame

def encode_image(frame, fmt):
    if fmt == "png":
        encode_param = [cv2.IMWRITE_PNG_COMPRESSION, 3]
        ext = ".png"
        mimetype = "image/png"
    else:
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
        ext = ".jpg"
        mimetype = "image/jpeg"
    ret, buf = cv2.imencode(ext, frame, encode_param)
    if not ret:
        raise RuntimeError("Failed to encode image")
    return buf.tobytes(), mimetype, ext

def mjpeg_stream():
    global streaming, stream_stop_event
    try:
        open_camera()
        while streaming and not stream_stop_event.is_set():
            with camera_lock:
                ret, frame = camera.read()
            if not ret or frame is None:
                continue
            img, _, _ = encode_image(frame, "jpeg")
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + img + b'\r\n')
            time.sleep(1.0 / STREAM_FPS)
    finally:
        # Only close camera if not streaming
        if not streaming:
            close_camera()

def start_streaming():
    global streaming, stream_stop_event, stream_thread
    if not streaming:
        streaming = True
        stream_stop_event.clear()

def stop_streaming():
    global streaming, stream_stop_event
    streaming = False
    stream_stop_event.set()
    close_camera()

@app.route("/camera/info", methods=["GET"])
def camera_info():
    return jsonify(DEVICE_INFO)

@app.route("/capture", methods=["POST"])
@app.route("/camera/capture", methods=["POST"])
def capture_image():
    try:
        frame = capture_frame()
        img_bytes, mimetype, ext = encode_image(frame, CAPTURE_IMAGE_FORMAT)
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        filename = f"capture_{timestamp.replace(':','-').replace('.','_')}{ext}"
        buf = io.BytesIO(img_bytes)
        buf.seek(0)
        return send_file(
            buf,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
            headers={
                "X-Image-Timestamp": timestamp,
                "X-Image-Format": mimetype
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stream/start", methods=["POST"])
@app.route("/camera/stream/start", methods=["POST"])
def start_stream():
    global streaming
    start_streaming()
    return jsonify({"status": "streaming_started"})

@app.route("/stream/stop", methods=["POST"])
@app.route("/camera/stream/stop", methods=["POST"])
def stop_stream():
    global streaming
    stop_streaming()
    return jsonify({"status": "streaming_stopped"})

@app.route("/video_feed")
def video_feed():
    if not streaming:
        return jsonify({"error": "Streaming not started. POST /stream/start first."}), 400
    return Response(
        mjpeg_stream(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.before_first_request
def setup():
    close_camera()

@app.teardown_appcontext
def shutdown(exception):
    stop_streaming()
    close_camera()

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
```
