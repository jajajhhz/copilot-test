import os
import threading
import io
import time

from flask import Flask, Response, send_file, jsonify, request

import cv2

app = Flask(__name__)

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8000"))
FRAME_WIDTH = int(os.environ.get("CAMERA_FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("CAMERA_FRAME_HEIGHT", "480"))
FPS = int(os.environ.get("CAMERA_FPS", "15"))

# Camera management and stream state
camera_lock = threading.Lock()
camera = None
streaming = False
last_frame = None
stop_stream_event = threading.Event()

def open_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            cam = cv2.VideoCapture(CAMERA_INDEX)
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cam.set(cv2.CAP_PROP_FPS, FPS)
            if not cam.isOpened():
                raise RuntimeError("Could not open USB camera")
            camera = cam

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def get_jpeg_frame():
    global camera
    open_camera()
    with camera_lock:
        ret, frame = camera.read()
        if not ret:
            raise RuntimeError("Failed to read frame from camera")
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            raise RuntimeError("Failed to encode frame as JPEG")
        return jpeg.tobytes()

def mjpeg_stream_generator():
    global camera, streaming, last_frame
    open_camera()
    while streaming and not stop_stream_event.is_set():
        with camera_lock:
            ret, frame = camera.read()
            if not ret:
                continue
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            last_frame = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + last_frame + b'\r\n')
        time.sleep(1.0 / FPS)
    release_camera()

@app.route('/image/capture', methods=['POST'])
def capture_image():
    try:
        jpeg_bytes = get_jpeg_frame()
        return Response(jpeg_bytes, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/stream/start', methods=['POST'])
def start_stream():
    global streaming, stop_stream_event
    if not streaming:
        try:
            open_camera()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        streaming = True
        stop_stream_event.clear()
        return jsonify({"status": "stream started"})
    else:
        return jsonify({"status": "stream already running"})

@app.route('/stream/stop', methods=['POST'])
def stop_stream():
    global streaming, stop_stream_event
    if streaming:
        streaming = False
        stop_stream_event.set()
        release_camera()
        return jsonify({"status": "stream stopped"})
    else:
        return jsonify({"status": "no stream running"})

@app.route('/stream', methods=['GET'])
def video_stream():
    global streaming
    if not streaming:
        return jsonify({"error": "stream not started"}), 400
    return Response(mjpeg_stream_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)