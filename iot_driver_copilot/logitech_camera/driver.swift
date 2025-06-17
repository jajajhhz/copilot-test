import os
import threading
import time
import cv2
import tempfile
from flask import Flask, Response, jsonify, request, send_file

# ========== Environment Variables ==========
HTTP_HOST = os.environ.get('SHIFU_HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('SHIFU_HTTP_PORT', '8080'))
DEFAULT_RESOLUTION = (
    int(os.environ.get('SHIFU_CAMERA_WIDTH', '640')),
    int(os.environ.get('SHIFU_CAMERA_HEIGHT', '480'))
)
DEFAULT_FPS = int(os.environ.get('SHIFU_CAMERA_FPS', '15'))

# ========== Camera Device Management ==========

class CameraInstance:
    def __init__(self, camera_id, resolution=DEFAULT_RESOLUTION, fps=DEFAULT_FPS):
        self.camera_id = camera_id
        self.resolution = resolution
        self.fps = fps
        self.cap = None
        self.active = False
        self.lock = threading.RLock()

    def open(self):
        with self.lock:
            if self.cap is not None and self.cap.isOpened():
                return True
            self.cap = cv2.VideoCapture(self.camera_id)
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                return False
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.active = True
            return True

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.active = False

    def read_frame(self):
        with self.lock:
            if self.cap is not None and self.cap.isOpened():
                ret, frame = self.cap.read()
                return ret, frame
            return False, None

    def is_opened(self):
        with self.lock:
            return self.cap is not None and self.cap.isOpened()

# ========== Camera Manager ==========

class CameraManager:
    def __init__(self):
        self.cameras = {}
        self.enumerate_cameras()
        self.active_camera_id = None
        self.active_camera = None
        self.manager_lock = threading.RLock()

    def enumerate_cameras(self):
        # Enumerate up to 10 camera indices, as cv2 doesn't provide camera lists
        found = {}
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found[i] = CameraInstance(i)
                cap.release()
        self.cameras = found
        if self.active_camera_id not in self.cameras:
            self.active_camera_id = next(iter(self.cameras), None)
            self.active_camera = self.cameras.get(self.active_camera_id, None)

    def get_camera_list(self):
        self.enumerate_cameras()
        return [{'camera_id': k, 'active': (k == self.active_camera_id)} for k in self.cameras]

    def select_camera(self, camera_id):
        with self.manager_lock:
            camera_id = int(camera_id)
            self.enumerate_cameras()
            if camera_id not in self.cameras:
                return False, f"Camera {camera_id} not found"
            if self.active_camera and self.active_camera.is_opened():
                self.active_camera.close()
            self.active_camera_id = camera_id
            self.active_camera = self.cameras[camera_id]
            return True, f"Camera {camera_id} selected"

    def start_camera(self, resolution=DEFAULT_RESOLUTION, fps=DEFAULT_FPS):
        with self.manager_lock:
            if self.active_camera is None:
                self.enumerate_cameras()
                if not self.cameras:
                    return False, "No camera found"
                self.active_camera_id = next(iter(self.cameras))
                self.active_camera = self.cameras[self.active_camera_id]
            self.active_camera.resolution = resolution
            self.active_camera.fps = fps
            if self.active_camera.open():
                return True, f"Camera {self.active_camera_id} started"
            else:
                return False, f"Failed to open camera {self.active_camera_id}"

    def stop_camera(self):
        with self.manager_lock:
            if self.active_camera and self.active_camera.is_opened():
                self.active_camera.close()
                return True, "Camera stopped"
            return False, "No camera is running"

    def get_active_camera(self):
        with self.manager_lock:
            return self.active_camera

# ========== Flask App ==========

app = Flask(__name__)
camera_manager = CameraManager()

@app.route('/cameras', methods=['GET'])
def list_cameras():
    cam_list = camera_manager.get_camera_list()
    return jsonify({'cameras': cam_list})

@app.route('/cameras/select', methods=['PUT'])
def select_camera():
    data = request.get_json(force=True, silent=True)
    if not data or 'camera_id' not in data:
        return jsonify({'error': 'camera_id is required'}), 400
    success, msg = camera_manager.select_camera(data['camera_id'])
    if success:
        return jsonify({'result': msg})
    else:
        return jsonify({'error': msg}), 404

@app.route('/camera/start', methods=['POST'])
def start_camera():
    resolution = (
        int(request.args.get('width', DEFAULT_RESOLUTION[0])),
        int(request.args.get('height', DEFAULT_RESOLUTION[1]))
    )
    fps = int(request.args.get('fps', DEFAULT_FPS))
    success, msg = camera_manager.start_camera(resolution, fps)
    if success:
        return jsonify({'result': msg})
    else:
        return jsonify({'error': msg}), 500

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    success, msg = camera_manager.stop_camera()
    if success:
        return jsonify({'result': msg})
    else:
        return jsonify({'error': msg}), 400

def gen_mjpeg(camera_instance):
    while True:
        with camera_instance.lock:
            if not camera_instance.is_opened():
                break
            ret, frame = camera_instance.read_frame()
        if not ret:
            time.sleep(0.1)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / camera_instance.fps)

