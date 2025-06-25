import os
import cv2
import threading
import time
import io
import numpy as np
from flask import Flask, Response, request, jsonify, send_file, stream_with_context

app = Flask(__name__)

# --- Configuration (from environment variables) ---
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
MJPEG_QUALITY = int(os.environ.get("MJPEG_QUALITY", "80"))
CAPTURE_IMAGE_FORMAT = os.environ.get("CAPTURE_IMAGE_FORMAT", "jpeg").lower()  # jpeg, png, etc.

# --- Camera/Stream State ---
camera_lock = threading.Lock()
camera = None
streaming = False
stream_thread = None
stream_frame = None
stream_stop_event = threading.Event()
capture_mode = False
capture_thread = None
capture_stop_event = threading.Event()
capture_interval = float(os.environ.get("CAPTURE_INTERVAL", "1.0"))  # seconds
last_captured_image = None
last_captured_timestamp = None

def open_camera():
    global camera
    if camera is None:
        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            return None
        camera = cap
    return camera

def release_camera():
    global camera
    if camera is not None:
        camera.release()
        camera = None

# --- Stream Thread ---

def stream_worker():
    global stream_frame
    cam = open_camera()
    while not stream_stop_event.is_set():
        ret, frame = cam.read()
        if not ret:
            time.sleep(0.1)
            continue
        # Encode as JPEG for MJPEG streaming
        ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), MJPEG_QUALITY])
        if ret:
            stream_frame = jpeg.tobytes()
        time.sleep(0.01)
    # Don't release camera here - might be used by others

# --- Capture Mode Thread ---

def capture_worker():
    global last_captured_image, last_captured_timestamp
    cam = open_camera()
    while not capture_stop_event.is_set():
        ret, frame = cam.read()
        if not ret:
            time.sleep(0.1)
            continue
        ret, img = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), MJPEG_QUALITY])
        if ret:
            last_captured_image = img.tobytes()
            last_captured_timestamp = time.time()
        time.sleep(capture_interval)
    # Don't release camera here - might be used by others

# --- API Endpoints ---

@app.route("/cam/capture/start", methods=["POST"])
def start_capture_mode():
    global capture_mode, capture_thread, capture_stop_event
    with camera_lock:
        if not capture_mode:
            capture_stop_event.clear()
            capture_thread = threading.Thread(target=capture_worker, daemon=True)
            capture_mode = True
            capture_thread.start()
    return jsonify({"status": "capture mode started"}), 200

@app.route("/cam/capture/stop", methods=["POST"])
def stop_capture_mode():
    global capture_mode, capture_thread, capture_stop_event
    with camera_lock:
        if capture_mode:
            capture_stop_event.set()
            capture_mode = False
            capture_thread = None
    return jsonify({"status": "capture mode stopped"}), 200

@app.route("/cam/stream/start", methods=["POST"])
def cam_stream_start():
    return start_stream_internal()

@app.route("/camera/startStream", methods=["POST"])
def camera_start_stream():
    return start_stream_internal()

def start_stream_internal():
    global streaming, stream_thread, stream_stop_event
    with camera_lock:
        if not streaming:
            stream_stop_event.clear()
            stream_thread = threading.Thread(target=stream_worker, daemon=True)
            streaming = True
            stream_thread.start()
    stream_url = f"http://{SERVER_HOST}:{SERVER_PORT}/cam/stream"
    return jsonify({"status": "stream started", "stream_url": stream_url, "format": "mjpeg"}), 200

@app.route("/cam/stream/stop", methods=["POST"])
def cam_stream_stop():
    return stop_stream_internal()

@app.route("/camera/stopStream", methods=["POST"])
def camera_stop_stream():
    return stop_stream_internal()

def stop_stream_internal():
    global streaming, stream_thread, stream_stop_event
    with camera_lock:
        if streaming:
            stream_stop_event.set()
            streaming = False
            stream_thread = None
    return jsonify({"status": "stream stopped"}), 200

def mjpeg_stream_generator():
    global stream_frame
    while streaming:
        frame = stream_frame
        if frame is None:
            time.sleep(0.01)
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.03)  # ~30 FPS

@app.route("/cam/stream", methods=["GET"])
def cam_stream():
    if not streaming:
        return jsonify({"error": "stream not started"}), 400
    return Response(stream_with_context(mjpeg_stream_generator()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    # Returns a JSON with the stream URL (for polling or HLS-style clients)
    if not streaming:
        return jsonify({"error": "stream not started"}), 400
    stream_url = f"http://{SERVER_HOST}:{SERVER_PORT}/cam/stream"
    return jsonify({"stream_url": stream_url, "format": "mjpeg"}), 200

@app.route("/cam/snap", methods=["GET"])
def cam_snap():
    cam = open_camera()
    ret, frame = cam.read()
    if not ret:
        return jsonify({"error": "failed to capture image"}), 500
    ret, img = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), MJPEG_QUALITY])
    if not ret:
        return jsonify({"error": "failed to encode image"}), 500
    image_bytes = img.tobytes()
    return send_file(
        io.BytesIO(image_bytes),
        mimetype="image/jpeg",
        as_attachment=False,
        download_name="snap.jpg"
    )

@app.route("/camera/capture", methods=["POST"])
def camera_capture():
    cam = open_camera()
    ret, frame = cam.read()
    if not ret:
        return jsonify({"error": "failed to capture image"}), 500
    # Choose format based on request or env
    fmt = request.args.get("format", CAPTURE_IMAGE_FORMAT)
    if fmt == "jpeg" or fmt == "jpg":
        ret, img = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), MJPEG_QUALITY])
        mime = "image/jpeg"
        ext = "jpg"
    elif fmt == "png":
        ret, img = cv2.imencode('.png', frame)
        mime = "image/png"
        ext = "png"
    else:
        # Default to jpeg
        ret, img = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), MJPEG_QUALITY])
        mime = "image/jpeg"
        ext = "jpg"
    if not ret:
        return jsonify({"error": "failed to encode image"}), 500
    image_bytes = img.tobytes()
    # Return as base64 or binary URL
    buf = io.BytesIO(image_bytes)
    buf.seek(0)
    ts = int(time.time())
    fname = f"capture_{ts}.{ext}"
    return send_file(
        buf,
        mimetype=mime,
        as_attachment=True,
        download_name=fname
    )

# --- Main ---

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)