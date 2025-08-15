import os
import io
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_file, abort
import cv2

# Load env configuration
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "480"))
FRAME_RATE = int(os.environ.get("FRAME_RATE", "15"))
STREAM_FORMAT = os.environ.get("STREAM_FORMAT", "MJPEG").upper()
CAPTURE_FORMAT = os.environ.get("CAPTURE_FORMAT", "JPEG").upper()

app = Flask(__name__)

# Camera Info
DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"]
}

# Global camera/video stream state
camera_lock = threading.Lock()
camera = None
streaming = False
stream_thread = None

def open_camera():
    global camera
    with camera_lock:
        if camera is None:
            camera = cv2.VideoCapture(CAMERA_INDEX)
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            camera.set(cv2.CAP_PROP_FPS, FRAME_RATE)
            # Select MJPEG if available
            if STREAM_FORMAT == "MJPEG":
                camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            elif STREAM_FORMAT == "YUYV":
                camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
            # H.264 not supported in OpenCV for USB, fallback to MJPEG
        if not camera.isOpened():
            raise RuntimeError("Unable to open camera.")

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def gen_mjpeg_stream():
    global camera, streaming
    try:
        open_camera()
        while streaming:
            ret, frame = camera.read()
            if not ret:
                continue
            # MJPEG: encode frame as JPEG
            ret2, jpeg = cv2.imencode('.jpg', frame)
            if not ret2:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / FRAME_RATE)
    finally:
        release_camera()

@app.route('/camera/info', methods=['GET'])
def get_camera_info():
    return jsonify(DEVICE_INFO)

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    global streaming, stream_thread
    if streaming:
        return jsonify({"status": "already streaming"}), 200
    streaming = True
    return jsonify({"status": "streaming started"}), 200

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    global streaming
    streaming = False
    release_camera()
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/camera/stream', methods=['GET'])
def mjpeg_stream():
    global streaming
    if not streaming:
        abort(404, description="Stream not started. Use /stream/start")
    return Response(gen_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    global camera
    try:
        open_camera()
        ret, frame = camera.read()
        if not ret:
            release_camera()
            return jsonify({"error": "Failed to capture image"}), 500
        timestamp = datetime.utcnow().isoformat() + "Z"
        # Convert to JPEG or PNG
        if CAPTURE_FORMAT == "PNG":
            ret2, buf = cv2.imencode('.png', frame)
            img_format = "PNG"
        else:
            ret2, buf = cv2.imencode('.jpg', frame)
            img_format = "JPEG"
        if not ret2:
            release_camera()
            return jsonify({"error": "Image encoding failed"}), 500
        img_bytes = buf.tobytes()
        release_camera()
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=f'image/{img_format.lower()}',
            as_attachment=False,
            download_name=f"capture_{timestamp}.{img_format.lower()}",
            headers={
                "X-Capture-Timestamp": timestamp,
                "X-Capture-Format": img_format
            }
        )
    except Exception as e:
        release_camera()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)