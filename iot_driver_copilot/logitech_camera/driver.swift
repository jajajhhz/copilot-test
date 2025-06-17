import os
import cv2
import threading
import time
import tempfile
import shutil
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

CAMERA_RESOLUTION = (
    int(os.environ.get('CAMERA_RESOLUTION_WIDTH', 640)),
    int(os.environ.get('CAMERA_RESOLUTION_HEIGHT', 480))
)
CAMERA_FPS = int(os.environ.get('CAMERA_FPS', 20))
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))

def list_usb_cameras(max_tested=10):
    ids = []
    for i in range(max_tested):
        cap = cv2.VideoCapture(i)
        if cap is not None and cap.isOpened():
            ids.append(i)
            cap.release()
    return ids

class USBCameraInstance:
    def __init__(self, camera_id, resolution, fps):
        self.camera_id = camera_id
        self.resolution = resolution
        self.fps = fps
        self.cap = None
        self.lock = threading.RLock()
        self.streaming = False
        self.active = False

    def open(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.camera_id)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
                self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.active = True

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.active = False

    def read(self):
        with self.lock:
            if self.cap is not None and self.cap.isOpened():
                ret, frame = self.cap.read()
                if not ret:
                    raise Exception('Camera read failed')
                return frame
            else:
                raise Exception('Camera not opened')

    def is_active(self):
        return self.active

    def is_opened(self):
        return self.cap is not None and self.cap.isOpened()

    def __del__(self):
        self.close()

class USBCameraManager:
    def __init__(self, resolution, fps):
        self.resolution = resolution
        self.fps = fps
        self.instances = {}
        self.active_id = None
        self.global_lock = threading.RLock()
        self.refresh()

    def refresh(self):
        with self.global_lock:
            ids = list_usb_cameras()
            # Remove disconnected cameras
            for cam_id in list(self.instances.keys()):
                if cam_id not in ids:
                    self.instances[cam_id].close()
                    del self.instances[cam_id]
            # Add new cameras
            for cam_id in ids:
                if cam_id not in self.instances:
                    self.instances[cam_id] = USBCameraInstance(cam_id, self.resolution, self.fps)
            # Set default active if not set
            if self.active_id not in self.instances and ids:
                self.active_id = ids[0]

    def list_cameras(self):
        self.refresh()
        cameras = []
        for cam_id in sorted(self.instances.keys()):
            cameras.append({
                'camera_id': cam_id,
                'active': self.active_id == cam_id,
                'opened': self.instances[cam_id].is_opened()
            })
        return cameras

    def get_active(self):
        with self.global_lock:
            if self.active_id is None:
                self.refresh()
            if self.active_id is None or self.active_id not in self.instances:
                raise Exception('No camera available')
            return self.instances[self.active_id]

    def start(self):
        with self.global_lock:
            cam = self.get_active()
            cam.open()

    def stop(self):
        with self.global_lock:
            cam = self.get_active()
            cam.close()

    def select(self, camera_id):
        camera_id = int(camera_id)
        with self.global_lock:
            self.refresh()
            if camera_id not in self.instances:
                raise Exception('Camera id {} not found'.format(camera_id))
            # close current
            if self.active_id in self.instances:
                self.instances[self.active_id].close()
            self.active_id = camera_id
            self.instances[camera_id].open()

camera_manager = USBCameraManager(CAMERA_RESOLUTION, CAMERA_FPS)

@app.route('/cameras', methods=['GET'])
def cameras_list():
    try:
        cameras = camera_manager.list_cameras()
        return jsonify({'cameras': cameras})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/camera/start', methods=['POST'])
def camera_start():
    try:
        camera_manager.start()
        return jsonify({'status': 'started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/camera/stop', methods=['POST'])
def camera_stop():
    try:
        camera_manager.stop()
        return jsonify({'status': 'stopped'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/cameras/select', methods=['PUT'])
def camera_select():
    try:
        data = request.get_json(force=True)
        camera_id = data.get('camera_id')
        if camera_id is None:
            return jsonify({'error': 'camera_id parameter required'}), 400
        camera_manager.select(camera_id)
        return jsonify({'status': 'selected', 'camera_id': int(camera_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def mjpeg_generator(cam):
    try:
        while True:
            with cam.lock:
                if not cam.is_opened():
                    break
                ret, frame = cam.cap.read()
                if not ret:
                    continue
                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                image_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + image_bytes + b'\r\n')
            time.sleep(1.0 / cam.fps)
    except Exception:
        pass

@app.route('/camera/stream', methods=['GET'])
def camera_stream():
    try:
        cam = camera_manager.get_active()
        if not cam.is_opened():
            cam.open()
        return Response(mjpeg_generator(cam), mimetype='multipart/x-mixed-replace; boundary=frame')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/camera/capture', methods=['GET'])
def camera_capture():
    try:
        cam = camera_manager.get_active()
        if not cam.is_opened():
            cam.open()
        frame = cam.read()
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            return jsonify({'error': 'Failed to encode frame'}), 500
        image_bytes = buffer.tobytes()
        # Metadata
        meta = {
            'camera_id': cam.camera_id,
            'resolution': cam.resolution,
            'timestamp': time.time()
        }
        # Save image to temp file for sending
        tmpdir = tempfile.mkdtemp()
        imgpath = os.path.join(tmpdir, 'capture.jpg')
        with open(imgpath, 'wb') as f:
            f.write(image_bytes)
        response = send_file(imgpath, mimetype='image/jpeg')
        response.headers['X-Metadata'] = str(meta)
        # Cleanup temp file after request
        @response.call_on_close
        def cleanup():
            shutil.rmtree(tmpdir)
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/camera/record', methods=['POST'])
def camera_record():
    tmpdir = tempfile.mkdtemp()
    try:
        cam = camera_manager.get_active()
        if not cam.is_opened():
            cam.open()
        data = request.get_json(force=True)
        duration = int(data.get('duration', 5))  # seconds
        fmt = data.get('format', 'mp4').lower()
        if fmt not in ['mp4', 'avi']:
            fmt = 'mp4'
        ext = 'mp4' if fmt == 'mp4' else 'avi'
        filename = os.path.join(tmpdir, f'record.{ext}')
        fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'mp4' else 'XVID'))
        out = cv2.VideoWriter(filename, fourcc, cam.fps, cam.resolution)
        start_time = time.time()
        with cam.lock:
            while time.time() - start_time < duration:
                frame = cam.read()
                out.write(frame)
                time.sleep(1.0 / cam.fps)
        out.release()
        response = send_file(filename, as_attachment=True, mimetype='video/mp4' if fmt == 'mp4' else 'video/x-msvideo')
        # Cleanup temp file after request
        @response.call_on_close
        def cleanup():
            shutil.rmtree(tmpdir)
        return response
    except Exception as e:
        shutil.rmtree(tmpdir)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)