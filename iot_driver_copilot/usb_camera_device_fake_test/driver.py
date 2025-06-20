import os
import threading
import time
from flask import Flask, Response, jsonify, send_file, url_for
import cv2
import io

app = Flask(__name__)

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', 0))  # Default USB camera index 0
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))
STREAM_FPS = int(os.environ.get('STREAM_FPS', 15))
STREAM_QUALITY = int(os.environ.get('STREAM_QUALITY', 80))  # JPEG quality 0-100

# Camera/streaming state
camera_lock = threading.Lock()
camera = None
streaming = False
last_frame = None
bitrate = 0
frame_counter = 0
start_time = None

def open_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            camera = cv2.VideoCapture(CAMERA_INDEX)
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def get_frame():
    global last_frame
    with camera_lock:
        if camera is not None and camera.isOpened():
            ret, frame = camera.read()
            if ret:
                last_frame = frame
                return frame
    return None

def encode_jpeg(frame):
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_QUALITY]
    ret, jpeg = cv2.imencode('.jpg', frame, encode_param)
    if ret:
        return jpeg.tobytes()
    return None

def gen_mjpeg_stream():
    global streaming, bitrate, frame_counter, start_time
    open_camera()
    streaming = True
    frame_counter = 0
    start_time = time.time()
    try:
        while streaming:
            frame = get_frame()
            if frame is not None:
                jpg = encode_jpeg(frame)
                frame_counter += 1
                if jpg is not None:
                    bitrate = (bitrate * (frame_counter - 1) + len(jpg) * 8 * STREAM_FPS) / frame_counter
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
            time.sleep(1.0 / STREAM_FPS)
    finally:
        # Only release camera if not streaming anymore
        if not streaming:
            release_camera()

@app.route('/stream', methods=['GET'])
def get_stream_status():
    # Returns stream status and metadata
    status = {
        'streaming': streaming,
        'stream_url': url_for('get_camera_stream', _external=True) if streaming else None,
        'bitrate_bps': int(bitrate) if streaming else 0,
        'fps': STREAM_FPS if streaming else 0,
        'device': {
            'model': 'USB camera device fake test',
            'manufacturer': 'Unknown',
            'type': 'Camera'
        }
    }
    return jsonify(status)

@app.route('/camera/start', methods=['POST'])
@app.route('/stream/start', methods=['POST'])
def start_stream():
    global streaming
    if not streaming:
        open_camera()
        # Start streaming in background if not already running
        streaming = True
    return jsonify({
        'status': 'streaming_started',
        'stream_url': url_for('get_camera_stream', _external=True),
        'fps': STREAM_FPS
    })

@app.route('/camera/stop', methods=['POST'])
@app.route('/stream/stop', methods=['POST'])
def stop_stream():
    global streaming
    streaming = False
    release_camera()
    return jsonify({'status': 'streaming_stopped'})

@app.route('/capture', methods=['POST'])
@app.route('/camera/capture', methods=['POST'])
def capture_image():
    open_camera()
    frame = get_frame()
    if frame is not None:
        jpg = encode_jpeg(frame)
        if jpg is not None:
            return Response(jpg, mimetype='image/jpeg')
    return jsonify({'error': 'Failed to capture image'}), 500

@app.route('/camera/stream', methods=['GET'])
def get_camera_stream():
    if not streaming:
        return jsonify({'error': 'Stream is not active'}), 400
    return Response(gen_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)