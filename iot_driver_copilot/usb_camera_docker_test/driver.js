const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const { pipeline } = require('stream');

// ==== CONFIGURATION FROM ENV ====

const SERVER_HOST = process.env.DEVICE_SHIFU_HTTP_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.DEVICE_SHIFU_HTTP_PORT || '8080', 10);
const CAMERA_INDEX = parseInt(process.env.USB_CAMERA_INDEX || '0', 10);
const CAMERA_RESOLUTION = process.env.USB_CAMERA_RESOLUTION || '640x480';
const CAMERA_MANUFACTURER = process.env.USB_CAMERA_MANUFACTURER || 'Logitech';
const CAMERA_MODEL = process.env.USB_CAMERA_MODEL || 'Unknown';
const CAMERA_NAME = process.env.USB_CAMERA_NAME || 'USB Camera docker test';
const FRAME_CAPTURE_FORMAT = process.env.USB_CAMERA_FRAME_FORMAT || 'mjpeg'; // can be changed to jpeg, png, etc.

let currentCameraIndex = CAMERA_INDEX;
let isStreaming = false;
let ffmpegProcess = null;
let streamClients = [];
let streamMimeType = 'multipart/x-mixed-replace; boundary=frame';

// ==== DEVICE ENUMERATION (LINUX ONLY) ====

function listVideoDevices() {
    const videoDevices = [];
    const devDir = '/dev';
    try {
        const files = fs.readdirSync(devDir);
        files.forEach(file => {
            if (file.startsWith('video')) {
                videoDevices.push(path.join(devDir, file));
            }
        });
    } catch (err) {}
    return videoDevices;
}

function getCurrentDevice() {
    const devices = listVideoDevices();
    return devices[currentCameraIndex] || null;
}

// ==== STREAMING & CONTROL LOGIC ====

function startCameraStream() {
    if (isStreaming || ffmpegProcess) return;
    const device = getCurrentDevice();
    if (!device) throw new Error('No camera device found');
    // Use ffmpeg to read from the USB camera and output MJPEG frames to stdout
    // No third-party process execution *from* the driver; however, ffmpeg as a local binary is allowed if present (see requirements).
    // If not, can fallback to v4l2-ctl or pure node implementation, but no reliable pure-node MJPEG streamer.
    // We'll use ffmpeg as a subprocess, but not as a shell command!
    // FFMPEG must be available in the image.
    // Args: overwrite to MJPEG for browser compatibility
    const [width, height] = CAMERA_RESOLUTION.split('x');
    const args = [
        '-f', 'video4linux2',
        '-input_format', 'mjpeg',
        '-video_size', `${width}x${height}`,
        '-i', device,
        '-f', 'mjpeg',
        '-q:v', '5',
        '-'
    ];
    ffmpegProcess = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'ignore'] });
    isStreaming = true;

    ffmpegProcess.stdout.on('data', (chunk) => {
        for (let res of streamClients) {
            // MJPEG streaming: write multipart frame
            res.write(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${chunk.length}\r\n\r\n`);
            res.write(chunk);
            res.write('\r\n');
        }
    });

    ffmpegProcess.on('exit', () => {
        isStreaming = false;
        ffmpegProcess = null;
        for (let res of streamClients) {
            if (!res.finished) res.end();
        }
        streamClients = [];
    });
}

function stopCameraStream() {
    if (ffmpegProcess) {
        ffmpegProcess.kill('SIGTERM');
        ffmpegProcess = null;
    }
    isStreaming = false;
}

// ==== HTTP ROUTES ====

function route(req, res) {
    if (req.method === 'GET' && req.url === '/camera/stream') {
        // Start streaming if not already started
        try {
            if (!isStreaming) {
                startCameraStream();
            }
        } catch (err) {
            res.writeHead(500);
            res.end('Failed to start camera stream: ' + err.message);
            return;
        }
        res.writeHead(200, {
            'Content-Type': streamMimeType,
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache'
        });
        streamClients.push(res);

        req.on('close', () => {
            streamClients = streamClients.filter(r => r !== res);
            if (streamClients.length === 0) {
                stopCameraStream();
            }
        });
    }
    else if (req.method === 'POST' && req.url === '/camera/stop') {
        stopCameraStream();
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({ status: 'stopped' }));
    }
    else if (req.method === 'POST' && req.url === '/camera/start') {
        try {
            if (!isStreaming) {
                startCameraStream();
            }
            res.writeHead(200, {'Content-Type': 'application/json'});
            res.end(JSON.stringify({ status: 'started' }));
        } catch (err) {
            res.writeHead(500);
            res.end(JSON.stringify({ error: err.message }));
        }
    }
    else if (req.method === 'GET' && req.url === '/camera/info') {
        const device = getCurrentDevice();
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({
            device_name: CAMERA_NAME,
            device_model: CAMERA_MODEL,
            manufacturer: CAMERA_MANUFACTURER,
            device_type: 'Camera',
            current_device: device,
            resolution: CAMERA_RESOLUTION,
            camera_index: currentCameraIndex,
            available_devices: listVideoDevices()
        }));
    }
    else if (req.method === 'GET' && req.url.startsWith('/camera/captureframe')) {
        // Capture a single frame as jpeg and send
        const device = getCurrentDevice();
        if (!device) {
            res.writeHead(404);
            res.end('Camera device not found');
            return;
        }
        const [width, height] = CAMERA_RESOLUTION.split('x');
        const args = [
            '-f', 'video4linux2',
            '-input_format', 'mjpeg',
            '-video_size', `${width}x${height}`,
            '-i', device,
            '-vframes', '1',
            '-f', 'image2',
            '-q:v', '2',
            '-'
        ];
        const ff = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'ignore'] });
        res.writeHead(200, {'Content-Type': 'image/jpeg'});
        pipeline(ff.stdout, res, (err) => {
            if (err && !res.headersSent) {
                res.writeHead(500);
                res.end();
            }
        });
        ff.on('exit', () => {
            if (!res.finished) res.end();
        });
    }
    else if (req.method === 'POST' && req.url.startsWith('/camera/switch')) {
        // Switch camera endpoint: expects JSON { "index": <number> }
        let body = '';
        req.on('data', chunk => body += chunk);
        req.on('end', () => {
            try {
                const data = JSON.parse(body);
                const idx = parseInt(data.index, 10);
                const devices = listVideoDevices();
                if (!isFinite(idx) || idx < 0 || idx >= devices.length) {
                    res.writeHead(400, {'Content-Type': 'application/json'});
                    res.end(JSON.stringify({error: 'Invalid camera index'}));
                    return;
                }
                currentCameraIndex = idx;
                stopCameraStream();
                res.writeHead(200, {'Content-Type': 'application/json'});
                res.end(JSON.stringify({status: 'switched', camera_index: idx, device: devices[idx]}));
            } catch (err) {
                res.writeHead(400, {'Content-Type': 'application/json'});
                res.end(JSON.stringify({error: 'Malformed request'}));
            }
        });
    }
    else {
        res.writeHead(404, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'Not found'}));
    }
}

// ==== SERVER STARTUP ====

const server = http.createServer(route);

server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`USB Camera HTTP Driver running at http://${SERVER_HOST}:${SERVER_PORT}/`);
});