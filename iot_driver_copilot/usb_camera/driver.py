import os
import yaml
import threading
import time

from flask import Flask, Response, jsonify, request, abort
import cv2
import io

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ==== CONFIGURATION ====

EDGEDEVICE_NAME = os.environ.get('EDGEDEVICE_NAME')
EDGEDEVICE_NAMESPACE = os.environ.get('EDGEDEVICE_NAMESPACE')
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', '8080'))
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))
INSTRUCTIONS_CONFIG_PATH = '/etc/edgedevice/config/instructions'

if EDGEDEVICE_NAME is None or EDGEDEVICE_NAMESPACE is None:
    raise RuntimeError("Both EDGEDEVICE_NAME and EDGEDEVICE_NAMESPACE environment variables must be set.")

# ==== LOAD INSTRUCTIONS ====

def load_api_instructions(config_path):
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

api_instructions = load_api_instructions(INSTRUCTIONS_CONFIG_PATH)

# ==== K8S CLIENT ====

def k8s_initialize():
    try:
        config.load_incluster_config()
    except Exception as e:
        raise RuntimeError(f"Kubernetes in-cluster config failed: {e}")

k8s_initialize()
custom_api = client.CustomObjectsApi()

EDGEDEVICE_GROUP = "shifu.edgenesis.io"
EDGEDEVICE_VERSION = "v1alpha1"
EDGEDEVICE_PLURAL = "edgedevices"

def update_edge_device_phase(phase):
    body = {
        "status": {
            "edgeDevicePhase": phase
        }
    }
    for _ in range(3):
        try:
            custom_api.patch_namespaced_custom_object_status(
                group=EDGEDEVICE_GROUP,
                version=EDGEDEVICE_VERSION,
                namespace=EDGEDEVICE_NAMESPACE,
                plural=EDGEDEVICE_PLURAL,
                name=EDGEDEVICE_NAME,
                body=body
            )
            return
        except ApiException as e:
            time.sleep(1)
    # Don't crash on failure, just log
    print(f"Failed to update EdgeDevice phase to {phase}")

def get_device_address():
    try:
        obj = custom_api.get_namespaced_custom_object(
            group=EDGEDEVICE_GROUP,
            version=EDGEDEVICE_VERSION,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=EDGEDEVICE_PLURAL,
            name=EDGEDEVICE_NAME
        )
        return obj.get('spec', {}).get('address', None)
    except Exception as e:
        print(f"Could not get device address from EdgeDevice CRD: {e}")
        return None

# ==== CAMERA LOGIC ====

class CameraStreamer:
    def __init__(self, camera_index):
        self.camera_index = camera_index
        self.cap = None
        self.lock = threading.Lock()
        self.streaming = False
        self.latest_frame = None
        self.frame_thread = None
        self.stop_request = threading.Event()

    def open(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.camera_index)
                if not self.cap.isOpened():
                    self.cap = None
                    return False
            return True

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None

    def start_stream(self):
        with self.lock:
            if self.streaming:
                return True
            if not self.open():
                return False
            self.streaming = True
            self.stop_request.clear()
            self.frame_thread = threading.Thread(target=self._grab_frames, daemon=True)
            self.frame_thread.start()
            return True

    def stop_stream(self):
        with self.lock:
            self.streaming = False
            self.stop_request.set()
        if self.frame_thread:
            self.frame_thread.join(timeout=2)
        self.close()

    def _grab_frames(self):
        while not self.stop_request.is_set():
            ret, frame = False, None
            with self.lock:
                if self.cap:
                    ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.latest_frame = frame
            time.sleep(0.03)  # ~30 FPS

    def get_latest_jpeg(self):
        with self.lock:
            if self.latest_frame is None:
                return None
            ret, jpeg = cv2.imencode('.jpg', self.latest_frame)
            if not ret:
                return None
            return jpeg.tobytes()

    def mjpeg_generator(self):
        while True:
            if not self.streaming:
                break
            frame = self.get_latest_jpeg()
            if frame is not None:
                yield (
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                )
            else:
                time.sleep(0.05)

    def capture_image(self):
        with self.lock:
            if not self.open():
                return None
            ret, frame = self.cap.read()
            if not ret:
                return None
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                return None
            return jpeg.tobytes()

# ==== DEVICE STATUS THREAD ====

def device_status_loop(camera_streamer):
    last_status = None
    while True:
        try:
            if camera_streamer.open():
                cur_status = "Running" if camera_streamer.cap and camera_streamer.cap.isOpened() else "Pending"
            else:
                cur_status = "Failed"
        except Exception:
            cur_status = "Unknown"
        if cur_status != last_status:
            update_edge_device_phase(cur_status)
            last_status = cur_status
        time.sleep(5)

# ==== FLASK APP ====

app = Flask(__name__)
camera = CameraStreamer(CAMERA_INDEX)
device_address = get_device_address()

# --- HTTP ENDPOINTS ---

@app.route("/stream/start", methods=['POST'])
def start_stream():
    settings = api_instructions.get('stream_start', {}).get('protocolPropertyList', {})
    if camera.start_stream():
        update_edge_device_phase("Running")
        return jsonify({"status": "success", "message": "Stream started."}), 200
    else:
        update_edge_device_phase("Failed")
        return jsonify({"status": "failure", "message": "Failed to start camera stream."}), 500

@app.route("/stream/stop", methods=['POST'])
def stop_stream():
    settings = api_instructions.get('stream_stop', {}).get('protocolPropertyList', {})
    camera.stop_stream()
    update_edge_device_phase("Pending")
    return jsonify({"status": "success", "message": "Stream stopped."})

@app.route("/stream", methods=['GET'])
def stream():
    settings = api_instructions.get('stream', {}).get('protocolPropertyList', {})
    if not camera.streaming:
        abort(503, "Stream not started. Please POST to /stream/start first.")
    return Response(camera.mjpeg_generator(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/image/capture", methods=['POST'])
def capture_image():
    settings = api_instructions.get('image_capture', {}).get('protocolPropertyList', {})
    img_bytes = camera.capture_image()
    if img_bytes is None:
        update_edge_device_phase("Failed")
        return jsonify({"status": "failure", "message": "Failed to capture image."}), 500
    update_edge_device_phase("Running")
    return Response(img_bytes, mimetype='image/jpeg')

@app.route("/healthz", methods=['GET'])
def healthz():
    return "ok"

# ==== MAIN ====

if __name__ == "__main__":
    status_thread = threading.Thread(target=device_status_loop, args=(camera,), daemon=True)
    status_thread.start()
    update_edge_device_phase("Pending")
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)