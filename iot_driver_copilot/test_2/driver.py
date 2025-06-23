import os
import json
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

# Device static info (could be dynamically fetched if device supports HTTP API)
DEVICE_INFO = {
    "device_name": "test2",
    "device_model": "DS-2CE16D0T-IRF",
    "manufacturer": "海康威视",
    "device_type": "摄像机"
}

VIDEO_CONFIG = {
    "resolution": "1920x1080",
    "infrared_range": "20m",
    "lens_options": ["2.8mm", "3.6mm", "6mm"],
    "protection_grade": "IP66",
    "video_output": "TVI"
}

# Env config
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

# There is no real TVI-to-HTTP transcoding in pure python, so we simulate an MJPEG stream
def fake_mjpeg_stream():
    import time
    import base64
    import io
    from PIL import Image, ImageDraw

    i = 0
    while True:
        # Create a simple image as a placeholder
        img = Image.new('RGB', (640, 360), color=(73, 109, 137))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"模拟视频流帧 {i}", fill=(255, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        frame = buf.getvalue()
        # MJPEG format
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.1)
        i += 1

@app.route("/video/feed", methods=["GET"])
def video_feed():
    """
    Streams a simulated MJPEG video. In a real implementation, this would
    convert the TVI video feed into HTTP MJPEG or HLS on the fly.
    """
    details = {
        "resolution": VIDEO_CONFIG["resolution"],
        "infrared_range": VIDEO_CONFIG["infrared_range"],
        "lens_options": VIDEO_CONFIG["lens_options"],
        "protection_grade": VIDEO_CONFIG["protection_grade"],
        "video_output": VIDEO_CONFIG["video_output"],
        "stream_type": "mjpeg",
        "mjpeg_url": "/video/feed/stream"
    }
    return jsonify(details)

@app.route("/video/feed/stream", methods=["GET"])
def video_feed_stream():
    """
    Returns a live MJPEG HTTP stream for browser/CLI access.
    """
    return Response(
        stream_with_context(fake_mjpeg_stream()),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route("/device/info", methods=["GET"])
def device_info():
    return jsonify(DEVICE_INFO)

@app.route("/commands/das", methods=["POST"])
def command_das():
    try:
        params = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400
    # Simulate action
    response = {
        "command": "das",
        "status": "received",
        "parameters": params or {}
    }
    return jsonify(response)

if __name__ == "__main__":
    app.run(debug=False, host=SERVER_HOST, port=SERVER_PORT)