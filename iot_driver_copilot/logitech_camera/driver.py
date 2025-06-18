import os
import threading
import cv2
import time
from flask import Flask, Response, request, jsonify, send_file, make_response
import io

app = Flask(__name__)

# Get environment variables for configuration
CAMERA_DEFAULT_ID = int(os.environ.get("CAMERA_DEFAULT_ID", "0"))
CAMERA_DEFAULT_WIDTH = int(os.environ.get("CAMERA_DEFAULT_WIDTH", "640"))
CAMERA_DEFAULT_HEIGHT = int(os.environ.get("CAMERA_DEFAULT_HEIGHT", "480"))
CAMERA_DEFAULT_FPS = int(os.environ.get("CAMERA_DEFAULT_FPS", "30"))
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))

# Supported image formats (OpenCV supports JPEG, PNG, BMP)
SUPPORTED_FORMATS = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "bmp": ".bmp"
}

lock = threading.Lock()

class CameraManager:
    def __init__(self):
        self.cap = None
        self.active_camera_id = None
        self.resolution = (CAMERA_DEFAULT_WIDTH, CAMERA_DEFAULT_HEIGHT)
        self.frame_rate = CAMERA_DEFAULT_FPS
        self.format = "jpeg"
        self.running = False
        self.last_frame = None
        self.read_thread = None

    def start_camera(self, camera_id=None, width=None, height=None, frame_rate=None, fmt=None):
        with lock:
            if self.running:
                return {"status": "error", "message": "Camera already started."}
            cam_id = camera_id if camera_id is not None else CAMERA_DEFAULT_ID
            w = width if width is not None else CAMERA_DEFAULT_WIDTH
            h = height if height is not None else CAMERA_DEFAULT_HEIGHT
            fps = frame_rate if frame_rate is not None else CAMERA_DEFAULT_FPS
            fmt = (fmt or "jpeg").lower()
            if fmt not in SUPPORTED_FORMATS:
                return {"status": "error", "message": f"Unsupported format: {fmt}"}
            cap = cv2.VideoCapture(cam_id, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            if not cap.isOpened():
                return {"status": "error", "message": f"Cannot open camera {cam_id}"}
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
            self.cap = cap
            self.active_camera_id = cam_id
            self.resolution = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            self.frame_rate = int(cap.get(cv2.CAP_PROP_FPS)) or fps
            self.format = fmt
            self.running = True
            self.last_frame = None
            self.read_thread = threading.Thread(target=self._update, daemon=True)
            self.read_thread.start()
            return {
                "status": "ok",
                "message": f"Camera {cam_id} started",
                "resolution": self.resolution,
                "frame_rate": self.frame_rate,
                "format": self.format,
                "camera_id": self.active_camera_id
            }

    def _update(self):
        while self.running and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                self.last_frame = frame
            else:
                break
            time.sleep(1 / (self.frame_rate if self.frame_rate > 0 else 30))

    def stop_camera(self):
        with lock:
            if not self.running:
                return {"status": "error", "message": "Camera not started."}
            self.running = False
            if self.cap:
                self.cap.release()
                self.cap = None
            self.last_frame = None
            self.active_camera_id = None
            return {"status": "ok", "message": "Camera stopped."}

    def capture_frame(self, width=None, height=None, fmt=None):
        with lock:
            if not self.running or not self.cap or not self.cap.isOpened():
                return None, "Camera not started."
            frame = self.last_frame
            if frame is None:
                return None, "No frame available."
            if width and height:
                frame = cv2.resize(frame, (int(width), int(height)))
            format_ext = SUPPORTED_FORMATS.get((fmt or self.format).lower(), ".jpg")
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if format_ext in [".jpg", ".jpeg"] else []
            success, img_bytes = cv2.imencode(format_ext, frame, encode_param)
            if not success:
                return None, "Failed to encode image."
            return img_bytes.tobytes(), None

    def switch_camera(self, camera_id):
        with lock:
            # Stop current camera if running
            if self.running:
                self.stop_camera()
            # Start new camera
            return self.start_camera(camera_id=camera_id)

    def get_mjpeg_stream(self):
        while True:
            with lock:
                if not self.running or not self.cap or not self.cap.isOpened():
                    break
                frame = self.last_frame
                if frame is None:
                    continue
                ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ret:
                    continue
                frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(1 / (self.frame_rate if self.frame_rate > 0 else 30))

    def is_running(self):
        return self.running

    def get_active_camera_id(self):
        return self.active_camera_id

camera_manager = CameraManager()

@app.route('/camera/start', methods=['POST'])
def start_camera():
    params = request.json if request.is_json else request.form
    camera_id = params.get('camera_id', type=int) if params else request.args.get('camera_id', type=int)
    width = params.get('width', type=int) if params else request.args.get('width', type=int)
    height = params.get('height', type=int) if params else request.args.get('height', type=int)
    frame_rate = params.get('frame_rate', type=int) if params else request.args.get('frame_rate', type=int)
    fmt = params.get('format') if params else request.args.get('format')
    result = camera_manager.start_camera(
        camera_id=camera_id,
        width=width,
        height=height,
        frame_rate=frame_rate,
        fmt=fmt
    )
    code = 200 if result['status'] == 'ok' else 400
    return jsonify(result), code

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    result = camera_manager.stop_camera()
    code = 200 if result['status'] == 'ok' else 400
    return jsonify(result), code

@app.route('/camera/stream', methods=['GET'])
def stream_camera():
    if not camera_manager.is_running():
        return jsonify({"status": "error", "message": "Camera not started."}), 400
    return Response(
        camera_manager.get_mjpeg_stream(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/camera/capture', methods=['GET'])
def capture_frame():
    width = request.args.get('width', type=int)
    height = request.args.get('height', type=int)
    fmt = request.args.get('format', default="jpeg").lower()
    if fmt not in SUPPORTED_FORMATS:
        return jsonify({"status": "error", "message": f"Unsupported format: {fmt}"}), 400
    img_bytes, err = camera_manager.capture_frame(width, height, fmt)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    format_ext = SUPPORTED_FORMATS[fmt]
    mimetype = f"image/{'jpeg' if fmt in ['jpg', 'jpeg'] else fmt}"
    response = make_response(img_bytes)
    response.headers.set('Content-Type', mimetype)
    response.headers.set('Content-Disposition', f'attachment; filename="capture{format_ext}"')
    return response

@app.route('/camera/switch', methods=['POST'])
def switch_camera():
    data = request.json if request.is_json else request.form
    camera_id = data.get('camera_id', type=int) if data else request.args.get('camera_id', type=int)
    if camera_id is None:
        return jsonify({"status": "error", "message": "camera_id parameter required."}), 400
    result = camera_manager.switch_camera(camera_id)
    code = 200 if result['status'] == 'ok' else 400
    return jsonify(result), code

@app.route('/device/info', methods=['GET'])
def device_info():
    info = {
        "device_name": "Logitech Camera",
        "device_model": "G660",
        "manufacturer": "Logitech",
        "device_type": "Camera",
        "supported_formats": list(SUPPORTED_FORMATS.keys())
    }
    return jsonify(info), 200

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)