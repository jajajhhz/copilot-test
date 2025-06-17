import os
import io
import cv2
import threading
import time
import platform
import json
from flask import Flask, Response, request, jsonify, send_file

app = Flask(__name__)

# ==== Environment Variables ====
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', 8080))
CAMERA_INDEXES = os.environ.get('CAMERA_INDEXES', '0,1,2,3,4')
DEFAULT_RES = os.environ.get('DEFAULT_RES', '640x480')
DEFAULT_FORMAT = os.environ.get('DEFAULT_FORMAT', 'JPEG')
MAX_RECORD_SECONDS = int(os.environ.get('MAX_RECORD_SECONDS', 60))

# ==== Global State ====
class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.camera = None
        self.camera_index = None
        self.resolution = self.parse_res(DEFAULT_RES)
        self.format = DEFAULT_FORMAT.upper()
        self.supported_formats = ['JPEG', 'PNG', 'MP4', 'MJPEG']
        self.supported_resolutions = [(320,240), (640,480), (1280,720), (1920,1080)]
        self.streaming = False
        self.recording = False
        self.record_thread = None
        self.record_stop_flag = threading.Event()

    def parse_res(self, resstr):
        try:
            w, h = resstr.lower().split('x')
            return (int(w), int(h))
        except Exception:
            return (640, 480)

    def enumerate_cameras(self):
        idxs = [int(idx) for idx in CAMERA_INDEXES.split(',') if idx.strip().isdigit()]
        available = []
        for idx in idxs:
            cap = cv2.VideoCapture(idx)
            if cap is not None and cap.isOpened():
                available.append(idx)
                cap.release()
        if not available:
            # Try default index 0 as last resort
            cap = cv2.VideoCapture(0)
            if cap is not None and cap.isOpened():
                available.append(0)
                cap.release()
        return available

    def open_camera(self, index=None, resolution=None, fmt=None):
        with self.lock:
            self.release_camera()
            available = self.enumerate_cameras()
            cam_idx = index if (index in available) else (available[0] if available else None)
            if cam_idx is None:
                return False, "No available camera devices found."
            self.camera = cv2.VideoCapture(cam_idx)
            if not self.camera.isOpened():
                self.camera = None
                return False, "Failed to open camera device."
            self.camera_index = cam_idx
            if resolution:
                self.set_resolution(resolution)
            else:
                self.set_resolution(self.resolution)
            if fmt:
                self.set_format(fmt)
            self.streaming = True
            return True, f"Camera device {cam_idx} started."

    def release_camera(self):
        if self.camera is not None:
            self.camera.release()
            self.camera = None
        self.streaming = False
        self.recording = False
        self.camera_index = None

    def set_resolution(self, res):
        if isinstance(res, str):
            res = self.parse_res(res)
        self.resolution = res
        if self.camera is not None:
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])

    def set_format(self, fmt):
        fmt = fmt.upper()
        if fmt in self.supported_formats:
            self.format = fmt
            return True
        return False

    def get_frame(self):
        if self.camera is None or not self.camera.isOpened():
            return False, None
        ret, frame = self.camera.read()
        if not ret or frame is None:
            return False, None
        return True, frame

    def stop(self):
        with self.lock:
            self.release_camera()

    def start(self, index=None, resolution=None, fmt=None):
        return self.open_camera(index, resolution, fmt)

    # MJPEG streaming generator
    def mjpeg_streamer(self):
        while self.streaming and self.camera is not None and self.camera.isOpened():
            ret, frame = self.camera.read()
            if not ret:
                continue
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            ret2, jpeg = cv2.imencode('.jpg', frame, encode_param)
            if not ret2:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        # End of stream
        yield b''

    # PNG streaming is not standard for MJPEG; only single capture
    def capture_image(self, fmt=None, resolution=None):
        with self.lock:
            if self.camera is None or not self.camera.isOpened():
                return False, None, "Camera is not started."
            if resolution:
                self.set_resolution(resolution)
            success, frame = self.get_frame()
            if not success:
                return False, None, "Failed to read frame."
            fmt = (fmt or self.format).upper()
            if fmt == 'PNG':
                ret, buf = cv2.imencode('.png', frame)
                mime = 'image/png'
                ext = '.png'
            else:
                ret, buf = cv2.imencode('.jpg', frame)
                mime = 'image/jpeg'
                ext = '.jpg'
            if not ret:
                return False, None, "Failed to encode image."
            return True, (io.BytesIO(buf.tobytes()), mime, ext), None

    # Video recording (MP4/MJPEG)
    def record_video(self, duration, fmt=None, resolution=None):
        with self.lock:
            if self.camera is None or not self.camera.isOpened():
                return False, None, "Camera is not started."
            fmt = (fmt or self.format).upper()
            if duration > MAX_RECORD_SECONDS:
                duration = MAX_RECORD_SECONDS
            if resolution:
                self.set_resolution(resolution)
            fourcc = cv2.VideoWriter_fourcc(*('mp4v' if fmt == 'MP4' else 'MJPG'))
            ext = '.mp4' if fmt == 'MP4' else '.avi'
            mime = 'video/mp4' if fmt == 'MP4' else 'video/x-motion-jpeg'
            tmpfile = f'camera_record_{int(time.time())}{ext}'
            out = cv2.VideoWriter(tmpfile, fourcc, 20.0, self.resolution)
            self.recording = True
            self.record_stop_flag.clear()
            start_time = time.time()
            try:
                while (time.time() - start_time) < duration and not self.record_stop_flag.is_set():
                    ret, frame = self.camera.read()
                    if not ret:
                        break
                    out.write(frame)
                out.release()
                self.recording = False
                if os.path.exists(tmpfile):
                    with open(tmpfile, 'rb') as f:
                        data = f.read()
                    os.remove(tmpfile)
                    return True, (io.BytesIO(data), mime, ext), None
                else:
                    return False, None, "Recording failed."
            except Exception as e:
                self.recording = False
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                return False, None, f"Recording error: {str(e)}"

    def stop_recording(self):
        self.record_stop_flag.set()
        self.recording = False

