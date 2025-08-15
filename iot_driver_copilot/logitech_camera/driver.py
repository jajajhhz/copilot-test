import os
import threading
import time
from datetime import datetime
from io import BytesIO

from flask import Flask, Response, request, jsonify

import cv2
import numpy as np

# Load config from env
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.environ.get("CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "480"))
CAMERA_FPS = int(os.environ.get("CAMERA_FPS", "15"))
IMAGE_FORMAT = os.environ.get("IMAGE_FORMAT", "jpeg").lower()  # jpeg or png

app = Flask(__name__)

device_info = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"],
    "connection_type": "USB"
}

streaming_lock = threading.Lock()
streaming_active = False
stream_thread = None
frame_buffer = None

def open_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    if not cap.isOpened():
        raise RuntimeError("Unable to open camera at index {}".format(CAMERA_INDEX))
    return cap

def get_frame(format="jpeg"):
    cap = open_camera()
    ret, frame = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError("Failed to capture image from camera")
    if format == "png":
        ret, img_bytes = cv2.imencode(".png", frame)
        mime = "image/png"
    else:
        ret, img_bytes = cv2.imencode(".jpg", frame)
        mime = "image/jpeg"
    if not ret:
        raise RuntimeError("Failed to encode image")
    return img_bytes.tobytes(), mime

def mjpeg_generator():
    global streaming_active, frame_buffer

    cap = open_camera()
    while streaming_active:
        ret, frame = cap.read()
        if not ret:
            continue
        ret, jpg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        frame_buffer = jpg.tobytes()
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame_buffer + b"\r\n")
        time.sleep(1.0 / CAMERA_FPS)
    cap.release()

@app.route("/camera/info", methods=["GET"])
def camera_info():
    return jsonify({
        "device_name": device_info["device_name"],
        "device_model": device_info["device_model"],
        "manufacturer": device_info["manufacturer"],
        "device_type": device_info["device_type"],
        "supported_formats": device_info["supported_formats"],
        "connection_type": device_info["connection_type"]
    })

@app.route("/capture", methods=["POST"])
@app.route("/camera/capture", methods=["POST"])
def capture():
    fmt = request.args.get("format", IMAGE_FORMAT)
    try:
        img_bytes, mime = get_frame(fmt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    timestamp = datetime.utcnow().isoformat() + "Z"
    metadata = {
        "image_format": mime.split('/')[-1],
        "timestamp": timestamp,
        "size": len(img_bytes)
    }
    response = Response(img_bytes, mimetype=mime)
    response.headers["X-Camera-Metadata"] = str(metadata)
    return response

@app.route("/stream/start", methods=["POST"])
@app.route("/camera/stream/start", methods=["POST"])
def stream_start():
    global streaming_active, stream_thread
    with streaming_lock:
        if streaming_active:
            return jsonify({"status": "already streaming"}), 200
        streaming_active = True
    return jsonify({"status": "stream started"}), 200

@app.route("/stream/stop", methods=["POST"])
@app.route("/camera/stream/stop", methods=["POST"])
def stream_stop():
    global streaming_active
    with streaming_lock:
        if not streaming_active:
            return jsonify({"status": "not streaming"}), 200
        streaming_active = False
    return jsonify({"status": "stream stopped"}), 200

@app.route("/stream", methods=["GET"])
def stream():
    global streaming_active
    with streaming_lock:
        if not streaming_active:
            return jsonify({"error": "Stream not started"}), 400
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")

if __name__ == "__main__":
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)