import os
import cv2
import threading
import time
import io
import logging
from datetime import datetime
from flask import Flask, Response, jsonify, request, send_file

# Load configuration from environment variables
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
DEFAULT_IMAGE_FORMAT = os.environ.get("IMAGE_FORMAT", "jpeg").lower()  # 'jpeg' or 'png'

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Device Info
DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"],
    "connection": "USB"
}

# Global state for streaming
streaming_lock = threading.Lock()
streaming_active = False
streaming_thread = None
streaming_clients = set()

def get_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("Cannot open camera device.")
    return cap

def encode_image(frame, fmt):
    if fmt == 'jpeg':
        success, encoded = cv2.imencode('.jpg', frame)
        mime = 'image/jpeg'
        ext = 'jpg'
    elif fmt == 'png':
        success, encoded = cv2.imencode('.png', frame)
        mime = 'image/png'
        ext = 'png'
    else:
        raise ValueError("Unsupported format: {}".format(fmt))
    if not success:
        raise RuntimeError("Image encoding failed.")
    return encoded.tobytes(), mime, ext

@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify(DEVICE_INFO)

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    fmt = request.args.get("format", DEFAULT_IMAGE_FORMAT)
    try:
        cap = get_camera()
        time.sleep(0.3)  # allow camera warmup
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return jsonify({"error": "Failed to capture image"}), 500
        img_bytes, mime, ext = encode_image(frame, fmt)
        timestamp = datetime.utcnow().isoformat() + "Z"
        filename = f"logitech_capture_{timestamp.replace(':', '').replace('.', '')}.{ext}"
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=mime,
            as_attachment=True,
            download_name=filename,
            conditional=False
        ), 200, {
            "X-Image-Timestamp": timestamp,
            "X-Image-Format": ext
        }
    except Exception as e:
        logging.exception("Image capture error")
        return jsonify({"error": str(e)}), 500

def mjpeg_stream_generator():
    global streaming_active
    try:
        cap = get_camera()
        while streaming_active:
            ret, frame = cap.read()
            if not ret:
                continue
            img_bytes, _, _ = encode_image(frame, "jpeg")
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
            time.sleep(0.04)  # ~25fps
        cap.release()
    except Exception as e:
        logging.exception("MJPEG stream generator error")
        yield b''

def start_streaming():
    global streaming_active
    with streaming_lock:
        if streaming_active:
            return False
        streaming_active = True
    return True

def stop_streaming():
    global streaming_active
    with streaming_lock:
        streaming_active = False
    return True

@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def start_stream():
    started = start_streaming()
    if not started:
        return jsonify({"status": "already streaming"}), 200
    return jsonify({"status": "streaming started"}), 200

@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stop_stream():
    stopped = stop_streaming()
    return jsonify({"status": "streaming stopped"}), 200

@app.route('/stream')
def stream():
    if not streaming_active:
        return jsonify({"error": "Streaming is not active. Start with /stream/start."}), 409
    return Response(mjpeg_stream_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)