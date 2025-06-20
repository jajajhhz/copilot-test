import os
import threading
import time
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Response
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import cv2
import uvicorn

# ---------------- Environment Variables ----------------
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
FRAME_WIDTH = int(os.environ.get("CAMERA_FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("CAMERA_FRAME_HEIGHT", "480"))
FRAME_RATE = int(os.environ.get("CAMERA_FRAME_RATE", "24"))

# ---------------- Camera Management ----------------

class CameraInfo(BaseModel):
    index: int
    name: str

def list_cameras(max_devices: int = 10) -> List[CameraInfo]:
    cameras = []
    for i in range(max_devices):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if os.name == "nt" else 0)
        if cap is not None and cap.isOpened():
            cameras.append(CameraInfo(index=i, name=f"USB Camera #{i}"))
            cap.release()
    return cameras

class CameraStream:
    def __init__(self):
        self.lock = threading.Lock()
        self.camera_index: Optional[int] = None
        self.cap: Optional[cv2.VideoCapture] = None
        self.streaming: bool = False
        self.frame: Optional[bytes] = None
        self.thread: Optional[threading.Thread] = None
        self.last_access = 0.0

    def start(self, camera_index: int, width: int, height: int, framerate: int):
        with self.lock:
            if self.streaming and self.camera_index == camera_index:
                return  # Already streaming this camera
            self.stop()
            self.cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW if os.name == "nt" else 0)
            if not self.cap.isOpened():
                self.cap = None
                raise RuntimeError(f"Cannot open camera index {camera_index}")
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_FPS, framerate)
            self.camera_index = camera_index
            self.streaming = True
            self.thread = threading.Thread(target=self._update, daemon=True)
            self.thread.start()

    def _update(self):
        while self.streaming and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if not ret:
                continue
            ret, jpeg = cv2.imencode('.jpg', frame)
            if ret:
                self.frame = jpeg.tobytes()
            time.sleep(1.0 / FRAME_RATE)
            # Stop if no access for some time (idle timeout)
            if (time.time() - self.last_access) > 60:
                self.stop()

    def read(self) -> Optional[bytes]:
        self.last_access = time.time()
        return self.frame

    def stop(self):
        with self.lock:
            self.streaming = False
            if self.cap:
                self.cap.release()
                self.cap = None
            self.camera_index = None
            self.frame = None
            self.thread = None

    def is_streaming(self) -> bool:
        return self.streaming and self.cap is not None and self.cap.isOpened()

    def get_camera_index(self) -> Optional[int]:
        return self.camera_index

camera_stream = CameraStream()

# ---------------- FastAPI App ----------------

app = FastAPI(title="Generic USB Camera HTTP Driver",
              description="HTTP API for streaming and controlling a generic USB camera",
              version="1.0.0")

# ----------- API Models ------------

class StreamStartRequest(BaseModel):
    camera_index: int = 0
    width: Optional[int] = FRAME_WIDTH
    height: Optional[int] = FRAME_HEIGHT
    framerate: Optional[int] = FRAME_RATE

class CameraSwitchRequest(BaseModel):
    camera_index: int

# ----------- API Endpoints ------------

@app.get("/cameras", response_model=List[CameraInfo])
def get_cameras():
    return list_cameras()

@app.post("/stream/start")
def start_stream(req: StreamStartRequest):
    try:
        camera_stream.start(req.camera_index, req.width, req.height, req.framerate)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "streaming", "camera_index": req.camera_index}

@app.post("/stream/stop")
def stop_stream():
    camera_stream.stop()
    return {"status": "stopped"}

def gen_mjpeg_stream():
    while camera_stream.is_streaming():
        frame = camera_stream.read()
        if frame is None:
            time.sleep(0.05)
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
    yield b''

@app.get("/stream/video")
def stream_video():
    if not camera_stream.is_streaming():
        raise HTTPException(status_code=404, detail="Stream not started")
    return StreamingResponse(gen_mjpeg_stream(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/capture")
def capture_image():
    if not camera_stream.is_streaming():
        raise HTTPException(status_code=404, detail="Stream not started")
    frame = camera_stream.read()
    if frame is None:
        raise HTTPException(status_code=500, detail="No frame available")
    return Response(content=frame, media_type="image/jpeg", headers={
        "Content-Disposition": "inline; filename=capture.jpg"
    })

# ----------- Main Entrypoint ------------

if __name__ == "__main__":
    uvicorn.run("main:app", host=HTTP_HOST, port=HTTP_PORT, reload=False)