import os
import threading
import time
import io
from typing import Optional
from fastapi import FastAPI, Response, Request, Query, Body, status
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import cv2
import numpy as np

# --- Environment Variables ---
CAMERA_DEVICE_INDEX = int(os.environ.get("CAMERA_DEVICE_INDEX", 0))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8000))
DEFAULT_RESOLUTION = os.environ.get("DEFAULT_RESOLUTION", "640x480")
DEFAULT_FORMAT = os.environ.get("DEFAULT_FORMAT", "MJPEG")  # JPEG, PNG, MP4, MJPEG

# --- State Variables ---
state = {
    "camera": None,
    "is_running": False,
    "resolution": tuple(map(int, DEFAULT_RESOLUTION.lower().split("x"))),
    "format": DEFAULT_FORMAT.upper(),
    "lock": threading.Lock(),
    "last_frame": None,
}

# --- FastAPI App ---
app = FastAPI()

def open_camera():
    with state["lock"]:
        if state["camera"] is None or not state["camera"].isOpened():
            cam = cv2.VideoCapture(CAMERA_DEVICE_INDEX)
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, state["resolution"][0])
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, state["resolution"][1])
            if not cam.isOpened():
                raise RuntimeError("Unable to open camera device.")
            state["camera"] = cam
        state["is_running"] = True

def close_camera():
    with state["lock"]:
        if state["camera"] is not None:
            state["camera"].release()
            state["camera"] = None
        state["is_running"] = False

def get_camera():
    with state["lock"]:
        if state["camera"] is None or not state["camera"].isOpened():
            raise RuntimeError("Camera is not started.")
        return state["camera"]

def read_frame():
    cam = get_camera()
    ret, frame = cam.read()
    if not ret:
        raise RuntimeError("Failed to read frame from camera.")
    return frame

def set_resolution(width: int, height: int):
    with state["lock"]:
        state["resolution"] = (width, height)
        if state["camera"] is not None:
            state["camera"].set(cv2.CAP_PROP_FRAME_WIDTH, width)
            state["camera"].set(cv2.CAP_PROP_FRAME_HEIGHT, height)

def set_format(fmt: str):
    with state["lock"]:
        state["format"] = fmt.upper()

def encode_image(frame, fmt: str):
    if fmt.upper() == "JPEG":
        ret, buf = cv2.imencode(".jpg", frame)
        mime = "image/jpeg"
    elif fmt.upper() == "PNG":
        ret, buf = cv2.imencode(".png", frame)
        mime = "image/png"
    else:
        raise ValueError("Unsupported image format.")
    if not ret:
        raise RuntimeError("Image encoding failed.")
    return mime, buf.tobytes()

def mjpeg_stream_gen():
    boundary = "--frame"
    while True:
        try:
            frame = read_frame()
        except Exception:
            break
        mime, img_bytes = encode_image(frame, "JPEG")
        yield (
            b"%s\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n" % (boundary.encode(), mime.encode(), len(img_bytes))
            + img_bytes
            + b"\r\n"
        )
        time.sleep(0.03)  # ~30 FPS

def mp4_record_buffer(duration, width, height, fps):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    buf = cv2.VideoWriter('appsrc ! videoconvert ! x264enc tune=zerolatency ! mp4mux ! filesink location=app.mp4', fourcc, fps, (width, height))
    frames = []
    cam = get_camera()
    start = time.time()
    while time.time() - start < duration:
        ret, frame = cam.read()
        if not ret:
            break
        frames.append(frame)
        time.sleep(1.0 / fps)
    buf.release()
    # Encode video into memory
    temp_file = "temp_record.mp4"
    out = cv2.VideoWriter(temp_file, fourcc, fps, (width, height))
    for f in frames:
        out.write(f)
    out.release()
    with open(temp_file, "rb") as f:
        video_bytes = f.read()
    # Clean up
    try:
        os.remove(temp_file)
    except Exception:
        pass
    return video_bytes

