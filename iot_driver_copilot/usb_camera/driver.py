import os
import io
import cv2
import threading
import time
from flask import Flask, Response, jsonify, request, send_file, abort

# Configuration from environment variables
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8080'))
CAMERA_LIST_MAX = int(os.environ.get('CAMERA_LIST_MAX', '10'))  # Max cameras to probe

app = Flask(__name__)

# Global state for camera streaming
streaming_state = {
    'camera_index': None,
    'capture': None,
    'is_streaming': False,
    'frame': None,
    'lock': threading.Lock(),
    'thread': None
}

def list_usb_cameras(max_index=10):
    """List available USB cameras by probing indices."""
    available = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if hasattr(cv2, 'CAP_DSHOW') else 0)
        if cap is not None and cap.isOpened():
            # Query camera name if possible
            # OpenCV doesn't provide camera name directly
            available.append({'index': i, 'name': f'USB Camera {i}'})
            cap.release()
    return available

def camera_capture_thread(camera_index, width=None, height=None, fps=None):
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW if hasattr(cv2, 'CAP_DSHOW') else 0)
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if fps:
        cap.set(cv2.CAP_PROP_FPS, fps)
    with streaming_state['lock']:
        streaming_state['capture'] = cap
    while True:
        with streaming_state['lock']:
            if not streaming_state['is_streaming']:
                break
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        with streaming_state['lock']:
            streaming_state['frame'] = frame
        time.sleep(0.01)
    cap.release()
    with streaming_state['lock']:
        streaming_state['capture'] = None
        streaming_state['frame'] = None
        streaming_state['camera_index'] = None

def start_stream(camera_index, width=None, height=None, fps=None):
    with streaming_state['lock']:
        if streaming_state['is_streaming']:
            stop_stream()
        streaming_state['is_streaming'] = True
        streaming_state['camera_index'] = camera_index
        streaming_state['thread'] = threading.Thread(
            target=camera_capture_thread, 
            args=(camera_index, width, height, fps), 
            daemon=True)
        streaming_state['thread'].start()

def stop_stream():
    with streaming_state['lock']:
        streaming_state['is_streaming'] = False
        t = streaming_state.get('thread')
    if t and t.is_alive():
        t.join(timeout=2)
    with streaming_state['lock']:
        streaming_state['thread'] = None
        if streaming_state['capture']:
            streaming_state['capture'].release()
            streaming_state['capture'] = None
        streaming_state['frame'] = None
        streaming_state['camera_index'] = None

def gen_mjpeg_stream():
    while True:
        with streaming_state['lock']:
            if not streaming_state['is_streaming']:
                break
            frame = streaming_state['frame']
        if frame is None:
            time.sleep(0.05)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.03)

@app.route('/cameras', methods=['GET'])
def get_cameras():
    cameras = list_usb_cameras(CAMERA_LIST_MAX)
    return jsonify({'cameras': cameras})

@app.route('/stream/start', methods=['POST'])
def stream_start():
    data = request.get_json(force=True, silent=True)
    if not data:
        data = {}
    camera_index = data.get('camera_index', 0)
    width = data.get('width')
    height = data.get('height')
    fps = data.get('fps')
    cameras = list_usb_cameras(CAMERA_LIST_MAX)
    if not any(cam['index'] == camera_index for cam in cameras):
        return jsonify({'error': 'Camera not found'}), 404
    try:
        start_stream(camera_index, width, height, fps)
        return jsonify({
            'result': 'ok',
            'stream_url': f'http://{HTTP_HOST}:{HTTP_PORT}/stream/video'
        })
    except Exception as e:
        stop_stream()
        return jsonify({'error': str(e)}), 500

@app.route('/stream/stop', methods=['POST'])
def stream_stop():
    stop_stream()
    return jsonify({'result': 'stopped'})

@app.route('/stream/video', methods=['GET'])
def stream_video():
    with streaming_state['lock']:
        if not streaming_state['is_streaming']:
            abort(404, description='Stream is not started')
    return Response(gen_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
def capture_image():
    with streaming_state['lock']:
        frame = streaming_state['frame']
    if frame is None:
        return jsonify({'error': 'No frame available'}), 404
    fmt = request.args.get('format', 'jpeg').lower()
    ext = '.jpg' if fmt == 'jpeg' else '.png'
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90] if fmt == 'jpeg' else []
    ret, buf = cv2.imencode(ext, frame, encode_param)
    if not ret:
        return jsonify({'error': 'Failed to encode image'}), 500
    return send_file(
        io.BytesIO(buf.tobytes()),
        mimetype=f'image/{fmt}',
        as_attachment=True,
        download_name=f'capture{ext}'
    )

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)