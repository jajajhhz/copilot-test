import os
import io
import time
import threading
from flask import Flask, Response, request, jsonify, send_file
import cv2
import numpy as np

app = Flask(__name__)

# Environment Variables
SERVER_HOST = os.environ.get('DEVICE_SHIFU_HTTP_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('DEVICE_SHIFU_HTTP_PORT', '8080'))
DEFAULT_RESOLUTION = os.environ.get('DEVICE_SHIFU_CAM_DEFAULT_RES', '640x480')
DEFAULT_FORMAT = os.environ.get('DEVICE_SHIFU_CAM_DEFAULT_FORMAT', 'MJPEG')
SUPPORTED_FORMATS = ['JPEG', 'PNG', 'MP4', 'MJPEG']

def parse_resolution(res_str):
    try:
        w, h = map(int, res_str.lower().split('x'))
        return w, h
    except:
        return 640, 480

class CameraManager:
    def __init__(self):
        self.cams = self.enumerate_cameras()
        self.cam_id = int(os.environ.get('DEVICE_SHIFU_CAM_ID', '0'))
        if self.cam_id not in self.cams:
            self.cam_id = 0 if 0 in self.cams else (self.cams[0] if self.cams else 0)
        self.cap = None
        self.lock = threading.Lock()
        self.running = False
        self.format = DEFAULT_FORMAT.upper() if DEFAULT_FORMAT.upper() in SUPPORTED_FORMATS else 'MJPEG'
        self.width, self.height = parse_resolution(DEFAULT_RESOLUTION)
        self.recording = False
        self.record_thread = None

    def enumerate_cameras(self, max_tested=10):
        cams = []
        for i in range(max_tested):
            cap = cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                cams.append(i)
                cap.release()
        return cams

    def switch_camera(self, cam_id):
        with self.lock:
            if cam_id in self.cams:
                if self.cap is not None:
                    self.cap.release()
                self.cam_id = cam_id
                self.cap = None
                self.running = False
                return True
            return False

    def start(self, width=None, height=None, fmt=None):
        with self.lock:
            if self.cap is None:
                self.cap = cv2.VideoCapture(self.cam_id)
            if width and height:
                self.width, self.height = width, height
            if fmt and fmt.upper() in SUPPORTED_FORMATS:
                self.format = fmt.upper()
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.running = True
            return self.cap.isOpened()

    def stop(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.running = False

    def get_frame(self):
        with self.lock:
            if self.cap is None:
                self.start()
            if self.cap is None or not self.cap.isOpened():
                return None
            ret, frame = self.cap.read()
            if not ret:
                return None
            return frame

    def capture(self, fmt=None, width=None, height=None):
        frame = self.get_frame()
        if frame is None:
            return None, None
        if width and height:
            frame = cv2.resize(frame, (width, height))
        fmt = fmt.upper() if fmt else self.format
        if fmt == 'PNG':
            ret, img = cv2.imencode('.png', frame)
            mime = 'image/png'
        else:
            ret, img = cv2.imencode('.jpg', frame)
            mime = 'image/jpeg'
        if not ret:
            return None, None
        return img.tobytes(), mime

    def gen_stream(self, width=None, height=None, fmt=None):
        fmt = (fmt or self.format).upper()
        while True:
            frame = self.get_frame()
            if frame is None:
                break
            if width and height:
                frame = cv2.resize(frame, (width, height))
            if fmt == 'JPEG' or fmt == 'MJPEG':
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                frame_bytes = jpeg.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            else:
                # Default to JPEG/MJPEG for browser streaming
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                frame_bytes = jpeg.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

    def record(self, duration=10, fmt='MP4', width=None, height=None):
        if self.recording:
            return False, "Already recording"
        if duration > 60:
            duration = 60
        fmt = fmt.upper()
        if fmt not in ['MP4', 'MJPEG']:
            fmt = 'MP4'
        width = width or self.width
        height = height or self.height
        out_name = f"record_{int(time.time())}.{fmt.lower() if fmt != 'MJPEG' else 'avi'}"
        fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'MP4' else 'MJPG'))
        out = cv2.VideoWriter(out_name, fourcc, 20.0, (width, height))
        self.recording = True

        def record_thread_fn():
            start_time = time.time()
            while self.recording and (time.time() - start_time) < duration:
                frame = self.get_frame()
                if frame is None:
                    continue
                frame = cv2.resize(frame, (width, height))
                out.write(frame)
            out.release()
            self.recording = False

        self.record_thread = threading.Thread(target=record_thread_fn)
        self.record_thread.start()
        self.record_thread.join()
        return True, out_name