cam_manager = CameraManager()

# ==== API Routes ====

@app.route('/cam/start', methods=['POST'])
def cam_start():
    params = request.args
    json_data = request.get_json(silent=True) or {}
    res = params.get('res') or json_data.get('res') or None
    fmt = params.get('format') or json_data.get('format') or None
    index = params.get('index') or json_data.get('index') or None
    if index is not None:
        try:
            index = int(index)
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid camera index.'}), 400
    ok, msg = cam_manager.start(index=index, resolution=res, fmt=fmt)
    if ok:
        return jsonify({'success': True, 'message': msg})
    else:
        return jsonify({'success': False, 'error': msg}), 500

@app.route('/cam/stop', methods=['POST'])
def cam_stop():
    cam_manager.stop()
    return jsonify({'success': True, 'message': 'Camera stopped.'})

@app.route('/cam/capture', methods=['GET'])
def cam_capture():
    res = request.args.get('res')
    fmt = request.args.get('format', cam_manager.format)
    success, result, err = cam_manager.capture_image(fmt=fmt, resolution=res)
    if not success:
        return jsonify({'success': False, 'error': err}), 500
    buf, mime, ext = result
    buf.seek(0)
    return send_file(buf, mimetype=mime, download_name='capture'+ext)

@app.route('/cam/stream', methods=['GET'])
def cam_stream():
    res = request.args.get('res')
    fmt = request.args.get('format', cam_manager.format)
    if not cam_manager.streaming or cam_manager.camera is None:
        return jsonify({'success': False, 'error': 'Camera not started.'}), 400
    if fmt.upper() == 'MJPEG':
        if res:
            cam_manager.set_resolution(res)
        return Response(cam_manager.mjpeg_streamer(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return jsonify({'success': False, 'error': 'Only MJPEG format supported for streaming.'}), 400

@app.route('/cam/form', methods=['PUT'])
def cam_form():
    data = request.get_json()
    if not data or 'format' not in data:
        return jsonify({'success': False, 'error': 'Missing format field.'}), 400
    fmt = data['format']
    if cam_manager.set_format(fmt):
        return jsonify({'success': True, 'format': cam_manager.format})
    else:
        return jsonify({'success': False, 'error': 'Unsupported format.'}), 400

@app.route('/cam/res', methods=['PUT'])
def cam_res():
    data = request.get_json()
    if not data or 'width' not in data or 'height' not in data:
        return jsonify({'success': False, 'error': 'Missing width or height.'}), 400
    try:
        res = (int(data['width']), int(data['height']))
        cam_manager.set_resolution(res)
        return jsonify({'success': True, 'resolution': {'width': res[0], 'height': res[1]}})
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid resolution values.'}), 400

@app.route('/cam/record', methods=['POST'])
def cam_record():
    data = request.get_json()
    if not data or 'duration' not in data:
        return jsonify({'success': False, 'error': 'Missing duration.'}), 400
    duration = int(data['duration'])
    if duration > MAX_RECORD_SECONDS:
        duration = MAX_RECORD_SECONDS
    fmt = data.get('format', cam_manager.format)
    res = data.get('res')
    success, result, err = cam_manager.record_video(duration, fmt=fmt, resolution=res)
    if not success:
        return jsonify({'success': False, 'error': err}), 500
    buf, mime, ext = result
    buf.seek(0)
    return send_file(buf, mimetype=mime, download_name='record'+ext)

@app.route('/cam/status', methods=['GET'])
def cam_status():
    available = cam_manager.enumerate_cameras()
    status = {
        'camera_opened': cam_manager.camera is not None and cam_manager.camera.isOpened(),
        'camera_index': cam_manager.camera_index,
        'resolution': cam_manager.resolution,
        'format': cam_manager.format,
        'available_cameras': available,
        'recording': cam_manager.recording,
        'streaming': cam_manager.streaming
    }
    return jsonify(status)

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)