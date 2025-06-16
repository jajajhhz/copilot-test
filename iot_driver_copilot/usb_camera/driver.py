import os
import threading
import time
import yaml
import cv2
from flask import Flask, Response, send_file, jsonify, request
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

# ========== Environment Variables ==========
EDGEDEVICE_NAME = os.environ["EDGEDEVICE_NAME"]
EDGEDEVICE_NAMESPACE = os.environ["EDGEDEVICE_NAMESPACE"]
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
INSTRUCTIONS_PATH = "/etc/edgedevice/config/instructions"
EDGEDEVICE_CRD_GROUP = "shifu.edgenesis.io"
EDGEDEVICE_CRD_VERSION = "v1alpha1"
EDGEDEVICE_CRD_PLURAL = "edgedevices"
EDGEDEVICE_CRD_KIND = "EdgeDevice"

# ========== ConfigMap Parsing ==========
def load_instruction_config():
    config = {}
    if os.path.exists(INSTRUCTIONS_PATH):
        with open(INSTRUCTIONS_PATH, "r") as f:
            config = yaml.safe_load(f) or {}
    return config

instruction_config = load_instruction_config()

# ========== EdgeDevice CRD Client ==========
def get_k8s_api():
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return client.CustomObjectsApi()

def get_edgedevice(api):
    try:
        return api.get_namespaced_custom_object(
            EDGEDEVICE_CRD_GROUP,
            EDGEDEVICE_CRD_VERSION,
            EDGEDEVICE_NAMESPACE,
            EDGEDEVICE_CRD_PLURAL,
            EDGEDEVICE_NAME
        )
    except ApiException:
        return None

def update_edgedevice_status(api, phase):
    for i in range(3):
        try:
            body = {
                "status": {
                    "edgeDevicePhase": phase
                }
            }
            api.patch_namespaced_custom_object_status(
                EDGEDEVICE_CRD_GROUP,
                EDGEDEVICE_CRD_VERSION,
                EDGEDEVICE_NAMESPACE,
                EDGEDEVICE_CRD_PLURAL,
                EDGEDEVICE_NAME,
                body
            )
            return
        except ApiException:
            time.sleep(1)

def get_edgedevice_address(api):
    ed = get_edgedevice(api)
    if ed and 'spec' in ed and 'address' in ed['spec']:
        return ed['spec']['address']
    return None

# ========== Camera Handler ==========
class USBCamera:
    def __init__(self, camera_index):
        self.camera_index = camera_index
        self.video_capture = None
        self.streaming = False
        self.lock = threading.Lock()

    def connect(self):
        with self.lock:
            if self.video_capture is not None:
                self.video_capture.release()
            self.video_capture = cv2.VideoCapture(self.camera_index)
            if not self.video_capture.isOpened():
                self.video_capture = None
                return False
            return True

    def disconnect(self):
        with self.lock:
            if self.video_capture is not None:
                self.video_capture.release()
                self.video_capture = None

    def is_connected(self):
        with self.lock:
            return self.video_capture is not None and self.video_capture.isOpened()

    def capture_image(self):
        with self.lock:
            if not self.is_connected():
                if not self.connect():
                    return None
            ret, frame = self.video_capture.read()
            if not ret:
                return None
            _, jpeg = cv2.imencode('.jpg', frame)
            return jpeg.tobytes()

    def frames(self):
        while self.streaming:
            with self.lock:
                if not self.is_connected():
                    if not self.connect():
                        yield None
                        continue
                ret, frame = self.video_capture.read()
                if not ret:
                    yield None
                    continue
                _, jpeg = cv2.imencode('.jpg', frame)
                frame_bytes = jpeg.tobytes()
            yield frame_bytes

    def start_stream(self):
        with self.lock:
            if not self.is_connected():
                if not self.connect():
                    return False
            self.streaming = True
        return True

    def stop_stream(self):
        with self.lock:
            self.streaming = False
        return True

# ========== Device Phase Monitor ==========
class DevicePhaseMonitor(threading.Thread):
    def __init__(self, camera: USBCamera, k8s_api):
        super().__init__(daemon=True)
        self.camera = camera
        self.k8s_api = k8s_api
        self.prev_phase = None

    def run(self):
        while True:
            try:
                if self.camera.is_connected():
                    phase = "Running" if self.camera.streaming else "Pending"
                else:
                    phase = "Failed"
            except Exception:
                phase = "Unknown"
            if phase != self.prev_phase:
                update_edgedevice_status(self.k8s_api, phase)
                self.prev_phase = phase
            time.sleep(3)

# ========== Flask App ==========
app = Flask(__name__)
camera = USBCamera(CAMERA_INDEX)
k8s_api = get_k8s_api()
device_phase_monitor = DevicePhaseMonitor(camera, k8s_api)
device_phase_monitor.start()

# ========== API Endpoints ==========

@app.route("/stream/start", methods=["POST"])
def start_stream():
    settings = instruction_config.get("stream/start", {}).get("protocolPropertyList", {})
    res = camera.start_stream()
    if res:
        return jsonify({"status": "streaming started"}), 200
    else:
        update_edgedevice_status(k8s_api, "Failed")
        return jsonify({"error": "Failed to start stream"}), 500

@app.route("/stream/stop", methods=["POST"])
def stop_stream():
    settings = instruction_config.get("stream/stop", {}).get("protocolPropertyList", {})
    res = camera.stop_stream()
    camera.disconnect()
    if res:
        return jsonify({"status": "stream stopped"}), 200
    else:
        return jsonify({"error": "Failed to stop stream"}), 500

@app.route("/stream", methods=["GET"])
def stream():
    settings = instruction_config.get("stream", {}).get("protocolPropertyList", {})
    if not camera.streaming:
        return jsonify({"error": "Stream not started"}), 400

    def generate():
        for frame in camera.frames():
            if frame is None or not camera.streaming:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/image/capture", methods=["POST"])
def capture_image():
    settings = instruction_config.get("image/capture", {}).get("protocolPropertyList", {})
    image_bytes = camera.capture_image()
    if image_bytes is None:
        update_edgedevice_status(k8s_api, "Failed")
        return jsonify({"error": "Failed to capture image"}), 500
    return Response(image_bytes, mimetype='image/jpeg')

@app.route("/healthz", methods=["GET"])
def healthz():
    return "OK", 200

# ========== Main ==========
if __name__ == "__main__":
    update_edgedevice_status(k8s_api, "Pending")
    try:
        if camera.connect():
            update_edgedevice_status(k8s_api, "Running")
        else:
            update_edgedevice_status(k8s_api, "Failed")
    except Exception:
        update_edgedevice_status(k8s_api, "Unknown")
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)