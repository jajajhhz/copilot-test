import os
import cv2
import threading
import time
import io
import numpy as np
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

# Environment Variables
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAPTURE_IMAGE_FORMAT = os.environ.get("CAPTURE_IMAGE_FORMAT", "jpg")
STREAM_FORMAT = os.environ.get("STREAM_FORMAT", "mjpeg")  # Options: mjpeg
CAPTURE_INTERVAL = float(os.environ.get("CAPTURE_INTERVAL", "1.0"))  # seconds between captures in capture mode

# Camera State
camera = None
camera_lock = threading.Lock()
capture_mode = False
capture_thread = None
streaming_mode = False
streaming_lock = threading.Lock()
last_frame = None


app = Flask(__name__)

def open_camera():
    global camera
    with camera_lock:
        if camera is None:
            camera = cv2.VideoCapture(CAMERA_INDEX)
    return camera

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def get_frame():
    cam = open_camera()
    if not cam.isOpened():
        raise RuntimeError("Camera not accessible")
    ret, frame = cam.read()
    if not ret:
        raise RuntimeError("Failed to read frame from camera")
    return frame

def encode_image(frame, fmt):
    if fmt == "jpg" or fmt == "jpeg":
        ret, buf = cv2.imencode('.jpg', frame)
        mimetype = 'image/jpeg'
    elif fmt == "png":
        ret, buf = cv2.imencode('.png', frame)
        mimetype = 'image/png'
    else:
        raise ValueError("Unsupported image format")
    if not ret:
        raise RuntimeError("Failed to encode image")
    return buf.tobytes(), mimetype

#########################
# Capture Mode Handlers #
#########################

capture_continuous_flag = threading.Event()
captured_images = []

def capture_continuous_images():
    global captured_images
    capture_continuous_flag.set()
    while capture_continuous_flag.is_set():
        try:
            frame = get_frame()
            img_bytes, _ = encode_image(frame, CAPTURE_IMAGE_FORMAT)
            captured_images.append(img_bytes)
            # Keep only the last 10 for demonstration
            if len(captured_images) > 10:
                captured_images = captured_images[-10:]
        except Exception:
            pass
        time.sleep(CAPTURE_INTERVAL)

@app.route("/cam/capture/start", methods=["POST"])
def start_capture():
    global capture_thread, capture_continuous_flag
    if capture_continuous_flag.is_set():
        return jsonify({"status": "already capturing"}), 200
    captured_images.clear()
    capture_thread = threading.Thread(target=capture_continuous_images, daemon=True)
    capture_thread.start()
    return jsonify({"status": "capture started"}), 200

@app.route("/cam/capture/stop", methods=["POST"])
def stop_capture():
    global capture_thread, capture_continuous_flag
    capture_continuous_flag.clear()
    capture_thread = None
    return jsonify({"status": "capture stopped"}), 200

@app.route("/camera/capture", methods=["POST"])
def camera_capture():
    """Capture a single image from the camera."""
    try:
        frame = get_frame()
        img_bytes, mimetype = encode_image(frame, CAPTURE_IMAGE_FORMAT)
        # Save to a BytesIO object for Flask's send_file
        img_io = io.BytesIO(img_bytes)
        img_io.seek(0)
        return send_file(img_io, mimetype=mimetype)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/cam/snap", methods=["GET"])
def cam_snap():
    """Capture a single image and return as base64 in JSON."""
    try:
        import base64
        frame = get_frame()
        img_bytes, mimetype = encode_image(frame, CAPTURE_IMAGE_FORMAT)
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        return jsonify({"format": mimetype, "image_base64": img_b64}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

########################
# MJPEG Stream Handler #
########################

def mjpeg_stream_generator():
    global streaming_mode, last_frame
    cam = open_camera()
    if not cam.isOpened():
        raise RuntimeError("Camera not accessible")
    with streaming_lock:
        streaming_mode = True
    try:
        while streaming_mode:
            ret, frame = cam.read()
            if not ret:
                continue
            last_frame = frame
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(0.04)  # ~25 fps
    finally:
        with streaming_lock:
            streaming_mode = False

@app.route("/cam/stream", methods=["GET"])
def cam_stream():
    """Provides a live MJPEG stream from the USB camera."""
    return Response(stream_with_context(mjpeg_stream_generator()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    """Returns stream details in JSON (for browser/cli to use /cam/stream)."""
    stream_url = f"http://{HTTP_HOST}:{HTTP_PORT}/cam/stream"
    return jsonify({
        "stream_url": stream_url,
        "format": "mjpeg"
    })

@app.route("/cam/stream/start", methods=["POST"])
def cam_stream_start():
    """Start the background stream mode (no-op for MJPEG, stream is always available)."""
    with streaming_lock:
        if streaming_mode:
            return jsonify({"status": "already streaming"}), 200
        # Set streaming_mode to True but actual streaming is handled on GET
        global streaming_mode
        streaming_mode = True
    return jsonify({"status": "stream started"}), 200

@app.route("/cam/stream/stop", methods=["POST"])
def cam_stream_stop():
    """Stop the background stream mode (for MJPEG immediately stops streaming)."""
    with streaming_lock:
        global streaming_mode
        streaming_mode = False
    return jsonify({"status": "stream stopped"}), 200

@app.route("/camera/startStream", methods=["POST"])
def camera_start_stream():
    """Start the video streaming session."""
    with streaming_lock:
        if streaming_mode:
            return jsonify({"status": "already streaming"}), 200
        global streaming_mode
        streaming_mode = True
    stream_url = f"http://{HTTP_HOST}:{HTTP_PORT}/cam/stream"
    return jsonify({
        "status": "stream started",
        "stream_url": stream_url,
        "format": "mjpeg"
    })

@app.route("/camera/stopStream", methods=["POST"])
def camera_stop_stream():
    with streaming_lock:
        global streaming_mode
        streaming_mode = False
    return jsonify({"status": "stream stopped"}), 200

#########################
# Graceful App Shutdown #
#########################

import signal
import sys

def cleanup(*args):
    release_camera()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

###################
# Main Entrypoint #
###################

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)