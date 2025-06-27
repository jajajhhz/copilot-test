const http = require('http');
const os = require('os');
const { spawn } = require('child_process');
const { parse } = require('url');
const fs = require('fs');

// Environment Variables
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT, 10) || 8080;
const CAMERA_DEVICE = process.env.CAMERA_DEVICE || '/dev/video0';
const CAMERA_RESOLUTION = process.env.CAMERA_RESOLUTION || '640x480';
const CAMERA_MJPEG_FPS = process.env.CAMERA_MJPEG_FPS || '15';

// State
let cameraStreaming = false;
let mjpegProcess = null;
let streamClients = [];

// Device Info
const deviceInfo = {
    device_name: "USB Camera docker test",
    device_model: "Unknown",
    manufacturer: "Logitech",
    device_type: "Camera",
    resolution: CAMERA_RESOLUTION
};

function startMJPEGStream() {
    if (mjpegProcess) return true;

    // Try to use platform-native mjpeg streaming using ffmpeg or v4l2-ctl+ffmpeg
    // Since third-party command execution is forbidden, we restrict to native Node.js code.
    // Node.js has no native video capture support, but we must not execute external binaries.
    // As a workaround, we use a minimal MJPEG frame server using the Linux video4linux2 API.

    // However, Node.js has no built-in way to access raw video devices.
    // Therefore, we will use a minimalistic solution here: if a sample MJPEG or JPEG image file
    // exists, we stream that; otherwise, we return 501 Not Implemented for /camera/stream.

    // In a real device environment, this would use a Node.js native addon or WASM module
    // for v4l2 or libuvc access. Here, we simulate by streaming from a test file.
    // If /tmp/sample.jpg or /tmp/sample.mjpeg exists, we use those. Otherwise, stub.

    return false;
}

function stopMJPEGStream() {
    if (mjpegProcess) {
        mjpegProcess.kill();
        mjpegProcess = null;
    }
    cameraStreaming = false;
}

function serveMJPEG(req, res) {
    // Simulate MJPEG by looping a static JPEG image if possible.
    // In real deployment, use a native Node.js module for camera capture.

    const jpegPath = process.env.MJPEG_SAMPLE_JPEG || '/tmp/sample.jpg';
    if (!fs.existsSync(jpegPath)) {
        res.writeHead(501, { 'Content-Type': 'text/plain' });
        res.end('Live streaming is not implemented in this driver build.\n');
        return;
    }

    res.writeHead(200, {
        'Cache-Control': 'no-cache',
        'Connection': 'close',
        'Pragma': 'no-cache',
        'Content-Type': 'multipart/x-mixed-replace; boundary=frame'
    });

    cameraStreaming = true;
    let stopped = false;
    req.on('close', () => { stopped = true; });

    function sendFrame() {
        if (stopped || !cameraStreaming) return;
        fs.readFile(jpegPath, (err, data) => {
            if (err) {
                res.end();
                return;
            }
            res.write('--frame\r\n');
            res.write('Content-Type: image/jpeg\r\n');
            res.write(`Content-Length: ${data.length}\r\n\r\n`);
            res.write(data, 'binary');
            res.write('\r\n');
            setTimeout(sendFrame, 1000 / parseInt(CAMERA_MJPEG_FPS, 10));
        });
    }
    sendFrame();
}

// HTTP Server
const server = http.createServer((req, res) => {
    const url = parse(req.url, true);

    if (req.method === 'GET' && url.pathname === '/camera/stream') {
        if (!cameraStreaming) {
            if (!startMJPEGStream()) {
                serveMJPEG(req, res);
                return;
            }
        }
        serveMJPEG(req, res);
        return;
    }

    if (req.method === 'POST' && url.pathname === '/camera/stop') {
        stopMJPEGStream();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'stopped' }));
        return;
    }

    if (req.method === 'POST' && url.pathname === '/camera/start') {
        cameraStreaming = true;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'started' }));
        return;
    }

    if (req.method === 'GET' && url.pathname === '/camera/info') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            device_name: deviceInfo.device_name,
            device_model: deviceInfo.device_model,
            manufacturer: deviceInfo.manufacturer,
            device_type: deviceInfo.device_type,
            resolution: deviceInfo.resolution
        }));
        return;
    }

    if (req.method === 'GET' && url.pathname === '/camera/captureframe') {
        // Serve a single frame (JPEG) from the simulated camera
        const jpegPath = process.env.MJPEG_SAMPLE_JPEG || '/tmp/sample.jpg';
        if (!fs.existsSync(jpegPath)) {
            res.writeHead(501, { 'Content-Type': 'text/plain' });
            res.end('Frame capture not implemented in this driver build.\n');
            return;
        }
        fs.readFile(jpegPath, (err, data) => {
            if (err) {
                res.writeHead(500, { 'Content-Type': 'text/plain' });
                res.end('Error reading frame.\n');
                return;
            }
            res.writeHead(200, { 'Content-Type': 'image/jpeg' });
            res.end(data, 'binary');
        });
        return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
});

// Switch camera endpoint (not exposed in API, but can be made available via env variable)
process.on('SIGINT', () => {
    stopMJPEGStream();
    process.exit();
});

server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Camera Driver HTTP server running at http://${SERVER_HOST}:${SERVER_PORT}/`);
});