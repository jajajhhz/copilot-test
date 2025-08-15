import os
import io
import time
import threading
from datetime import datetime
from flask import Flask, Response, jsonify, request, send_file
import cv2

# Environment Variables
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "90"))

app = Flask(__name__)

device_info = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264"],
    "data_points": ["video stream", "image capture"],
    "commands": [
        "start stream", "stop stream", "capture image"
    ]
}

# Camera and streaming state
camera_lock = threading.Lock()
camera = None
streaming = False
stream_thread = None
stream_clients = set()
stream_clients_lock = threading.Lock()

def open_camera():
    global camera
    if camera is None or not camera.isOpened():
        cam = cv2.VideoCapture(CAMERA_INDEX)
        # Try to set preferred format, fallback if fails
        cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        if not cam.isOpened():
            cam.release()
            raise RuntimeError("Could not open camera.")
        camera = cam

def close_camera():
    global camera
    if camera is not None:
        camera.release()
        camera = None

def get_frame():
    with camera_lock:
        open_camera()
        ret, frame = camera.read()
        if not ret:
            raise RuntimeError("Failed to capture image from camera.")
        return frame

def encode_image(frame, ext=".jpg"):
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY] if ext == ".jpg" else []
    ret, buf = cv2.imencode(ext, frame, encode_param)
    if not ret:
        raise RuntimeError("Failed to encode image.")
    return buf.tobytes(), ext

def frame_generator():
    global streaming
    while streaming:
        try:
            frame = get_frame()
            img, _ = encode_image(frame, ".jpg")
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + img + b'\r\n')
            time.sleep(0.04)  # ~25fps
        except Exception:
            break

@app.route("/camera/info", methods=["GET"])
def camera_info():
    return jsonify({
        "device_name": device_info["device_name"],
        "device_model": device_info["device_model"],
        "manufacturer": device_info["manufacturer"],
        "device_type": device_info["device_type"],
        "supported_formats": device_info["supported_formats"],
        "data_points": device_info["data_points"],
        "commands": device_info["commands"]
    })

@app.route("/stream/start", methods=["POST"])
@app.route("/camera/stream/start", methods=["POST"])
def start_stream():
    global streaming, stream_thread
    with camera_lock:
        if not streaming:
            streaming = True
    resp = {
        "status": "streaming started",
        "stream_url": f"http://{SERVER_HOST}:{SERVER_PORT}/stream/live"
    }
    return jsonify(resp), 200

@app.route("/stream/stop", methods=["POST"])
@app.route("/camera/stream/stop", methods=["POST"])
def stop_stream():
    global streaming
    with camera_lock:
        if streaming:
            streaming = False
    return jsonify({"status": "streaming stopped"}), 200

@app.route("/stream/live")
def stream_live():
    global streaming
    with camera_lock:
        if not streaming:
            return Response("Stream is not active.", status=404)
    def gen():
        for frame in frame_generator():
            yield frame
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/capture", methods=["POST"])
@app.route("/camera/capture", methods=["POST"])
def capture_image():
    format_ = request.args.get("format", "jpg").lower()
    if format_ not in ["jpg", "jpeg", "png"]:
        return jsonify({"error": "Unsupported format. Use jpg or png."}), 400
    ext = ".jpg" if format_ in ["jpg", "jpeg"] else ".png"
    frame = get_frame()
    img_bytes, ext = encode_image(frame, ext)
    timestamp = datetime.utcnow().isoformat() + "Z"
    img_io = io.BytesIO(img_bytes)
    img_io.seek(0)
    return send_file(
        img_io,
        mimetype=f"image/{format_}",
        as_attachment=False,
        download_name=f"capture_{int(time.time())}{ext}",
        headers={
            "X-Image-Format": format_,
            "X-Timestamp": timestamp
        }
    )

if __name__ == "__main__":
    try:
        app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
    finally:
        close_camera()