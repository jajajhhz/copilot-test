import os
import io
import cv2
import threading
import time
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from datetime import datetime

# Get configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))
IMAGE_FORMAT = os.environ.get('IMAGE_FORMAT', 'jpeg').lower()  # 'jpeg' or 'png'
FRAME_WIDTH = int(os.environ.get('FRAME_WIDTH', '640'))
FRAME_HEIGHT = int(os.environ.get('FRAME_HEIGHT', '480'))
FRAME_RATE = int(os.environ.get('FRAME_RATE', '15'))

# Device info
DEVICE_INFO = {
    "device_name": "Logitech Camera",
    "device_model": "Logitech Camera",
    "manufacturer": "Logitech",
    "device_type": "Camera",
    "supported_formats": ["MJPEG", "YUYV", "H.264", "JPEG", "PNG"]
}

app = Flask(__name__)

# Global state for streaming
streaming_state = {
    "active": False,
    "thread": None,
    "lock": threading.Lock(),
    "frame": None,
    "last_frame_time": 0,
    "camera": None
}


def get_camera():
    camera = cv2.VideoCapture(CAMERA_INDEX)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    camera.set(cv2.CAP_PROP_FPS, FRAME_RATE)
    return camera


def release_camera(camera):
    if camera is not None and camera.isOpened():
        camera.release()


def read_frame(camera):
    if not camera.isOpened():
        camera.open(CAMERA_INDEX)
    ret, frame = camera.read()
    return frame if ret else None


def encode_image(frame, ext='.jpg'):
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if ext == '.jpg' else [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    ret, buf = cv2.imencode(ext, frame, encode_param)
    if not ret:
        return None
    return buf.tobytes()


# Background streaming thread
def streaming_loop():
    camera = get_camera()
    with streaming_state["lock"]:
        streaming_state["camera"] = camera
    try:
        while streaming_state["active"]:
            frame = read_frame(camera)
            if frame is not None:
                with streaming_state["lock"]:
                    streaming_state["frame"] = frame
                    streaming_state["last_frame_time"] = time.time()
            time.sleep(1.0 / FRAME_RATE)
    finally:
        release_camera(camera)
        with streaming_state["lock"]:
            streaming_state["camera"] = None
            streaming_state["frame"] = None


def start_streaming_thread():
    with streaming_state["lock"]:
        if not streaming_state["active"]:
            streaming_state["active"] = True
            streaming_state["thread"] = threading.Thread(target=streaming_loop, daemon=True)
            streaming_state["thread"].start()


def stop_streaming_thread():
    with streaming_state["lock"]:
        streaming_state["active"] = False
        thread = streaming_state["thread"]
    if thread is not None:
        thread.join(timeout=1.0)
    with streaming_state["lock"]:
        streaming_state["thread"] = None
        streaming_state["frame"] = None
    # Camera will be released in streaming_loop


@app.route('/camera/info', methods=['GET'])
def camera_info():
    return jsonify(DEVICE_INFO)


@app.route('/camera/capture', methods=['POST'])
@app.route('/capture', methods=['POST'])
def camera_capture():
    camera = get_camera()
    frame = read_frame(camera)
    release_camera(camera)
    if frame is None:
        return jsonify({"success": False, "error": "Could not capture image"}), 500
    ext = '.jpg' if IMAGE_FORMAT == 'jpeg' else '.png'
    img_bytes = encode_image(frame, ext=ext)
    if img_bytes is None:
        return jsonify({"success": False, "error": "Image encoding failed"}), 500

    timestamp = datetime.utcnow().isoformat() + 'Z'
    meta = {
        "format": IMAGE_FORMAT.upper(),
        "timestamp": timestamp,
        "size_bytes": len(img_bytes),
    }
    response = send_file(
        io.BytesIO(img_bytes),
        mimetype='image/jpeg' if IMAGE_FORMAT == 'jpeg' else 'image/png',
        as_attachment=True,
        download_name=f'capture_{timestamp.replace(":", "-")}{ext}'
    )
    response.headers['X-Capture-Metadata'] = str(meta)
    return response


def mjpeg_stream_generator():
    while True:
        with streaming_state["lock"]:
            frame = streaming_state["frame"]
            active = streaming_state["active"]
        if not active:
            break
        if frame is not None:
            img_bytes = encode_image(frame, ext='.jpg')
            if img_bytes is not None:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
        time.sleep(1.0 / FRAME_RATE)


@app.route('/stream/start', methods=['POST'])
@app.route('/camera/stream/start', methods=['POST'])
def stream_start():
    start_streaming_thread()
    return jsonify({"success": True, "message": "Streaming started"})


@app.route('/stream/stop', methods=['POST'])
@app.route('/camera/stream/stop', methods=['POST'])
def stream_stop():
    stop_streaming_thread()
    return jsonify({"success": True, "message": "Streaming stopped"})


@app.route('/video_feed', methods=['GET'])
def video_feed():
    with streaming_state["lock"]:
        if not streaming_state["active"]:
            return jsonify({"success": False, "error": "Streaming not started"}), 400
    return Response(
        stream_with_context(mjpeg_stream_generator()),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)