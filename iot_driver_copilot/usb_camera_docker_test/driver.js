const express = require('express');
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const fs = require('fs');
const path = require('path');

const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT || '8080', 10);
const CAMERA_DEVICE = process.env.CAMERA_DEVICE || '/dev/video0';
const CAMERA_RESOLUTION = process.env.CAMERA_RESOLUTION || '640x480';
const CAMERA_FORMAT = process.env.CAMERA_FORMAT || 'mjpeg';

const app = express();
app.use(express.json());

// Driver state
let streaming = false;
let streamProcess = null;
let cameraIndex = 0;
let cameraDevice = CAMERA_DEVICE;
let clients = [];

// Detect available video devices (Linux only)
function listCameras() {
    // Only check /dev/video* on Linux
    if (os.platform() === 'linux') {
        return fs.readdirSync('/dev')
            .filter(f => f.startsWith('video'))
            .map(f => `/dev/${f}`);
    }
    // Not implemented for other OS
    return [CAMERA_DEVICE];
}

function getCameraInfo() {
    return {
        device_name: "USB Camera docker test",
        device_model: "Unknown",
        manufacturer: "Logitech",
        device_type: "Camera",
        resolution: CAMERA_RESOLUTION,
        format: CAMERA_FORMAT,
        device: cameraDevice
    };
}

// MJPEG streaming logic
function startMJPEGStream(res, device, resolution) {
    // Use ffmpeg to grab frames from the webcam and output them as MJPEG
    // ffmpeg must be available in the container/environment
    const [width, height] = resolution.split('x');

    const args = [
        '-f', 'v4l2',
        '-input_format', CAMERA_FORMAT,
        '-video_size', `${width}x${height}`,
        '-i', device,
        '-f', 'mjpeg',
        '-q:v', '5',
        '-'
    ];

    const ffmpeg = spawn('ffmpeg', args);

    ffmpeg.stderr.on('data', (data) => {
        // Optionally log ffmpeg errors
    });

    ffmpeg.on('close', (code) => {
        res.end();
    });

    res.writeHead(200, {
        'Content-Type': 'multipart/x-mixed-replace; boundary=ffserver',
        'Cache-Control': 'no-cache',
        'Connection': 'close',
        'Pragma': 'no-cache'
    });

    ffmpeg.stdout.pipe(res);

    res.on('close', () => {
        ffmpeg.kill('SIGTERM');
    });

    return ffmpeg;
}

// Single frame capture as JPEG
function captureFrame(device, resolution, cb) {
    const [width, height] = resolution.split('x');
    const args = [
        '-f', 'v4l2',
        '-input_format', CAMERA_FORMAT,
        '-video_size', `${width}x${height}`,
        '-i', device,
        '-vframes', '1',
        '-f', 'image2',
        '-q:v', '2',
        '-'
    ];
    const ffmpeg = spawn('ffmpeg', args);

    let chunks = [];
    ffmpeg.stdout.on('data', chunk => chunks.push(chunk));
    ffmpeg.on('close', code => {
        const image = Buffer.concat(chunks);
        cb(image);
    });
}

// API: GET /camera/stream
app.get('/camera/stream', (req, res) => {
    if (streaming) {
        // Only allow one streaming process for all clients
        res.status(409).json({ error: 'Stream already started' });
        return;
    }
    streaming = true;
    streamProcess = startMJPEGStream(res, cameraDevice, CAMERA_RESOLUTION);
    res.on('close', () => {
        streaming = false;
        if (streamProcess) {
            streamProcess.kill('SIGTERM');
        }
    });
});

// API: POST /camera/start
app.post('/camera/start', (req, res) => {
    if (streaming) {
        res.status(200).json({ message: 'Camera streaming already started' });
        return;
    }
    streaming = true;
    res.status(200).json({ message: 'Camera streaming started' });
});

// API: POST /camera/stop
app.post('/camera/stop', (req, res) => {
    if (!streaming) {
        res.status(200).json({ message: 'Camera already stopped' });
        return;
    }
    streaming = false;
    if (streamProcess) {
        streamProcess.kill('SIGTERM');
        streamProcess = null;
    }
    res.status(200).json({ message: 'Camera streaming stopped' });
});

// API: GET /camera/info
app.get('/camera/info', (req, res) => {
    res.status(200).json(getCameraInfo());
});

// API: GET /camera/captureframe
app.get('/camera/captureframe', (req, res) => {
    const resolution = req.query.resolution || CAMERA_RESOLUTION;
    captureFrame(cameraDevice, resolution, (image) => {
        res.writeHead(200, {
            'Content-Type': 'image/jpeg',
            'Content-Length': image.length
        });
        res.end(image);
    });
});

// API: POST /camera/switch
app.post('/camera/switch', (req, res) => {
    // Switch to next available camera
    const cameras = listCameras();
    if (cameras.length < 2) {
        res.status(404).json({ error: 'No other camera available to switch' });
        return;
    }
    cameraIndex = (cameraIndex + 1) % cameras.length;
    cameraDevice = cameras[cameraIndex];
    streaming = false;
    if (streamProcess) {
        streamProcess.kill('SIGTERM');
        streamProcess = null;
    }
    res.status(200).json({ message: `Switched to camera ${cameraDevice}` });
});

// Start HTTP server
const server = http.createServer(app);
server.listen(SERVER_PORT, SERVER_HOST, () => {
    // Ready
});