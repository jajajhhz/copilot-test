import os
import threading
import time
import io
import cv2
from flask import Flask, Response, request, jsonify, send_file, abort

# Environment Variables for Config
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DEFAULT_RESOLUTION = (
    int(os.environ.get("CAMERA_DEFAULT_WIDTH", "640")),
    int(os.environ.get("CAMERA_DEFAULT_HEIGHT", "480")),
)
DEFAULT_FRAME_RATE = int(os.environ.get("CAMERA_DEFAULT_FRAMERATE", "15"))
DEFAULT_FORMAT = os.environ.get("CAMERA_DEFAULT_FORMAT", "jpeg")
DEFAULT_CAMERA_ID = int(os.environ.get("CAMERA_DEFAULT_ID", "0"))

app = Flask(__name__)

class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.camera = None
        self.camera_id = DEFAULT_CAMERA_ID
        self.resolution = DEFAULT_RESOLUTION
        self.frame_rate = DEFAULT_FRAME_RATE
        self.format = DEFAULT_FORMAT
        self.running = False
        self.last_frame = None
        self.stream_thread = None
        self.stop_event = threading.Event()

    def _open_camera(self, camera_id=None, resolution=None, frame_rate=None, fmt=None):
        with self.lock:
            if self.camera is not None:
                self.camera.release()
            cam_id = camera_id if camera_id is not None else self.camera_id
            cap = cv2.VideoCapture(cam_id)
            if not cap.isOpened():
                raise Exception(f"Cannot open camera with id {cam_id}")

            # Set resolution
            width, height = resolution if resolution else self.resolution
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            # Set frame rate
            cap.set(cv2.CAP_PROP_FPS, frame_rate if frame_rate else self.frame_rate)
            self.camera = cap
            self.camera_id = cam_id
            self.resolution = (width, height)
            self.frame_rate = frame_rate if frame_rate else self.frame_rate
            self.format = fmt if fmt else self.format
            self.running = True
            self.stop_event.clear()
            return True

    def start(self, resolution=None, frame_rate=None, fmt=None, camera_id=None):
        try:
            return self._open_camera(camera_id, resolution, frame_rate, fmt)
        except Exception as e:
            return str(e)

    def stop(self):
        with self.lock:
            if self.camera is not None:
                self.camera.release()
                self.camera = None
            self.running = False
            self.stop_event.set()
            return True

    def switch(self, camera_id):
        with self.lock:
            if self.camera_id == camera_id:
                return True  # No switch needed
            return self._open_camera(camera_id, self.resolution, self.frame_rate, self.format)

    def get_frame(self, resolution=None, fmt=None):
        with self.lock:
            if not self.running or self.camera is None:
                raise Exception("Camera not started")
            ret, frame = self.camera.read()
            if not ret:
                raise Exception("Failed to capture frame")
            if resolution:
                frame = cv2.resize(frame, resolution)
            image_format = (fmt or self.format).lower()
            if image_format == "jpeg" or image_format == "jpg":
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
                ext = ".jpg"
            elif image_format == "png":
                encode_param = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
                ext = ".png"
            else:
                raise Exception(f"Unsupported format: {image_format}")
            ret, buf = cv2.imencode(ext, frame, encode_param)
            if not ret:
                raise Exception("Failed to encode frame")
            self.last_frame = buf.tobytes()
            return self.last_frame, f"image/{image_format}", ext

    def stream_generator(self):
        boundary = "--frame"
        while self.running and not self.stop_event.is_set():
            with self.lock:
                if self.camera is None:
                    time.sleep(0.1)
                    continue
                ret, frame = self.camera.read()
                if not ret:
                    continue
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ret:
                    continue
                jpg_bytes = buffer.tobytes()
            yield (
                b"%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % (boundary.encode(), len(jpg_bytes))
                + jpg_bytes
                + b"\r\n"
            )
            time.sleep(1.0 / self.frame_rate)
        yield b"--frame--\r\n"

camera_mgr = CameraManager()

@app.route("/camera/start", methods=["POST"])
def start_camera():
    data = request.json if request.is_json else request.form
    width = data.get("width") or request.args.get("width", type=int)
    height = data.get("height") or request.args.get("height", type=int)
    frame_rate = data.get("frame_rate") or request.args.get("frame_rate", type=int)
    fmt = data.get("format") or request.args.get("format")
    camera_id = data.get("camera_id") or request.args.get("camera_id", type=int)
    resolution = None
    if width and height:
        resolution = (int(width), int(height))
    result = camera_mgr.start(resolution=resolution, frame_rate=frame_rate, fmt=fmt, camera_id=camera_id)
    if result is True:
        return jsonify({"status": "success", "message": "Camera started successfully"}), 200
    else:
        return jsonify({"status": "error", "message": str(result)}), 400

@app.route("/camera/stop", methods=["POST"])
def stop_camera():
    try:
        camera_mgr.stop()
        return jsonify({"status": "success", "message": "Camera stopped successfully"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/camera/switch", methods=["POST"])
def switch_camera():
    data = request.json if request.is_json else request.form
    camera_id = data.get("camera_id") or request.args.get("camera_id", type=int)
    if camera_id is None:
        return jsonify({"status": "error", "message": "camera_id parameter is required"}), 400
    try:
        result = camera_mgr.switch(int(camera_id))
        if result is True:
            return jsonify({"status": "success", "message": f"Switched to camera {camera_id}"}), 200
        else:
            return jsonify({"status": "error", "message": str(result)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/camera/capture", methods=["GET"])
def capture_frame():
    width = request.args.get("width", type=int)
    height = request.args.get("height", type=int)
    fmt = request.args.get("format")
    resolution = (width, height) if width and height else None
    try:
        img_bytes, mime, ext = camera_mgr.get_frame(resolution=resolution, fmt=fmt)
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=mime,
            as_attachment=True,
            download_name=f"frame{ext}",
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/camera/stream", methods=["GET"])
def stream_video():
    try:
        if not camera_mgr.running:
            raise Exception("Camera not started")
        return Response(
            camera_mgr.stream_generator(),
            mimetype="multipart/x-mixed-replace; boundary=frame"
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)