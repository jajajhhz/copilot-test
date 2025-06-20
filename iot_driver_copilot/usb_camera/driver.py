import os
import io
import threading
import base64
import time
from flask import Flask, Response, request, jsonify, send_file
import cv2

# Configuration from environment variables
DEVICE_NAME = os.getenv('DEVICE_NAME', 'USB Camera')
DEVICE_MODEL = os.getenv('DEVICE_MODEL', 'Generic USB Camera')
MANUFACTURER = os.getenv('MANUFACTURER', 'Generic')
DEVICE_TYPE = os.getenv('DEVICE_TYPE', 'Camera')
CAMERA_INDEX = int(os.getenv('CAMERA_INDEX', 0))
SERVER_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.getenv('SERVER_PORT', 8080))

app = Flask(__name__)

# Shared resources for camera streaming
camera_lock = threading.Lock()
camera_instance = {'cap': None, 'streaming': False, 'thread': None, 'stop_flag': False}

def open_camera():
    with camera_lock:
        if camera_instance['cap'] is None or not camera_instance['cap'].isOpened():
            cap = cv2.VideoCapture(CAMERA_INDEX)
            if not cap.isOpened():
                return None
            camera_instance['cap'] = cap
        return camera_instance['cap']

def close_camera():
    with camera_lock:
        if camera_instance['cap'] is not None:
            camera_instance['cap'].release()
            camera_instance['cap'] = None

def start_streaming():
    with camera_lock:
        if camera_instance['streaming']:
            return True
        cap = open_camera()
        if cap is None:
            return False
        camera_instance['stop_flag'] = False
        camera_instance['streaming'] = True
        return True

def stop_streaming():
    with camera_lock:
        camera_instance['stop_flag'] = True
        camera_instance['streaming'] = False
        close_camera()
        return True

def gen_mjpeg():
    while True:
        with camera_lock:
            if camera_instance['stop_flag']:
                break
            cap = camera_instance['cap']
            if cap is None or not cap.isOpened():
                break
            ret, frame = cap.read()
        if not ret:
            time.sleep(0.05)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            time.sleep(0.05)
            continue
        frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.03)  # ~30 fps
    stop_streaming()

@app.route('/stream/start', methods=['POST'])
def api_stream_start():
    success = start_streaming()
    if not success:
        return jsonify({'success': False, 'message': 'Camera not available'}), 503
    stream_url = f'http://{SERVER_HOST}:{SERVER_PORT}/stream?format=mjpeg'
    return jsonify({'success': True, 'message': 'Streaming started', 'stream_url': stream_url})

@app.route('/stream/stop', methods=['POST'])
def api_stream_stop():
    stop_streaming()
    return jsonify({'success': True, 'message': 'Streaming stopped'})

@app.route('/capture', methods=['POST'])
def api_capture_post():
    cap = open_camera()
    if cap is None:
        return jsonify({'success': False, 'message': 'Camera not available'}), 503
    with camera_lock:
        ret, frame = cap.read()
    if not ret:
        close_camera()
        return jsonify({'success': False, 'message': 'Failed to capture image'}), 500
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        close_camera()
        return jsonify({'success': False, 'message': 'Failed to encode image'}), 500
    img_bytes = jpeg.tobytes()
    metadata = {
        'device': DEVICE_NAME,
        'model': DEVICE_MODEL,
        'manufacturer': MANUFACTURER,
        'format': 'jpeg',
        'timestamp': int(time.time())
    }
    return Response(img_bytes, mimetype='image/jpeg', headers={'X-Metadata': str(metadata)})

@app.route('/capture', methods=['GET'])
def api_capture_get():
    cap = open_camera()
    if cap is None:
        return jsonify({'success': False, 'message': 'Camera not available'}), 503
    with camera_lock:
        ret, frame = cap.read()
    if not ret:
        close_camera()
        return jsonify({'success': False, 'message': 'Failed to capture image'}), 500
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        close_camera()
        return jsonify({'success': False, 'message': 'Failed to encode image'}), 500
    img_bytes = jpeg.tobytes()
    img_b64 = base64.b64encode(img_bytes).decode('utf-8')
    metadata = {
        'device': DEVICE_NAME,
        'model': DEVICE_MODEL,
        'manufacturer': MANUFACTURER,
        'format': 'jpeg',
        'timestamp': int(time.time())
    }
    return jsonify({'success': True, 'image_base64': img_b64, 'metadata': metadata})

@app.route('/stream', methods=['GET'])
def api_stream():
    req_format = request.args.get('format', 'mjpeg').lower()
    if req_format not in ['mjpeg', 'jpeg']:
        return jsonify({'success': False, 'message': 'Unsupported stream format'}), 400
    if req_format == 'mjpeg':
        if not start_streaming():
            return jsonify({'success': False, 'message': 'Camera not available'}), 503
        return Response(gen_mjpeg(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    elif req_format == 'jpeg':
        cap = open_camera()
        if cap is None:
            return jsonify({'success': False, 'message': 'Camera not available'}), 503
        with camera_lock:
            ret, frame = cap.read()
        if not ret:
            close_camera()
            return jsonify({'success': False, 'message': 'Failed to capture image'}), 500
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            close_camera()
            return jsonify({'success': False, 'message': 'Failed to encode image'}), 500
        img_bytes = jpeg.tobytes()
        return Response(img_bytes, mimetype='image/jpeg')
    else:
        return jsonify({'success': False, 'message': 'Unsupported format'}), 400

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)