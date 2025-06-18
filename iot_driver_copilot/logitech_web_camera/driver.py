import os
import threading
import cv2
import time
import io
from flask import Flask, Response, request, jsonify, send_file
from werkzeug.exceptions import BadRequest

app = Flask(__name__)

# Environment variables for server configuration
HTTP_HOST = os.getenv('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.getenv('HTTP_PORT', '8080'))

# Thread-safe camera manager
class CameraManager:
    def __init__(self):
        self.cameras = {}  # camera_id: { 'cap': cv2.VideoCapture, 'lock': threading.Lock(), 'params': { ... } }
        self.locks = {}    # camera_id: threading.Lock

    def start_camera(self, camera_id=0, width=640, height=480, fps=None, fmt=None):
        camera_id = int(camera_id)
        if camera_id in self.cameras:
            return {'status': 'already_started'}

        lock = threading.Lock()
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            return {'error': f'Failed to open camera {camera_id}.'}

        # Set resolution
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        # Set FPS if provided
        if fps:
            cap.set(cv2.CAP_PROP_FPS, float(fps))

        # Set format if provided (OpenCV supports some fourcc formats)
        if fmt:
            try:
                fourcc = cv2.VideoWriter_fourcc(*fmt)
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            except Exception:
                pass  # Ignore if format is invalid

        self.cameras[camera_id] = {'cap': cap, 'lock': lock, 'params': {
            'width': width, 'height': height, 'fps': fps, 'format': fmt
        }}
        self.locks[camera_id] = lock
        return {'status': 'started', 'camera_id': camera_id, 'resolution': [width, height], 'fps': fps, 'format': fmt}

    def stop_camera(self, camera_id=0):
        camera_id = int(camera_id)
        if camera_id not in self.cameras:
            return {'error': f'Camera {camera_id} is not active.'}
        with self.locks[camera_id]:
            cap = self.cameras[camera_id]['cap']
            cap.release()
            del self.cameras[camera_id]
            del self.locks[camera_id]
        return {'status': 'stopped', 'camera_id': camera_id}

    def get_camera(self, camera_id=0):
        camera_id = int(camera_id)
        return self.cameras.get(camera_id)

    def is_active(self, camera_id=0):
        camera_id = int(camera_id)
        return camera_id in self.cameras

    def get_params(self, camera_id=0):
        camera_id = int(camera_id)
        return self.cameras[camera_id]['params'] if camera_id in self.cameras else {}

camera_manager = CameraManager()

# Capture frame endpoint
@app.route('/cameras/capture', methods=['GET'])
def capture_frame():
    camera_id = int(request.args.get('camera_id', 0))

    if not camera_manager.is_active(camera_id):
        return jsonify({'error': f'Camera {camera_id} is not active. Please start the camera first.'}), 400

    camera = camera_manager.get_camera(camera_id)
    lock = camera['lock']
    cap = camera['cap']

    with lock:
        ret, frame = cap.read()
        if not ret or frame is None:
            return jsonify({'error': f'Failed to capture frame from camera {camera_id}.'}), 500
        # Encode as JPEG
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            return jsonify({'error': 'Failed to encode image.'}), 500
        img_bytes = buffer.tobytes()

    return send_file(
        io.BytesIO(img_bytes),
        mimetype='image/jpeg',
        as_attachment=True,
        download_name=f'camera_{camera_id}_frame.jpg'
    )

# Stream video endpoint
def generate_stream(camera_id):
    camera = camera_manager.get_camera(camera_id)
    if camera is None:
        return
    cap = camera['cap']
    lock = camera['lock']

    while True:
        with lock:
            ret, frame = cap.read()
            if not ret or frame is None:
                # End stream if error
                break
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                break
            img_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
        time.sleep(0.04)  # ~25 FPS default

@app.route('/cameras/stream', methods=['GET'])
def stream_camera():
    camera_id = int(request.args.get('camera_id', 0))

    if not camera_manager.is_active(camera_id):
        return jsonify({'error': f'Camera {camera_id} is not active. Please start the camera first.'}), 400

    return Response(
        generate_stream(camera_id),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

# Start camera endpoint
@app.route('/cameras/start', methods=['POST'])
def start_camera():
    if request.is_json:
        data = request.get_json()
    else:
        data = {}

    camera_id = int(data.get('camera_id', 0))
    width = int(data.get('width', 640))
    height = int(data.get('height', 480))
    fps = data.get('fps', None)
    fmt = data.get('format', None)

    if fps is not None:
        try:
            fps = float(fps)
        except Exception:
            return jsonify({'error': 'Invalid fps value'}), 400

    result = camera_manager.start_camera(camera_id, width, height, fps, fmt)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)

# Stop camera endpoint
@app.route('/cameras/stop', methods=['POST'])
def stop_camera():
    if request.is_json:
        data = request.get_json()
    else:
        data = {}
    camera_id = int(data.get('camera_id', 0))
    result = camera_manager.stop_camera(camera_id)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)