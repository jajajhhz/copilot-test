import os
import cv2
import io
import time
import threading
import numpy as np
from flask import Flask, Response, request, jsonify, send_file, abort

app = Flask(__name__)

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', 0))
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', 8000))

SUPPORTED_FORMATS = ['jpeg', 'png', 'mp4', 'mjpeg']
DEFAULT_FORMAT = 'jpeg'
DEFAULT_RES = (640, 480)

# Global camera state
class CameraState:
    def __init__(self):
        self.cap = None
        self.lock = threading.Lock()
        self.started = False
        self.resolution = DEFAULT_RES
        self.format = DEFAULT_FORMAT
        self.recording = False
        self.record_thread = None
        self.last_frame = None

    def open(self):
        with self.lock:
            if self.cap is None or not self.cap.isOpened():
                self.cap = cv2.VideoCapture(CAMERA_INDEX)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
                self.started = self.cap.isOpened()
            return self.started

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.started = False

    def set_resolution(self, width, height):
        with self.lock:
            self.resolution = (int(width), int(height))
            if self.cap is not None and self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))

    def set_format(self, fmt):
        fmt = fmt.lower()
        if fmt in SUPPORTED_FORMATS:
            self.format = fmt
            return True
        return False

    def get_frame(self):
        with self.lock:
            if self.cap is not None and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    self.last_frame = frame
                    return frame
            return self.last_frame

camera = CameraState()

@app.route('/cam/start', methods=['POST'])
def cam_start():
    params = request.args
    width = params.get('width', camera.resolution[0])
    height = params.get('height', camera.resolution[1])
    fmt = params.get('format', camera.format)
    camera.set_resolution(width, height)
    camera.set_format(fmt)
    started = camera.open()
    if started:
        return jsonify({'status': 'Camera started', 'resolution': camera.resolution, 'format': camera.format}), 200
    else:
        return jsonify({'error': 'Failed to start camera'}), 500

@app.route('/cam/stop', methods=['POST'])
def cam_stop():
    camera.close()
    return jsonify({'status': 'Camera stopped'}), 200

@app.route('/cam/res', methods=['PUT'])
def cam_set_resolution():
    data = request.get_json(force=True)
    width = data.get('width')
    height = data.get('height')
    if width is None or height is None:
        return jsonify({'error': 'Width and height must be provided'}), 400
    camera.set_resolution(width, height)
    return jsonify({'status': 'Resolution updated', 'resolution': camera.resolution}), 200

@app.route('/cam/form', methods=['PUT'])
def cam_set_format():
    data = request.get_json(force=True)
    fmt = data.get('format')
    if fmt is None or fmt.lower() not in SUPPORTED_FORMATS:
        return jsonify({'error': f'Format must be one of {SUPPORTED_FORMATS}'}), 400
    camera.set_format(fmt)
    return jsonify({'status': 'Format updated', 'format': camera.format}), 200

@app.route('/cam/capture', methods=['GET'])
def cam_capture():
    if not camera.open():
        return jsonify({'error': 'Camera not started'}), 400
    fmt = request.args.get('format', camera.format).lower()
    width = request.args.get('width', camera.resolution[0])
    height = request.args.get('height', camera.resolution[1])
    camera.set_resolution(width, height)
    frame = camera.get_frame()
    if frame is None:
        return jsonify({'error': 'Failed to capture image'}), 500
    if fmt not in ['jpeg', 'png']:
        return jsonify({'error': 'Supported image formats: jpeg, png'}), 400
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if fmt == 'jpeg' else [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    ret, buf = cv2.imencode('.' + fmt, frame, encode_param)
    if not ret:
        return jsonify({'error': 'Failed to encode image'}), 500
    return Response(buf.tobytes(), mimetype=f'image/{fmt}')

@app.route('/cam/stream', methods=['GET'])
def cam_stream():
    if not camera.open():
        return jsonify({'error': 'Camera not started'}), 400
    fmt = request.args.get('format', camera.format).lower()
    width = request.args.get('width', camera.resolution[0])
    height = request.args.get('height', camera.resolution[1])
    camera.set_resolution(width, height)

    if fmt == 'mjpeg':
        def mjpeg_stream():
            while camera.started:
                frame = camera.get_frame()
                if frame is None:
                    continue
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        return Response(mjpeg_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    elif fmt == 'mp4':
        def mp4_stream():
            import queue
            import av
            q = queue.Queue(maxsize=10)
            stop_event = threading.Event()

            def producer():
                while camera.started and not stop_event.is_set():
                    frame = camera.get_frame()
                    if frame is not None:
                        q.put(frame)
            t = threading.Thread(target=producer)
            t.daemon = True
            t.start()
            output = io.BytesIO()
            output_writer = av.open(output, mode='w', format='mp4')
            stream = output_writer.add_stream('h264', rate=20)
            stream.width = camera.resolution[0]
            stream.height = camera.resolution[1]
            stream.pix_fmt = 'yuv420p'
            frames_sent = 0
            try:
                while True:
                    try:
                        frame = q.get(timeout=1)
                    except Exception:
                        break
                    img = av.VideoFrame.from_ndarray(frame, format='bgr24')
                    for packet in stream.encode(img):
                        output_writer.mux(packet)
                        output.seek(0)
                        data = output.read()
                        if data:
                            yield data
                        output.truncate(0)
                        output.seek(0)
                        frames_sent += 1
            finally:
                for packet in stream.encode():
                    output_writer.mux(packet)
                output_writer.close()
                stop_event.set()
            t.join()
        try:
            import av
        except ImportError:
            return jsonify({'error': 'PyAV is required for mp4 streaming'}), 500
        return Response(mp4_stream(), mimetype='video/mp4')

    else:
        return jsonify({'error': 'Supported stream formats: mjpeg, mp4'}), 400

@app.route('/cam/record', methods=['POST'])
def cam_record():
    if not camera.open():
        return jsonify({'error': 'Camera not started'}), 400
    data = request.get_json(force=True)
    duration = int(data.get('duration', 10))
    if duration < 1 or duration > 60:
        return jsonify({'error': 'Duration must be between 1 and 60 seconds'}), 400
    width = data.get('width', camera.resolution[0])
    height = data.get('height', camera.resolution[1])
    fmt = data.get('format', camera.format).lower()
    camera.set_resolution(width, height)
    if fmt not in ['mp4', 'mjpeg']:
        return jsonify({'error': 'Supported record formats: mp4, mjpeg'}), 400

    fourcc = cv2.VideoWriter_fourcc(*('XVID' if fmt == 'mjpeg' else 'mp4v'))
    ext = 'avi' if fmt == 'mjpeg' else 'mp4'
    fps = 20
    out_file = f'/tmp/record_{int(time.time())}.{ext}'
    out = cv2.VideoWriter(out_file, fourcc, fps, (camera.resolution[0], camera.resolution[1]))

    start_time = time.time()
    while time.time() - start_time < duration:
        frame = camera.get_frame()
        if frame is not None:
            out.write(frame)
        else:
            time.sleep(0.01)
    out.release()
    return send_file(out_file, as_attachment=True, download_name=f'record.{ext}', mimetype=f'video/{ext}')

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)