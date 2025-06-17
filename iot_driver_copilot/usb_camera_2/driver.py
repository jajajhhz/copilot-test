import os
import threading
import time
import io
from flask import Flask, Response, request, jsonify, send_file
import cv2
import numpy as np

app = Flask(__name__)

# Environment configuration
SERVER_HOST = os.environ.get('DEVICE_SHIFU_SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('DEVICE_SHIFU_SERVER_PORT', '8080'))

# Camera state and configuration
class CameraManager:
    def __init__(self):
        self.cams = self.enumerate_cameras()
        self.cam_id = 0 if self.cams else None
        self.cap = None
        self.lock = threading.Lock()
        self.format = 'JPEG'  # Default format
        self.width = 640
        self.height = 480
        self.fps = 20
        self.recording = False
        self.record_thread = None
        self.last_frame = None
        self.running = False

    def enumerate_cameras(self, max_cams=10):
        available = []
        for i in range(max_cams):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            if cap is not None and cap.isOpened():
                available.append(i)
                cap.release()
        if not available:
            # Try default camera
            cap = cv2.VideoCapture(0)
            if cap is not None and cap.isOpened():
                available.append(0)
                cap.release()
        return available

    def set_camera(self, cam_id):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.cam_id = cam_id
            self.cap = cv2.VideoCapture(cam_id, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.running = self.cap.isOpened()
            return self.running

    def start(self, width=None, height=None, fps=None, fmt=None, cam_id=None):
        with self.lock:
            if cam_id is not None:
                if cam_id in self.cams:
                    self.cam_id = cam_id
            if width:
                self.width = int(width)
            if height:
                self.height = int(height)
            if fps:
                self.fps = int(fps)
            if fmt:
                self.format = fmt.upper()
            if self.cap is not None:
                self.cap.release()
            self.cap = cv2.VideoCapture(self.cam_id, cv2.CAP_DSHOW if os.name == 'nt' else 0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            self.running = self.cap.isOpened()
            return self.running

    def stop(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.running = False

    def get_frame(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                return None
            ret, frame = self.cap.read()
            if not ret:
                return None
            self.last_frame = frame
            return frame

    def capture(self, fmt=None, width=None, height=None):
        frame = self.get_frame()
        if frame is None:
            return None, None
        if width and height:
            frame = cv2.resize(frame, (int(width), int(height)))
        encode_fmt = (fmt or self.format).upper()
        if encode_fmt == 'PNG':
            ext = '.png'
            result, img = cv2.imencode(ext, frame)
            mimetype = 'image/png'
        else:
            ext = '.jpg'
            result, img = cv2.imencode(ext, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            mimetype = 'image/jpeg'
        if not result:
            return None, None
        return img.tobytes(), mimetype

    def get_stream_generator(self, fmt=None, width=None, height=None):
        encode_fmt = (fmt or self.format).upper()
        mimetype = 'image/jpeg' if encode_fmt in ['JPEG', 'JPG', 'MJPEG'] else 'image/png'
        ext = '.jpg' if encode_fmt in ['JPEG', 'JPG', 'MJPEG'] else '.png'
        while True:
            frame = self.get_frame()
            if frame is None:
                continue
            if width and height:
                frame = cv2.resize(frame, (int(width), int(height)))
            if encode_fmt == 'PNG':
                result, img = cv2.imencode(ext, frame)
            else:
                result, img = cv2.imencode(ext, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not result:
                continue
            yield (b'--frame\r\n' +
                   b'Content-Type: ' + mimetype.encode() + b'\r\n\r\n' +
                   img.tobytes() + b'\r\n')
            time.sleep(1.0 / self.fps if self.fps else 0.05)

    def record(self, duration=10, fmt='MP4', width=None, height=None):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                return None
            fourcc = cv2.VideoWriter_fourcc(*('MP4V' if fmt.upper() == 'MP4' else 'MJPG'))
            out_w = int(width) if width else self.width
            out_h = int(height) if height else self.height
            temp_video = 'temp_record.' + ('mp4' if fmt.upper() == 'MP4' else 'avi')
            out = cv2.VideoWriter(temp_video, fourcc, self.fps, (out_w, out_h))
            start_time = time.time()
            frames_written = 0
            self.recording = True
            while self.recording and (time.time() - start_time) < duration:
                frame = self.get_frame()
                if frame is None:
                    continue
                frame = cv2.resize(frame, (out_w, out_h))
                out.write(frame)
                frames_written += 1
            out.release()
            self.recording = False
            if frames_written == 0:
                return None
            return temp_video

    def stop_record(self):
        self.recording = False

camera_mgr = CameraManager()

@app.route('/cam/start', methods=['POST'])
def cam_start():
    params = request.args
    data = request.get_json(silent=True) or {}
    width = params.get('width') or data.get('width')
    height = params.get('height') or data.get('height')
    fps = params.get('fps') or data.get('fps')
    fmt = params.get('format') or data.get('format')
    cam_id = params.get('cam_id') or data.get('cam_id')
    if cam_id is not None:
        try:
            cam_id = int(cam_id)
        except:
            return jsonify({'error': 'Invalid cam_id'}), 400
        if cam_id not in camera_mgr.cams:
            return jsonify({'error': 'Camera id not available'}), 404
    started = camera_mgr.start(width, height, fps, fmt, cam_id)
    if started:
        return jsonify({'status': 'started', 'cam_id': camera_mgr.cam_id, 'format': camera_mgr.format, 'resolution': [camera_mgr.width, camera_mgr.height]})
    else:
        return jsonify({'error': 'Failed to start camera'}), 500

@app.route('/cam/stop', methods=['POST'])
def cam_stop():
    camera_mgr.stop()
    return jsonify({'status': 'stopped'})

@app.route('/cam/capture', methods=['GET'])
def cam_capture():
    fmt = request.args.get('format')
    width = request.args.get('width')
    height = request.args.get('height')
    img_bytes, mimetype = camera_mgr.capture(fmt, width, height)
    if img_bytes is None:
        return jsonify({'error': 'Failed to capture image'}), 500
    return Response(img_bytes, mimetype=mimetype)

@app.route('/cam/stream', methods=['GET'])
def cam_stream():
    fmt = request.args.get('format', 'MJPEG')
    width = request.args.get('width')
    height = request.args.get('height')
    return Response(camera_mgr.get_stream_generator(fmt, width, height),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/cam/record', methods=['POST'])
def cam_record():
    data = request.get_json(force=True)
    duration = int(data.get('duration', 10))
    duration = min(max(duration, 1), 60)
    fmt = data.get('format', 'MP4')
    width = data.get('width')
    height = data.get('height')
    video_file = camera_mgr.record(duration, fmt, width, height)
    if video_file is None:
        return jsonify({'error': 'Recording failed'}), 500
    return send_file(video_file, as_attachment=True)
    
@app.route('/cam/form', methods=['PUT'])
def cam_form():
    data = request.get_json(force=True)
    fmt = data.get('format')
    if not fmt or fmt.upper() not in ['JPEG', 'PNG', 'MP4', 'MJPEG']:
        return jsonify({'error': 'Unsupported format'}), 400
    camera_mgr.format = fmt.upper()
    return jsonify({'status': 'format set', 'format': camera_mgr.format})

@app.route('/cam/res', methods=['PUT'])
def cam_res():
    data = request.get_json(force=True)
    width = data.get('width')
    height = data.get('height')
    if not width or not height:
        return jsonify({'error': 'width and height required'}), 400
    camera_mgr.width = int(width)
    camera_mgr.height = int(height)
    # If running, update stream
    if camera_mgr.cap is not None and camera_mgr.cap.isOpened():
        camera_mgr.cap.set(cv2.CAP_PROP_FRAME_WIDTH, camera_mgr.width)
        camera_mgr.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, camera_mgr.height)
    return jsonify({'status': 'resolution set', 'resolution': [camera_mgr.width, camera_mgr.height]})

@app.route('/cam/enumerate', methods=['GET'])
def cam_enumerate():
    return jsonify({'cameras': camera_mgr.cams, 'current': camera_mgr.cam_id})

@app.route('/cam/switch', methods=['POST'])
def cam_switch():
    data = request.get_json(force=True)
    cam_id = data.get('cam_id')
    if cam_id is None:
        return jsonify({'error': 'cam_id required'}), 400
    try:
        cam_id = int(cam_id)
    except:
        return jsonify({'error': 'Invalid cam_id'}), 400
    if cam_id not in camera_mgr.cams:
        return jsonify({'error': 'Camera id not available'}), 404
    ok = camera_mgr.set_camera(cam_id)
    if ok:
        return jsonify({'status': 'switched', 'cam_id': cam_id})
    else:
        return jsonify({'error': 'Camera switch failed'}), 500

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)