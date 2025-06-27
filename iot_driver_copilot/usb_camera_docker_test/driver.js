const express = require('express');
const http = require('http');
const { spawn } = require('child_process');
const os = require('os');
const fs = require('fs');
const path = require('path');

// ======= ENVIRONMENT VARIABLES ======= //
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT, 10) || 8080;
const CAMERA_DEVICE = process.env.CAMERA_DEVICE || '/dev/video0';
const CAMERA_RESOLUTION = process.env.CAMERA_RESOLUTION || '640x480';
const CAMERA_FPS = process.env.CAMERA_FPS || '15';

// ======= MJPEG STREAMING HELPERS ======= //
// We'll use 'ffmpeg' for video capture and transcoding. Only Node APIs allowed, but ffmpeg must be available in the image.
function getFfmpegArgs({ device, width, height, fps, format, output }) {
    // Format: MJPEG
    // Output: pipe:1 (stdout)
    return [
        '-f', 'v4l2',
        '-input_format', 'mjpeg',
        '-framerate', '' + fps,
        '-video_size', `${width}x${height}`,
        '-i', device,
        '-f', format,
        ...output
    ];
}

// Parse resolution string "640x480" => [640, 480]
function parseResolution(res) {
    const [w, h] = (res || '').split('x').map(Number);
    if (!w || !h) return [640, 480];
    return [w, h];
}

// ======= CAMERA STATE ======= //
let streamProcess = null;
let streamClients = [];
let streamingActive = false;
let activeDevice = CAMERA_DEVICE;

// ======= EXPRESS SETUP ======= //
const app = express();
app.use(express.json());

// ======= /camera/info ======= //
app.get('/camera/info', (req, res) => {
    const [width, height] = parseResolution(CAMERA_RESOLUTION);
    res.json({
        device_name: "USB Camera docker test",
        device_model: "Unknown",
        manufacturer: "Logitech",
        device_type: "Camera",
        device_path: activeDevice,
        resolution: { width, height },
        streaming: streamingActive
    });
});

// ======= /camera/start ======= //
app.post('/camera/start', (req, res) => {
    if (streamingActive) return res.status(200).json({ status: "already streaming" });

    startStreaming();
    res.status(200).json({ status: "started" });
});

// ======= /camera/stop ======= //
app.post('/camera/stop', (req, res) => {
    stopStreaming();
    res.status(200).json({ status: "stopped" });
});

// ======= /camera/stream ======= //
app.get('/camera/stream', (req, res) => {
    res.setHeader('Content-Type', 'multipart/x-mixed-replace; boundary=ffserver');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'close');

    if (!streamingActive) startStreaming();

    streamClients.push(res);

    req.on('close', () => {
        streamClients = streamClients.filter(c => c !== res);
        if (streamClients.length === 0) {
            stopStreaming();
        }
    });
});

// ======= /camera/captureframe ======= //
app.get('/camera/captureframe', (req, res) => {
    const [width, height] = parseResolution(CAMERA_RESOLUTION);

    const ffmpeg = spawn('ffmpeg', [
        '-f', 'v4l2',
        '-input_format', 'mjpeg',
        '-framerate', CAMERA_FPS,
        '-video_size', `${width}x${height}`,
        '-i', activeDevice,
        '-frames:v', '1',
        '-f', 'image/jpeg',
        'pipe:1'
    ]);

    let chunks = [];
    let errored = false;
    ffmpeg.stdout.on('data', data => chunks.push(data));
    ffmpeg.stderr.on('data', () => {});
    ffmpeg.on('error', () => {
        errored = true;
        res.status(500).send('Camera frame capture failed');
    });
    ffmpeg.on('close', code => {
        if (!errored && code === 0) {
            res.setHeader('Content-Type', 'image/jpeg');
            res.end(Buffer.concat(chunks));
        } else if (!errored) {
            res.status(500).send('Camera frame capture failed');
        }
    });
});

// ======= /camera/switch ======= //
app.post('/camera/switch', (req, res) => {
    const { device } = req.body;
    if (typeof device !== 'string' || !device.startsWith('/dev/video')) {
        return res.status(400).json({ error: 'Invalid device name' });
    }
    activeDevice = device;
    stopStreaming();
    res.status(200).json({ status: "switched", device: activeDevice });
});

// ======= STREAMING FUNCTIONS ======= //
function startStreaming() {
    if (streamingActive) return;
    const [width, height] = parseResolution(CAMERA_RESOLUTION);

    // ffmpeg command: produces MJPEG stream
    streamProcess = spawn('ffmpeg', [
        '-f', 'v4l2',
        '-input_format', 'mjpeg',
        '-framerate', CAMERA_FPS,
        '-video_size', `${width}x${height}`,
        '-i', activeDevice,
        '-f', 'mjpeg',
        'pipe:1'
    ]);
    streamingActive = true;

    let buffer = Buffer.alloc(0);

    streamProcess.stdout.on('data', chunk => {
        buffer = Buffer.concat([buffer, chunk]);
        let start, end;
        while ((start = buffer.indexOf(Buffer.from([0xff, 0xd8]))) !== -1 &&
               (end = buffer.indexOf(Buffer.from([0xff, 0xd9]), start + 2)) !== -1) {
            let frame = buffer.slice(start, end + 2);
            buffer = buffer.slice(end + 2);

            const header = Buffer.from(
                `--ffserver\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.length}\r\n\r\n`
            );

            for (const client of streamClients) {
                client.write(header);
                client.write(frame);
                client.write('\r\n');
            }
        }
    });

    streamProcess.stderr.on('data', () => {});

    streamProcess.on('close', () => {
        streamingActive = false;
        streamProcess = null;
        for (const client of streamClients) {
            if (!client.headersSent) {
                client.status(503).end();
            } else {
                client.end();
            }
        }
        streamClients = [];
    });

    streamProcess.on('error', () => {
        streamingActive = false;
        streamProcess = null;
    });
}

function stopStreaming() {
    if (streamProcess) {
        streamProcess.kill('SIGTERM');
        streamProcess = null;
    }
    streamingActive = false;
    for (const client of streamClients) {
        if (!client.headersSent) {
            client.status(503).end();
        } else {
            client.end();
        }
    }
    streamClients = [];
}

// ======= SERVER START ======= //
const server = http.createServer(app);
server.listen(SERVER_PORT, SERVER_HOST, () => {
    console.log(`Camera HTTP server listening on http://${SERVER_HOST}:${SERVER_PORT}`);
});