import os
import threading
import time
import io
import cv2
import numpy as np
from flask import Flask, Response, jsonify, send_file, request, stream_with_context

# ================== ENVIRONMENT CONFIG ====================

HTTP_HOST = os.environ.get('DEVICE_HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('DEVICE_HTTP_PORT', '8080'))
CAMERA_INDEX = int(os.environ.get('DEVICE_USB_CAMERA_INDEX', '0'))
CAMERA_WIDTH = int(os.environ.get('DEVICE_USB_CAMERA_WIDTH', '640'))
CAMERA_HEIGHT = int(os.environ.get('DEVICE_USB_CAMERA_HEIGHT', '480'))
CAMERA_FPS = int(os.environ.get('DEVICE_USB_CAMERA_FPS', '15'))

# ================== CAMERA CONTROL LAYER ===================

class CameraController:
    def __init__(self, index=0, width=640, height=480, fps=15):
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.stream_active = False
        self.capture_active = False
        self.lock = threading.Lock()
        self.last_frame = None
        self.last_image = None
        self.background_thread = None

    def _open_camera(self):
        if self.cap is None or not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.index)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)

    def _close_camera(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def start_stream(self):
        with self.lock:
            if self.stream_active:
                return
            self._open_camera()
            self.stream_active = True
            self.background_thread = threading.Thread(target=self._update_stream_frame, daemon=True)
            self.background_thread.start()

    def stop_stream(self):
        with self.lock:
            self.stream_active = False
            self._close_camera()

    def _update_stream_frame(self):
        while self.stream_active:
            ret, frame = self.cap.read() if self.cap else (False, None)
            if ret:
                with self.lock:
                    self.last_frame = frame
            else:
                time.sleep(0.05)

    def gen_mjpeg_stream(self):
        while self.stream_active:
            frame = None
            with self.lock:
                if self.last_frame is not None:
                    frame = self.last_frame.copy()
            if frame is not None:
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            else:
                time.sleep(0.05)

    def get_snapshot(self):
        self._open_camera()
        ret, frame = self.cap.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to capture image")
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            raise RuntimeError("Failed to encode image")
        self.last_image = jpeg.tobytes()
        return self.last_image

    def start_capture(self):
        with self.lock:
            if self.capture_active:
                return
            self._open_camera()
            self.capture_active = True
            self.capture_thread = threading.Thread(target=self._continuous_capture, daemon=True)
            self.capture_thread.start()

    def stop_capture(self):
        with self.lock:
            self.capture_active = False

    def _continuous_capture(self):
        while self.capture_active:
            self.get_snapshot()
            time.sleep(1.0 / self.fps)

    def get_last_captured_image(self):
        with self.lock:
            return self.last_image

controller = CameraController(
    index=CAMERA_INDEX,
    width=CAMERA_WIDTH,
    height=CAMERA_HEIGHT,
    fps=CAMERA_FPS
)

# ================== FLASK API LAYER ===================

app = Flask(__name__)

@app.route('/cam/stream/start', methods=['POST'])
def cam_stream_start():
    controller.start_stream()
    return jsonify({'status': 'streaming started', 'detail': 'Access stream at /cam/stream or /camera/stream'})

@app.route('/cam/stream/stop', methods=['POST'])
@app.route('/camera/stopStream', methods=['POST'])
def cam_stream_stop():
    controller.stop_stream()
    return jsonify({'status': 'streaming stopped'})

@app.route('/cam/stream', methods=['GET'])
@app.route('/camera/stream', methods=['GET'])
def cam_stream():
    # Browser-accessible MJPEG stream
    if not controller.stream_active:
        controller.start_stream()
    return Response(
        stream_with_context(controller.gen_mjpeg_stream()),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/cam/snap', methods=['GET'])
def cam_snap():
    try:
        img = controller.get_snapshot()
        return send_file(
            io.BytesIO(img),
            mimetype='image/jpeg',
            as_attachment=False,
            download_name='snapshot.jpg'
        )
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/camera/capture', methods=['POST'])
def camera_capture():
    try:
        img = controller.get_snapshot()
        img_b64 = np.array(img).tobytes()
        return jsonify({
            'status': 'success',
            'format': 'jpeg',
            'detail': 'Image captured',
            'image_url': request.url_root.rstrip('/') + '/cam/snap'
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/cam/capture/start', methods=['POST'])
def cam_capture_start():
    controller.start_capture()
    return jsonify({'status': 'continuous capture started'})

@app.route('/cam/capture/stop', methods=['POST'])
def cam_capture_stop():
    controller.stop_capture()
    return jsonify({'status': 'continuous capture stopped'})

@app.route('/camera/startStream', methods=['POST'])
def camera_start_stream():
    controller.start_stream()
    return jsonify({
        'status': 'streaming started',
        'stream_url': request.url_root.rstrip('/') + '/camera/stream',
        'format': 'mjpeg'
    })

@app.route('/faked-h264', methods=['GET'])
def h264_stream():
    return jsonify({'status': 'not supported', 'detail': 'H.264 streaming not implemented in this driver.'}), 501

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)