import os
import threading
import time
import cv2
import io
from flask import Flask, Response, request, jsonify, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configuration from environment variables
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

# Default camera params
DEFAULT_CAMERA_ID = int(os.environ.get("DEFAULT_CAMERA_ID", "0"))
DEFAULT_RESOLUTION = os.environ.get("DEFAULT_RESOLUTION", "640x480")
DEFAULT_FRAME_RATE = int(os.environ.get("DEFAULT_FRAME_RATE", "30"))
DEFAULT_FORMAT = os.environ.get("DEFAULT_FORMAT", "jpg")

def parse_resolution(res_str):
    try:
        width, height = map(int, res_str.lower().split('x'))
        return width, height
    except Exception:
        return 640, 480

class CameraManager:
    def __init__(self):
        self.cameras = {}
        self.locks = {}

    def start_camera(self, camera_id=DEFAULT_CAMERA_ID, resolution=None, frame_rate=None, format_=None):
        camera_id = int(camera_id)
        if camera_id in self.cameras:
            return {"status": "already started"}
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            return {"error": f"Cannot open camera {camera_id}"}
        # Set resolution
        if resolution:
            width, height = resolution
        else:
            width, height = parse_resolution(DEFAULT_RESOLUTION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        # Set frame rate if provided
        if frame_rate:
            cap.set(cv2.CAP_PROP_FPS, int(frame_rate))
        self.cameras[camera_id] = {
            "cap": cap,
            "resolution": (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))),
            "frame_rate": int(cap.get(cv2.CAP_PROP_FPS)),
            "format": format_ or DEFAULT_FORMAT
        }
        self.locks[camera_id] = threading.Lock()
        return {"status": "started", "camera_id": camera_id}

    def stop_camera(self, camera_id=DEFAULT_CAMERA_ID):
        camera_id = int(camera_id)
        if camera_id not in self.cameras:
            return {"error": f"Camera {camera_id} is not started"}
        with self.locks[camera_id]:
            self.cameras[camera_id]["cap"].release()
            del self.cameras[camera_id]
            del self.locks[camera_id]
        return {"status": "stopped", "camera_id": camera_id}

    def get_camera(self, camera_id=DEFAULT_CAMERA_ID):
        camera_id = int(camera_id)
        return self.cameras.get(camera_id, None)

    def capture_frame(self, camera_id=DEFAULT_CAMERA_ID, resolution=None, format_=None):
        camera_id = int(camera_id)
        if camera_id not in self.cameras:
            start_result = self.start_camera(camera_id, resolution, None, format_)
            if "error" in start_result:
                return None, start_result["error"], None
        cam_info = self.cameras[camera_id]
        cap = cam_info["cap"]
        fmt = format_ or cam_info.get("format", DEFAULT_FORMAT)
        if resolution:
            width, height = resolution
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        with self.locks[camera_id]:
            ret, frame = cap.read()
        if not ret or frame is None:
            return None, f"Failed to capture frame from camera {camera_id}", None
        # Encode frame
        encode_param = []
        file_ext = fmt.lower()
        if file_ext == 'jpg' or file_ext == 'jpeg':
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            file_ext = 'jpg'
        elif file_ext == 'png':
            encode_param = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
            file_ext = 'png'
        else:
            # Default to jpeg
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            file_ext = 'jpg'
        ret, buf = cv2.imencode(f'.{file_ext}', frame, encode_param)
        if not ret:
            return None, "Encoding frame failed", None
        return buf.tobytes(), None, file_ext

    def generate_mjpeg(self, camera_id=DEFAULT_CAMERA_ID):
        camera_id = int(camera_id)
        if camera_id not in self.cameras:
            start_result = self.start_camera(camera_id)
            if "error" in start_result:
                yield f"--frame\r\nContent-Type: text/plain\r\n\r\n{start_result['error']}\r\n"
                return
        cam_info = self.cameras[camera_id]
        cap = cam_info["cap"]
        width, height = cam_info.get("resolution", (640,480))
        fmt = cam_info.get("format", DEFAULT_FORMAT)
        while True:
            with self.locks[camera_id]:
                ret, frame = cap.read()
            if not ret or frame is None:
                break
            ret, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ret:
                continue
            frame_bytes = buf.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            # Try to control FPS
            time.sleep(1.0 / (cam_info.get("frame_rate", DEFAULT_FRAME_RATE) or 30))

camera_manager = CameraManager()

@app.route('/camera/start', methods=['POST'])
def start_camera():
    params = request.get_json(silent=True) or {}
    camera_id = params.get("camera_id", DEFAULT_CAMERA_ID)
    res = params.get("resolution")
    if res:
        if isinstance(res, list) and len(res) == 2:
            resolution = (int(res[0]), int(res[1]))
        elif isinstance(res, str):
            resolution = parse_resolution(res)
        else:
            resolution = parse_resolution(DEFAULT_RESOLUTION)
    else:
        resolution = parse_resolution(DEFAULT_RESOLUTION)
    frame_rate = params.get("frame_rate", DEFAULT_FRAME_RATE)
    format_ = params.get("format", DEFAULT_FORMAT)
    result = camera_manager.start_camera(camera_id, resolution, frame_rate, format_)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    params = request.get_json(silent=True) or {}
    camera_id = params.get("camera_id", DEFAULT_CAMERA_ID)
    result = camera_manager.stop_camera(camera_id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/camera/capture', methods=['GET'])
def capture_frame():
    camera_id = request.args.get("camera_id", DEFAULT_CAMERA_ID)
    res = request.args.get("resolution")
    fmt = request.args.get("format", DEFAULT_FORMAT)
    if res:
        resolution = parse_resolution(res)
    else:
        resolution = parse_resolution(DEFAULT_RESOLUTION)
    frame_bytes, error, file_ext = camera_manager.capture_frame(camera_id, resolution, fmt)
    if error:
        return jsonify({"error": error}), 400
    filename = secure_filename(f"camera_{camera_id}_{int(time.time())}.{file_ext}")
    return send_file(
        io.BytesIO(frame_bytes),
        mimetype=f'image/{file_ext}',
        as_attachment=True,
        download_name=filename
    )

@app.route('/camera/stream', methods=['GET'])
def stream_camera():
    camera_id = request.args.get("camera_id", DEFAULT_CAMERA_ID)
    def generate():
        yield from camera_manager.generate_mjpeg(camera_id)
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/status', methods=['GET'])
def camera_status():
    status = {
        "cameras": [
            {
                "camera_id": cam_id,
                "resolution": cam_info["resolution"],
                "frame_rate": cam_info["frame_rate"],
                "format": cam_info["format"],
            } for cam_id, cam_info in camera_manager.cameras.items()
        ]
    }
    return jsonify(status)

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)