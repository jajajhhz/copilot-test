import os
import threading
import time
import io
import cv2
import platform
import numpy as np
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# ENV VARS
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
CAMERA_INDEX = os.environ.get("CAMERA_INDEX", None)  # None means auto-detect, else try this index first
CAMERA_LIST_MAX = int(os.environ.get("CAMERA_LIST_MAX", "10"))
DEFAULT_RES = os.environ.get("DEFAULT_RES", "640x480")
DEFAULT_FORMAT = os.environ.get("DEFAULT_FORMAT", "MJPEG")
DEFAULT_FPS = int(os.environ.get("DEFAULT_FPS", "20"))

# Supported formats
SUPPORTED_IMAGE_FORMATS = ['jpeg', 'jpg', 'png']
SUPPORTED_VIDEO_FORMATS = ['mp4', 'mjpeg']
SUPPORTED_FORMATS = SUPPORTED_IMAGE_FORMATS + SUPPORTED_VIDEO_FORMATS

# Camera State
class CameraState:
    def __init__(self):
        self.lock = threading.Lock()
        self.cap = None
        self.camera_index = None
        self.is_running = False
        self.width, self.height = self.parse_res(DEFAULT_RES)
        self.format = DEFAULT_FORMAT.lower()
        self.fps = DEFAULT_FPS

    @staticmethod
    def parse_res(res_str):
        try:
            w, h = res_str.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 640, 480

    def enumerate_cameras(self):
        indices = []
        for i in range(CAMERA_LIST_MAX):
            cap = cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                indices.append(i)
                cap.release()
        return indices

    def open_camera(self, index=None):
        self.close_camera()
        # Try user-specified index, else enumerate
        indices = [int(index)] if index is not None else self.enumerate_cameras()
        # If user specified and not openable, try others (including 0)
        if not indices:
            indices = [0]
        for idx in indices:
            cap = cv2.VideoCapture(idx)
            if cap is not None and cap.isOpened():
                self.camera_index = idx
                self.cap = cap
                self.set_resolution(self.width, self.height)
                self.set_fps(self.fps)
                return True
            if cap is not None:
                cap.release()
        return False

    def close_camera(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.is_running = False

    def set_resolution(self, width, height):
        self.width = int(width)
        self.height = int(height)
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

    def set_fps(self, fps):
        self.fps = int(fps)
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)

    def set_format(self, fmt):
        fmt = fmt.lower()
        if fmt in SUPPORTED_FORMATS:
            self.format = fmt
            return True
        return False

    def get_frame(self):
        if self.cap is None or not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        if not ret:
            return None
        return frame

camera_state = CameraState()

# ========== Camera control endpoints ==========

@app.route('/cam/start', methods=['POST'])
def start_camera():
    params = request.args
    content = request.get_json(silent=True) or {}
    width, height = camera_state.width, camera_state.height
    fmt = camera_state.format
    fps = camera_state.fps

    # Prefer POST body, then query params
    if 'resolution' in content:
        width, height = CameraState.parse_res(content['resolution'])
    elif 'resolution' in params:
        width, height = CameraState.parse_res(params['resolution'])
    else:
        if 'width' in content and 'height' in content:
            width = int(content['width'])
            height = int(content['height'])
        elif 'width' in params and 'height' in params:
            width = int(params['width'])
            height = int(params['height'])

    if 'format' in content:
        fmt = content['format'].lower()
    elif 'format' in params:
        fmt = params['format'].lower()

    if 'fps' in content:
        fps = int(content['fps'])
    elif 'fps' in params:
        fps = int(params['fps'])

    with camera_state.lock:
        # Try user-specified camera index, else auto
        user_index = CAMERA_INDEX if CAMERA_INDEX is not None else None
        ok = camera_state.open_camera(user_index)
        if not ok:
            # Try fallback
            ok = camera_state.open_camera(None)
        if not ok:
            return jsonify({"error": "No camera device found."}), 404
        camera_state.set_resolution(width, height)
        camera_state.set_fps(fps)
        camera_state.set_format(fmt)
        camera_state.is_running = True

    return jsonify({
        "status": "camera started",
        "camera_index": camera_state.camera_index,
        "width": camera_state.width,
        "height": camera_state.height,
        "format": camera_state.format,
        "fps": camera_state.fps
    })

@app.route('/cam/stop', methods=['POST'])
def stop_camera():
    with camera_state.lock:
        camera_state.close_camera()
    return jsonify({"status": "camera stopped"})

@app.route('/cam/res', methods=['PUT'])
def set_resolution():
    data = request.get_json(force=True)
    width = data.get('width')
    height = data.get('height')
    if not width or not height:
        return jsonify({"error": "Width and height required"}), 400
    with camera_state.lock:
        camera_state.set_resolution(width, height)
    return jsonify({"status": "resolution set", "width": camera_state.width, "height": camera_state.height})

@app.route('/cam/form', methods=['PUT'])
def set_format():
    data = request.get_json(force=True)
    fmt = data.get('format', '').lower()
    if not fmt:
        return jsonify({"error": "Format is required"}), 400
    if not camera_state.set_format(fmt):
        return jsonify({"error": f"Format {fmt} not supported"}), 400
    return jsonify({"status": "format set", "format": camera_state.format})

