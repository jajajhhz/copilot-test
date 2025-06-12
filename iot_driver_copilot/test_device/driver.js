```javascript
// DeviceShifu Driver for "Test device" using HTTP protocol
// Requirements: Runs as an HTTP server, manages EdgeDevice CRD status, loads ConfigMap, proxies device APIs
// Language: JavaScript (Node.js)

const express = require('express');
const axios = require('axios');
const fs = require('fs');
const yaml = require('js-yaml');
const k8s = require('@kubernetes/client-node');
const path = require('path');

// Environment variable validation
const {
    EDGEDEVICE_NAME,
    EDGEDEVICE_NAMESPACE,
    DEVICE_HTTP_ADDRESS,
    SERVER_HOST = '0.0.0.0',
    SERVER_PORT = '8080'
} = process.env;

if (!EDGEDEVICE_NAME || !EDGEDEVICE_NAMESPACE || !DEVICE_HTTP_ADDRESS) {
    console.error('Missing required environment variables: EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE, or DEVICE_HTTP_ADDRESS');
    process.exit(1);
}

// Load API settings from ConfigMap YAML
const CONFIG_PATH = '/etc/edgedevice/config/instructions';
let apiSettings = {};
try {
    if (fs.existsSync(CONFIG_PATH)) {
        const fileContent = fs.readFileSync(CONFIG_PATH, 'utf8');
        apiSettings = yaml.load(fileContent) || {};
    }
} catch (err) {
    console.error('Error reading/parsing config instructions:', err);
}

// Kubernetes API client setup
const kc = new k8s.KubeConfig();
kc.loadFromCluster();
const k8sApiCustomObjects = kc.makeApiClient(k8s.CustomObjectsApi);

// CRD Info
const CRD_GROUP = 'shifu.edgenesis.io';
const CRD_VERSION = 'v1alpha1';
const CRD_PLURAL = 'edgedevices';

// Device Phase Enum
const DevicePhase = {
    Pending: 'Pending',
    Running: 'Running',
    Failed: 'Failed',
    Unknown: 'Unknown'
};

let currentPhase = DevicePhase.Unknown;

// Helper: Update EdgeDevice Status
async function updateEdgeDevicePhase(newPhase) {
    if (currentPhase === newPhase) return;
    currentPhase = newPhase;
    try {
        await k8sApiCustomObjects.patchNamespacedCustomObjectStatus(
            CRD_GROUP,
            CRD_VERSION,
            EDGEDEVICE_NAMESPACE,
            CRD_PLURAL,
            EDGEDEVICE_NAME,
            { status: { edgeDevicePhase: newPhase } },
            undefined,
            undefined,
            undefined,
            {
                headers: { 'Content-Type': 'application/merge-patch+json' }
            }
        );
    } catch (err) {
        // Don't crash, just log
        console.error('Failed to update EdgeDevice phase:', err.body || err);
    }
}

// Helper: Simple connectivity check
async function checkDeviceStatus() {
    try {
        // Try to GET /info from the device (as a health check)
        const url = `${DEVICE_HTTP_ADDRESS.replace(/\/+$/, '')}/info`;
        await axios.get(url, { timeout: 3000 });
        await updateEdgeDevicePhase(DevicePhase.Running);
    } catch (err) {
        await updateEdgeDevicePhase(DevicePhase.Failed);
    }
}

// Periodic status check
setInterval(checkDeviceStatus, 8000);
checkDeviceStatus();

// Express server setup
const app = express();
app.use(express.json());

function getApiSettings(api) {
    return (apiSettings[api] && apiSettings[api].protocolPropertyList) ? apiSettings[api].protocolPropertyList : {};
}

// GET /info endpoint
app.get('/info', async (req, res) => {
    // Proxy GET /info to the device
    try {
        const deviceUrl = `${DEVICE_HTTP_ADDRESS.replace(/\/+$/, '')}/info`;
        const params = req.query || {};
        const deviceResp = await axios.get(deviceUrl, { params, responseType: 'json', timeout: 5000 });

        // Optionally adjust response with settings if needed
        const settings = getApiSettings('info');
        // (For this device, settings are not used further, but available if needed)

        res.status(deviceResp.status).json(deviceResp.data);
        await updateEdgeDevicePhase(DevicePhase.Running);
    } catch (err) {
        await updateEdgeDevicePhase(DevicePhase.Failed);
        if (err.response) {
            res.status(err.response.status).send(err.response.data);
        } else {
            res.status(502).json({ error: 'Device not reachable', details: err.message });
        }
    }
});

// POST /msg endpoint
app.post('/msg', async (req, res) => {
    // Proxy POST /msg to the device
    try {
        const deviceUrl = `${DEVICE_HTTP_ADDRESS.replace(/\/+$/, '')}/msg`;
        const settings = getApiSettings('msg');
        // (For this device, settings are not used further, but available if needed)

        const deviceResp = await axios.post(deviceUrl, req.body, {
            headers: { 'Content-Type': 'application/json' },
            timeout: 5000
        });
        res.status(deviceResp.status).json(deviceResp.data);
        await updateEdgeDevicePhase(DevicePhase.Running);
    } catch (err) {
        await updateEdgeDevicePhase(DevicePhase.Failed);
        if (err.response) {
            res.status(err.response.status).send(err.response.data);
        } else {
            res.status(502).json({ error: 'Device not reachable', details: err.message });
        }
    }
});

// Root endpoint
app.get('/', (req, res) => {
    res.json({
        message: 'DeviceShifu HTTP driver for Test device',
        endpoints: [
            { method: 'GET', path: '/info', description: 'Get device information' },
            { method: 'POST', path: '/msg', description: 'Send command message to device' }
        ]
    });
});

// Start HTTP server
app.listen(Number(SERVER_PORT), SERVER_HOST, () => {
    console.log(`DeviceShifu HTTP driver listening on http://${SERVER_HOST}:${SERVER_PORT}`);
});

// Graceful shutdown
process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT', () => process.exit(0));
```