def mjpeg_record_buffer(duration, width, height, fps):
    frames = []
    cam = get_camera()
    start = time.time()
    while time.time() - start < duration:
        ret, frame = cam.read()
        if not ret:
            break
        ret, buf = cv2.imencode(".jpg", frame)
        if not ret:
            continue
        frames.append(buf.tobytes())
        time.sleep(1.0 / fps)
    # Simple MJPEG: concatenate JPEGs with boundaries
    boundary = b"--frame"
    stream = b""
    for img_bytes in frames:
        stream += boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + img_bytes + b"\r\n"
    return stream

# --- Models ---
class FormatRequest(BaseModel):
    format: str

class ResolutionRequest(BaseModel):
    width: int
    height: int

class RecordRequest(BaseModel):
    duration: float  # seconds
    resolution: Optional[str] = None  # e.g., "1280x720"
    format: Optional[str] = None      # "MP4" or "MJPEG"

# --- Endpoints ---

@app.post("/cam/start")
async def start_cam(request: Request):
    params = dict(request.query_params)
    fmt = params.get("format", None)
    res = params.get("resolution", None)
    if res:
        try:
            width, height = map(int, res.lower().split("x"))
            set_resolution(width, height)
        except Exception:
            return JSONResponse({"error": "Invalid resolution."}, status_code=status.HTTP_400_BAD_REQUEST)
    if fmt:
        set_format(fmt)
    try:
        open_camera()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return {"status": "started"}

@app.post("/cam/stop")
async def stop_cam():
    try:
        close_camera()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return {"status": "stopped"}

@app.get("/cam/capture")
async def capture_image(
    width: Optional[int] = Query(None),
    height: Optional[int] = Query(None),
    format: Optional[str] = Query(None)
):
    if not state["is_running"]:
        return JSONResponse({"error": "Camera not started."}, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        frame = read_frame()
        if width and height:
            frame = cv2.resize(frame, (width, height))
        mime, img_bytes = encode_image(frame, format or state["format"])
        return Response(content=img_bytes, media_type=mime)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.put("/cam/form")
async def set_output_format(req: FormatRequest):
    fmt = req.format.upper()
    if fmt not in ("JPEG", "PNG", "MP4", "MJPEG"):
        return JSONResponse({"error": "Unsupported format."}, status_code=status.HTTP_400_BAD_REQUEST)
    set_format(fmt)
    return {"format": fmt}

@app.put("/cam/res")
async def set_output_res(req: ResolutionRequest):
    try:
        set_resolution(req.width, req.height)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return {"resolution": f"{req.width}x{req.height}"}

@app.get("/cam/stream")
async def stream_video(
    width: Optional[int] = Query(None),
    height: Optional[int] = Query(None),
    format: Optional[str] = Query(None)
):
    if not state["is_running"]:
        return JSONResponse({"error": "Camera not started."}, status_code=status.HTTP_400_BAD_REQUEST)
    fmt = (format or state["format"]).upper()
    if fmt == "MJPEG":
        return StreamingResponse(
            mjpeg_stream_gen(),
            media_type="multipart/x-mixed-replace; boundary=frame"
        )
    else:
        return JSONResponse({"error": "Only MJPEG stream is supported for /cam/stream."}, status_code=status.HTTP_400_BAD_REQUEST)

@app.post("/cam/record")
async def record_video(req: RecordRequest):
    if not state["is_running"]:
        return JSONResponse({"error": "Camera not started."}, status_code=status.HTTP_400_BAD_REQUEST)
    fmt = (req.format or state["format"]).upper()
    duration = min(req.duration, 60)
    try:
        width, height = state["resolution"]
        if req.resolution:
            width, height = map(int, req.resolution.lower().split("x"))
        fps = 20
        if fmt == "MP4":
            video_bytes = mp4_record_buffer(duration, width, height, fps)
            return Response(content=video_bytes, media_type="video/mp4")
        elif fmt == "MJPEG":
            stream = mjpeg_record_buffer(duration, width, height, fps)
            return Response(content=stream, media_type="multipart/x-mixed-replace; boundary=frame")
        else:
            return JSONResponse({"error": "Unsupported format for recording."}, status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

# --- Entrypoint ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)