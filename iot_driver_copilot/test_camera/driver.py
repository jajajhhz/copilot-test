```python
import os
import cv2
import io
import json
import threading
import time
from flask import Flask, Response, request, jsonify, send_file, abort

app = Flask(__name__)

# Configuration from environment variables
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
CAMERA_DEFAULT_ID = int(os.environ.get("CAMERA_ID", "0"))
CAMERA_DEFAULT_RES = os.environ.get("CAMERA_RESOLUTION", "640x480")
CAMERA_DEFAULT_FRAME_RATE = int(os.environ.get("CAMERA_FRAME_RATE", "30"))

# Camera State Management
class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cameras = {}  # id: CameraInstance

    def start(self, cam_id=0, resolution="640x480", frame_rate=30):
        cam_id = int(cam_id)
        with self.lock:
            if cam_id in self.cameras:
                cam = self.cameras[cam_id]
                if cam.is_active():
                    return True, "Camera already started"
                else:
                    cam.release()
            width, height = (int(x) for x in resolution.split("x"))
            cam = CameraInstance(cam_id, width, height, frame_rate)
            if cam.is_active():
                self.cameras[cam_id] = cam
                return True, "Camera started"
            else:
                cam.release()
                return False, "Failed to start camera"

    def stop(self, cam_id=0):
        cam_id = int(cam_id)
        with self.lock:
            cam = self.cameras.get(cam_id)
            if cam and cam.is_active():
                cam.release()
                del self.cameras[cam_id]
                return True, "Camera stopped"
            else:
                return False, "Camera not active"

    def get(self, cam_id=0):
        cam_id = int(cam_id)
        with self.lock:
            cam = self.cameras.get(cam_id)
            if cam and cam.is_active():
                return cam
            else:
                return None

    def status(self):
        with self.lock:
            return {cam_id: cam.status() for cam_id, cam in self.cameras.items()}

class CameraInstance:
    def __init__(self, cam_id, width, height, frame_rate):
        self.cam_id = cam_id
        self.width = width
        self.height = height
        self.frame_rate = frame_rate
        self.cap = cv2.VideoCapture(cam_id)
        self.set_props(width, height, frame_rate)
        self.last_frame = None
        self.active = self.cap.isOpened()

    def set_props(self, width, height, frame_rate):
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, frame_rate)

    def is_active(self):
        return self.cap.isOpened()

    def release(self):
        if self.cap.isOpened():
            self.cap.release()

    def read(self):
        if not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        if ret:
            self.last_frame = frame
            return frame
        return None

    def status(self):
        return {
            "id": self.cam_id,
            "active": self.is_active(),
            "resolution": f"{self.width}x{self.height}",
            "frame_rate": self.frame_rate
        }

camera_manager = CameraManager()

# Device info for /camera/info
DEVICE_INFO = {
    "device_name": "Test Camera",
    "device_model": "Test Camera USB",
    "manufacturer": "Unknown",
    "device_type": "Camera"
}

# API Endpoints

@app.route("/camera/info", methods=["GET"])
def camera_info():
    return jsonify({
        "device_info": DEVICE_INFO,
        "active_cameras": camera_manager.status()
    })

@app.route("/camera/start", methods=["POST"])
def camera_start():
    cam_id = request.args.get("id", CAMERA_DEFAULT_ID)
    resolution = request.args.get("resolution", CAMERA_DEFAULT_RES)
    frame_rate = int(request.args.get("frame_rate", CAMERA_DEFAULT_FRAME_RATE))
    ok, msg = camera_manager.start(cam_id, resolution, frame_rate)
    if ok:
        return jsonify({"message": msg, "id": int(cam_id)}), 200
    else:
        return jsonify({"error": msg}), 500

@app.route("/camera/stop", methods=["POST"])
def camera_stop():
    cam_id = request.args.get("id", CAMERA_DEFAULT_ID)
    ok, msg = camera_manager.stop(cam_id)
    if ok:
        return jsonify({"message": msg, "id": int(cam_id)}), 200
    else:
        return jsonify({"error": msg}), 404

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    cam_id = request.args.get("id", CAMERA_DEFAULT_ID)
    cam = camera_manager.get(cam_id)
    if not cam:
        return jsonify({"error": "Camera not started"}), 404

    def mjpeg_stream(cam):
        while True:
            frame = cam.read()
            if frame is None:
                break
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(1.0 / cam.frame_rate)

    return Response(mjpeg_stream(cam),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/capture", methods=["GET"])
def camera_capture():
    cam_id = request.args.get("id", CAMERA_DEFAULT_ID)
    cam = camera_manager.get(cam_id)
    if not cam:
        return jsonify({"error": "Camera not started"}), 404
    frame = cam.read()
    if frame is None:
        return jsonify({"error": "Failed to capture image"}), 500
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return jsonify({"error": "JPEG encoding failed"}), 500
    return Response(jpeg.tobytes(),
                    mimetype='image/jpeg',
                    headers={"Content-Disposition": "attachment; filename=capture.jpg"})

@app.route("/camera/snapshot", methods=["POST"])
def camera_snapshot():
    cam_id = request.args.get("id", CAMERA_DEFAULT_ID)
    cam = camera_manager.get(cam_id)
    if not cam:
        return jsonify({"error": "Camera not started"}), 404
    frame = cam.read()
    if frame is None:
        return jsonify({"error": "Failed to capture image"}), 500
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return jsonify({"error": "JPEG encoding failed"}), 500
    b64 = base64_encode(jpeg.tobytes())
    return jsonify({
        "id": int(cam_id),
        "format": "jpeg",
        "image_data_base64": b64,
        "size": len(jpeg.tobytes())
    })

def base64_encode(data):
    import base64
    return base64.b64encode(data).decode("utf-8")

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)
```