# ========== Camera functional endpoints ==========

@app.route('/cam/capture', methods=['GET'])
def capture_image():
    params = request.args
    width = params.get('width', camera_state.width)
    height = params.get('height', camera_state.height)
    fmt = params.get('format', camera_state.format).lower()
    if fmt not in SUPPORTED_IMAGE_FORMATS:
        return jsonify({"error": f"Format {fmt} not supported for capture"}), 400
    with camera_state.lock:
        if not camera_state.is_running or camera_state.cap is None:
            return jsonify({"error": "Camera is not running"}), 400
        camera_state.set_resolution(width, height)
        frame = camera_state.get_frame()
    if frame is None:
        return jsonify({"error": "Could not capture image"}), 500
    # Encode
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if fmt in ['jpeg', 'jpg'] else []
    ext = '.jpg' if fmt in ['jpeg', 'jpg'] else '.png'
    ret, buf = cv2.imencode(ext, frame, encode_param)
    if not ret:
        return jsonify({"error": "Failed to encode image"}), 500
    return Response(buf.tobytes(), mimetype=f'image/{fmt}')

@app.route('/cam/stream', methods=['GET'])
def stream_video():
    params = request.args
    width = params.get('width', camera_state.width)
    height = params.get('height', camera_state.height)
    fmt = params.get('format', camera_state.format).lower()
    fps = int(params.get('fps', camera_state.fps))
    if fmt not in SUPPORTED_VIDEO_FORMATS:
        return jsonify({"error": f"Format {fmt} not supported for stream"}), 400

    def mjpeg_gen():
        while True:
            with camera_state.lock:
                if not camera_state.is_running or camera_state.cap is None:
                    break
                camera_state.set_resolution(width, height)
                frame = camera_state.get_frame()
            if frame is None:
                break
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / float(fps))

    def mp4_gen():
        boundary = "videoboundary"
        memfile = io.BytesIO()
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter('appsrc ! videoconvert ! x264enc tune=zerolatency bitrate=500 speed-preset=ultrafast ! mp4mux ! filesink location=/dev/stdout', fourcc, float(fps), (int(width), int(height)))
        start_time = time.time()
        try:
            while True:
                with camera_state.lock:
                    if not camera_state.is_running or camera_state.cap is None:
                        break
                    camera_state.set_resolution(width, height)
                    frame = camera_state.get_frame()
                if frame is None:
                    break
                out.write(frame)
                # Skipping actual MP4 HTTP streaming due to OpenCV limitations
                # For production, use GStreamer Python bindings or ffmpeg-python
                # Here, as a placeholder, just yield MJPEG
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                time.sleep(1.0 / float(fps))
                if time.time() - start_time > 60:
                    break
        finally:
            out.release()

    if fmt == "mjpeg":
        return Response(mjpeg_gen(), mimetype='multipart/x-mixed-replace; boundary=frame')
    elif fmt == "mp4":
        # For browser compatibility, fallback to MJPEG stream
        return Response(mp4_gen(), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return jsonify({"error": f"Format {fmt} not implemented"}), 400

@app.route('/cam/record', methods=['POST'])
def record_video():
    data = request.get_json(force=True)
    duration = float(data.get('duration', 10))
    if duration > 60:
        duration = 60
    width = data.get('width', camera_state.width)
    height = data.get('height', camera_state.height)
    fmt = data.get('format', camera_state.format).lower()
    fps = int(data.get('fps', camera_state.fps))
    if fmt not in SUPPORTED_VIDEO_FORMATS:
        return jsonify({"error": f"Format {fmt} not supported for recording"}), 400
    with camera_state.lock:
        if not camera_state.is_running or camera_state.cap is None:
            return jsonify({"error": "Camera is not running"}), 400
        camera_state.set_resolution(width, height)
        camera_state.set_fps(fps)
        fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'mp4' else 'MJPG'))
        ext = '.mp4' if fmt == 'mp4' else '.avi'
        tmpfile = f"/tmp/recording_{int(time.time())}{ext}" if platform.system() != "Windows" else f"recording_{int(time.time())}{ext}"
        out = cv2.VideoWriter(tmpfile, fourcc, float(fps), (int(width), int(height)))
        frames_captured = 0
        start_time = time.time()
        while time.time() - start_time < duration:
            frame = camera_state.get_frame()
            if frame is None:
                break
            out.write(frame)
            frames_captured += 1
            time.sleep(max(0, 1.0 / float(fps)))
        out.release()
    # Serve the file
    if not os.path.exists(tmpfile):
        return jsonify({"error": "Recording failed"}), 500
    def cleanup(filename):
        try:
            os.remove(filename)
        except Exception:
            pass
    return send_file(tmpfile, mimetype=f'video/{fmt if fmt!="mjpeg" else "avi"}', as_attachment=True, download_name=f"record{ext}"), 200, {'X-Delete-File': tmpfile}

# ========== Camera enumeration ==========

@app.route('/cam/list', methods=['GET'])
def list_cameras():
    indices = camera_state.enumerate_cameras()
    return jsonify({"available_cameras": indices})

# ========== Flask Startup ==========

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)