cam_mgr = CameraManager()

@app.route('/cam/start', methods=['POST'])
def cam_start():
    query = request.args
    width = int(query.get('width', cam_mgr.width))
    height = int(query.get('height', cam_mgr.height))
    fmt = query.get('format', cam_mgr.format)
    cam_id = int(query.get('id', cam_mgr.cam_id))
    success = cam_mgr.switch_camera(cam_id)
    started = cam_mgr.start(width, height, fmt)
    if not started:
        return jsonify({'status': 'error', 'message': 'Camera could not be started'}), 500
    return jsonify({'status': 'started', 'camera_id': cam_mgr.cam_id, 'resolution': [cam_mgr.width, cam_mgr.height], 'format': cam_mgr.format})

@app.route('/cam/stop', methods=['POST'])
def cam_stop():
    cam_mgr.stop()
    return jsonify({'status': 'stopped'})

@app.route('/cam/capture', methods=['GET'])
def cam_capture():
    fmt = request.args.get('format', cam_mgr.format)
    width = request.args.get('width')
    height = request.args.get('height')
    width = int(width) if width else cam_mgr.width
    height = int(height) if height else cam_mgr.height
    img_bytes, mime = cam_mgr.capture(fmt, width, height)
    if img_bytes is None:
        return jsonify({'status': 'error', 'message': 'Failed to capture image'}), 500
    return Response(img_bytes, mimetype=mime)

@app.route('/cam/stream', methods=['GET'])
def cam_stream():
    fmt = request.args.get('format', cam_mgr.format)
    width = request.args.get('width')
    height = request.args.get('height')
    width = int(width) if width else cam_mgr.width
    height = int(height) if height else cam_mgr.height
    return Response(cam_mgr.gen_stream(width, height, fmt),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/cam/res', methods=['PUT'])
def cam_set_res():
    data = request.get_json(force=True)
    width = int(data.get('width', cam_mgr.width))
    height = int(data.get('height', cam_mgr.height))
    cam_mgr.width = width
    cam_mgr.height = height
    cam_mgr.start(width, height)
    return jsonify({'status': 'ok', 'width': width, 'height': height})

@app.route('/cam/form', methods=['PUT'])
def cam_set_form():
    data = request.get_json(force=True)
    fmt = data.get('format', cam_mgr.format).upper()
    if fmt not in SUPPORTED_FORMATS:
        return jsonify({'status': 'error', 'message': 'Unsupported format'}), 400
    cam_mgr.format = fmt
    cam_mgr.start(fmt=fmt)
    return jsonify({'status': 'ok', 'format': fmt})

@app.route('/cam/record', methods=['POST'])
def cam_record():
    data = request.get_json(force=True)
    duration = int(data.get('duration', 10))
    if duration > 60:
        duration = 60
    fmt = data.get('format', cam_mgr.format)
    width = int(data.get('width', cam_mgr.width))
    height = int(data.get('height', cam_mgr.height))
    ok, result = cam_mgr.record(duration, fmt, width, height)
    if not ok:
        return jsonify({'status': 'error', 'message': result}), 500
    # Return video file for download
    return send_file(result, as_attachment=True)

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)