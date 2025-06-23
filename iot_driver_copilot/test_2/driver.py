import os
import io
import json
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

# Config from environment variables
DEVICE_NAME = os.getenv("DEVICE_NAME", "test2")
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "DS-2CE16D0T-IRF")
DEVICE_MANUFACTURER = os.getenv("DEVICE_MANUFACTURER", "海康威视")
DEVICE_TYPE = os.getenv("DEVICE_TYPE", "摄像机")

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))

# Simulated TVI video stream generator (for demo only; in real use, replace with TVI capture logic)
def simulated_video_stream():
    # Simulate a multipart MJPEG HTTP stream with dummy JPEGs.
    # In real integration, replace this logic with actual TVI signal capture and encoding.
    import time
    import base64

    # A single black pixel JPEG
    jpeg_bytes = base64.b64decode(
        b'/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SEhQeGBoZFxcZGhohJCQkIC4nICIsKyIrLCk9NDQ0NTw7QDs+RkZGRj5IRz9JR0w4QkJCT0xK/2wBDAQ8NDhISFBQeGBoZGhoaGCgrKycrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrK//AABEIAAEAAQMBIgACEQEDEQH/xAAbAAACAgMBAAAAAAAAAAAAAAAFBgIDBAEAB//EADwQAAEDAgQDBgUEAgICAwAAAAEAAgMEEQUSITFBUQYTImFxgZEykaEUM0JSscHR8BVCU2KistHx/8QAGQEBAAMBAQAAAAAAAAAAAAAAAAIDBAEF/8QAJREBAAICAgICAgMBAAAAAAAAAAERAhIhAzFBUQRRImFxkcH/2gAMAwEAAhEDEQA/AO6iIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgP/Z'
    )
    boundary = "--frame"
    while True:
        yield (
            f"{boundary}\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
        ).encode("utf-8") + jpeg_bytes + b"\r\n"
        time.sleep(0.05)  # 20 FPS

@app.route("/video/feed", methods=["GET"])
def video_feed():
    # Accepts ?status=1 to return config/status JSON, otherwise streams video
    if request.args.get("status") == "1":
        info = {
            "resolution": "1920x1080 (1080P)",
            "infrared_range": "20m",
            "lens_options": ["2.8mm", "3.6mm", "6mm"],
            "protection_grade": "IP66",
            "video_output": "TVI",
            "power_input": "12V DC ±25%",
            "stream_url": f"http://{request.host}/video/feed"
        }
        return jsonify(info)
    # Otherwise, stream simulated MJPEG video (HTTP multipart/x-mixed-replace)
    return Response(
        stream_with_context(simulated_video_stream()),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@app.route("/device/info", methods=["GET"])
def device_info():
    info = {
        "device_name": DEVICE_NAME,
        "device_model": DEVICE_MODEL,
        "manufacturer": DEVICE_MANUFACTURER,
        "device_type": DEVICE_TYPE
    }
    return jsonify(info)

@app.route("/commands/das", methods=["POST"])
def command_das():
    try:
        params = request.get_json(force=True)
    except Exception:
        params = None
    # For demonstration, just echo back the command parameters and a dummy result
    result = {
        "status": "success",
        "command": "das",
        "parameters": params
    }
    return jsonify(result)

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT)