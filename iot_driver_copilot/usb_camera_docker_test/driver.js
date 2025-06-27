const express = require('express');
const { Readable } = require('stream');
const NodeWebcam = require('node-webcam');

// Configuration from environment variables
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT || '8080', 10);
const CAMERA_DEVICE = process.env.CAMERA_DEVICE || null; // e.g., '/dev/video0'
const CAMERA_WIDTH = parseInt(process.env.CAMERA_WIDTH || '640', 10);
const CAMERA_HEIGHT = parseInt(process.env.CAMERA_HEIGHT || '480', 10);
const CAMERA_FPS = parseInt(process.env.CAMERA_FPS || '10', 10);
const CAMERA_MJPEG_QUALITY = parseInt(process.env.CAMERA_MJPEG_QUALITY || '80', 10);

// State
let isStreaming = false;
let streamClients = [];
let streamInterval = null;
let selectedCamera = CAMERA_DEVICE;

const app = express();
app.use(express.json());

// Camera options
function getWebcamOpts() {
  return {
    width: CAMERA_WIDTH,
    height: CAMERA_HEIGHT,
    quality: CAMERA_MJPEG_QUALITY,
    device: selectedCamera,
    fps: CAMERA_FPS,
    saveShots: false,
    output: "jpeg",
    callbackReturn: "buffer",
    verbose: false
  };
}

// List available cameras using node-webcam
function listAvailableCameras(cb) {
  NodeWebcam.list(cb);
}

// Start streaming
function startStream() {
  if (isStreaming) return;
  isStreaming = true;
  streamInterval = setInterval(() => {
    if (streamClients.length === 0) return;
    NodeWebcam.capture("stream_frame", getWebcamOpts(), function(err, frameBuffer) {
      if (err || !frameBuffer) return;
      // Write MJPEG frame to all clients
      const header = `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${frameBuffer.length}\r\n\r\n`;
      streamClients.forEach(res => {
        res.write(header);
        res.write(frameBuffer);
        res.write('\r\n');
      });
    });
  }, 1000 / CAMERA_FPS);
}

// Stop streaming
function stopStream() {
  isStreaming = false;
  if (streamInterval) {
    clearInterval(streamInterval);
    streamInterval = null;
  }
  streamClients.forEach(res => {
    try { res.end(); } catch (e) {}
  });
  streamClients = [];
}

// GET /camera/info
app.get('/camera/info', (req, res) => {
  res.json({
    device_name: "USB Camera docker test",
    device_model: "Unknown",
    manufacturer: "Logitech",
    device_type: "Camera",
    resolution: `${CAMERA_WIDTH}x${CAMERA_HEIGHT}`,
    camera_device: selectedCamera
  });
});

// POST /camera/start
app.post('/camera/start', (req, res) => {
  if (!isStreaming) {
    startStream();
  }
  res.status(200).json({ status: "streaming_started" });
});

// POST /camera/stop
app.post('/camera/stop', (req, res) => {
  stopStream();
  res.status(200).json({ status: "streaming_stopped" });
});

// GET /camera/stream - MJPEG multipart stream
app.get('/camera/stream', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
    'Cache-Control': 'no-cache',
    'Connection': 'close',
    'Pragma': 'no-cache'
  });
  streamClients.push(res);

  req.on('close', () => {
    streamClients = streamClients.filter(c => c !== res);
    if (streamClients.length === 0) {
      stopStream();
    }
  });

  // Auto-start stream if not running
  if (!isStreaming) {
    startStream();
  }
});

// GET /camera/captureframe - Returns a single JPEG image
app.get('/camera/captureframe', (req, res) => {
  NodeWebcam.capture("capture_frame", getWebcamOpts(), function(err, frameBuffer) {
    if (err || !frameBuffer) {
      res.status(500).json({ error: "Failed to capture frame" });
      return;
    }
    res.writeHead(200, {
      'Content-Type': 'image/jpeg',
      'Content-Length': frameBuffer.length
    });
    res.end(frameBuffer);
  });
});

// POST /camera/switch - Switch to another camera device
app.post('/camera/switch', (req, res) => {
  let { device } = req.body;
  if (!device) {
    res.status(400).json({ error: "Missing 'device' in body" });
    return;
  }
  listAvailableCameras(function(list) {
    if (!list || !list.includes(device)) {
      res.status(404).json({ error: "Device not found" });
      return;
    }
    selectedCamera = device;
    stopStream();
    res.status(200).json({ status: "switched", device });
  });
});

// GET /camera/list - List available camera devices
app.get('/camera/list', (req, res) => {
  listAvailableCameras(function(list) {
    res.status(200).json({ devices: list });
  });
});

app.listen(SERVER_PORT, SERVER_HOST, () => {
  console.log(`Camera driver listening at http://${SERVER_HOST}:${SERVER_PORT}`);
});