@app.route('/camera/stream', methods=['GET'])
def stream_video():
    camera = camera_manager.get_active_camera()
    if camera is None or not camera.is_opened():
        return jsonify({'error': 'Camera is not started'}), 400
    return Response(gen_mjpeg(camera),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/capture', methods=['GET'])
def capture_frame():
    camera = camera_manager.get_active_camera()
    if camera is None or not camera.is_opened():
        return jsonify({'error': 'Camera is not started'}), 400
    with camera.lock:
        ret, frame = camera.read_frame()
        if not ret:
            return jsonify({'error': 'Failed to capture frame'}), 500
        encode_format = request.args.get('format', 'jpg')
        encode_ext = '.jpg' if encode_format.lower() not in ('png',) else '.png'
        ret, buf = cv2.imencode(encode_ext, frame)
        if not ret:
            return jsonify({'error': 'Failed to encode image'}), 500
        tmpfile = tempfile.NamedTemporaryFile(suffix=encode_ext, delete=False)
        try:
            tmpfile.write(buf.tobytes())
            tmpfile.flush()
            tmpfile.close()
            metadata = {
                'camera_id': camera.camera_id,
                'resolution': {'width': camera.resolution[0], 'height': camera.resolution[1]},
                'format': encode_format
            }
            return send_file(tmpfile.name, mimetype=f'image/{encode_format}', as_attachment=True,
                             download_name=f'capture_{camera.camera_id}{encode_ext}',
                             headers={'X-Image-Metadata': str(metadata)})
        finally:
            try:
                os.unlink(tmpfile.name)
            except Exception:
                pass

@app.route('/camera/record', methods=['POST'])
def record_video():
    data = request.get_json(force=True, silent=True)
    duration = int(data.get('duration', 5)) if data else 5
    codec = request.args.get('codec', 'mp4v')
    fmt = request.args.get('format', 'mp4')
    fps = int(request.args.get('fps', DEFAULT_FPS))

    camera = camera_manager.get_active_camera()
    if camera is None or not camera.is_opened():
        return jsonify({'error': 'Camera is not started'}), 400

    fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'mp4' else 'XVID'))
    ext = '.mp4' if fmt == 'mp4' else '.avi'
    tmpfile = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    output_path = tmpfile.name
    tmpfile.close()

    record_success = False
    try:
        with camera.lock:
            width, height = camera.resolution
            out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
            if not out.isOpened():
                raise Exception("Failed to open video writer")
            end_time = time.time() + duration
            while time.time() < end_time:
                ret, frame = camera.read_frame()
                if not ret:
                    continue
                out.write(frame)
                time.sleep(1.0 / fps)
            out.release()
            record_success = True
        if record_success:
            return send_file(output_path, mimetype=f'video/{fmt}', as_attachment=True,
                             download_name=f'record_{camera.camera_id}{ext}')
        else:
            return jsonify({'error': 'Failed to record video'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            os.unlink(output_path)
        except Exception:
            pass

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)