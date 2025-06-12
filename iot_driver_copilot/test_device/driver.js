// DeviceShifu HTTP Driver for "Test device"
// Language: JavaScript (Node.js)
// Protocol: HTTP

const express = require('express');
const k8s = require('@kubernetes/client-node');
const fs = require('fs').promises;
const path = require('path');

// ENV config
const {
    EDGEDEVICE_NAME,
    EDGEDEVICE_NAMESPACE,
    SERVER_HOST = '0.0.0.0',
    SERVER_PORT = '8080'
} = process.env;

// Kubernetes CRD API group/version/kind
const GROUP = 'shifu.edgenesis.io';
const VERSION = 'v1alpha1';
const PLURAL = 'edgedevices';

const EDGEDEVICE_CONFIG_PATH = '/etc/edgedevice/config/instructions';

// Device status phases
const PHASES = {
    PENDING: 'Pending',
    RUNNING: 'Running',
    FAILED: 'Failed',
    UNKNOWN: 'Unknown'
};

let deviceAddress = null;
let protocolSettings = {};

async function loadConfig() {
    try {
        const files = await fs.readdir(EDGEDEVICE_CONFIG_PATH);
        for (const file of files) {
            const content = await fs.readFile(path.join(EDGEDEVICE_CONFIG_PATH, file), 'utf-8');
            const yaml = require('js-yaml');
            const parsed = yaml.load(content);
            protocolSettings = { ...protocolSettings, ...parsed };
        }
    } catch (err) {
        // Ignore missing config directory
    }
}

async function getDeviceAddress(k8sApi) {
    try {
        const res = await k8sApi.getNamespacedCustomObject(
            GROUP, VERSION, EDGEDEVICE_NAMESPACE, PLURAL, EDGEDEVICE_NAME
        );
        deviceAddress = res.body?.spec?.address || null;
        return deviceAddress;
    } catch (err) {
        return null;
    }
}

async function updateDevicePhase(k8sApi, phase) {
    try {
        const patch = [
            {
                op: 'replace',
                path: '/status/edgeDevicePhase',
                value: phase
            }
        ];
        await k8sApi.patchNamespacedCustomObjectStatus(
            GROUP,
            VERSION,
            EDGEDEVICE_NAMESPACE,
            PLURAL,
            EDGEDEVICE_NAME,
            patch,
            undefined,
            undefined,
            undefined,
            { headers: { 'Content-Type': 'application/json-patch+json' } }
        );
    } catch (err) {
        // fallback: try add if not exist
        try {
            const patch = [
                {
                    op: 'add',
                    path: '/status',
                    value: { edgeDevicePhase: phase }
                }
            ];
            await k8sApi.patchNamespacedCustomObjectStatus(
                GROUP,
                VERSION,
                EDGEDEVICE_NAMESPACE,
                PLURAL,
                EDGEDEVICE_NAME,
                patch,
                undefined,
                undefined,
                undefined,
                { headers: { 'Content-Type': 'application/json-patch+json' } }
            );
        } catch (e) {
            // Give up if still fails
        }
    }
}

async function checkDeviceConnection() {
    if (!deviceAddress) return false;
    try {
        const res = await fetch(`http://${deviceAddress}/info`, { method: 'GET', timeout: 2000 });
        return res.ok;
    } catch (err) {
        return false;
    }
}

async function main() {
    // K8s API config
    const kc = new k8s.KubeConfig();
    kc.loadFromCluster();
    const k8sApi = kc.makeApiClient(k8s.CustomObjectsApi);

    // Load API settings
    await loadConfig();

    // Get device address from CRD
    deviceAddress = await getDeviceAddress(k8sApi);

    // Device phase management
    let currentPhase = PHASES.PENDING;
    if (!deviceAddress) {
        currentPhase = PHASES.PENDING;
        await updateDevicePhase(k8sApi, currentPhase);
    } else {
        // Try connect to device
        try {
            const isConnected = await checkDeviceConnection();
            currentPhase = isConnected ? PHASES.RUNNING : PHASES.FAILED;
            await updateDevicePhase(k8sApi, currentPhase);
        } catch {
            currentPhase = PHASES.FAILED;
            await updateDevicePhase(k8sApi, currentPhase);
        }
    }

    // Periodically refresh device status
    setInterval(async () => {
        const addr = await getDeviceAddress(k8sApi);
        if (!addr) {
            if (currentPhase !== PHASES.PENDING) {
                currentPhase = PHASES.PENDING;
                await updateDevicePhase(k8sApi, currentPhase);
            }
            deviceAddress = null;
            return;
        }
        if (addr !== deviceAddress) {
            deviceAddress = addr;
        }
        try {
            const isConnected = await checkDeviceConnection();
            const newPhase = isConnected ? PHASES.RUNNING : PHASES.FAILED;
            if (newPhase !== currentPhase) {
                currentPhase = newPhase;
                await updateDevicePhase(k8sApi, currentPhase);
            }
        } catch {
            if (currentPhase !== PHASES.FAILED) {
                currentPhase = PHASES.FAILED;
                await updateDevicePhase(k8sApi, currentPhase);
            }
        }
    }, 10000);

    // HTTP Server
    const app = express();
    app.use(express.json());

    // GET /info: proxy to device, stream JSON result
    app.get('/info', async (req, res) => {
        if (!deviceAddress) {
            return res.status(503).json({ error: 'Device address not configured.' });
        }
        try {
            // Pass query params
            const url = new URL(`http://${deviceAddress}/info`);
            Object.entries(req.query).forEach(([key, value]) => url.searchParams.append(key, value));
            const fetchRes = await fetch(url, { method: 'GET', timeout: 5000 });
            if (!fetchRes.ok) {
                return res.status(fetchRes.status).json({ error: 'Device responded with error.' });
            }
            res.set('Content-Type', 'application/json');
            fetchRes.body.pipe(res);
        } catch (err) {
            res.status(502).json({ error: 'Failed to fetch device info.' });
        }
    });

    // POST /msg: proxy JSON command to device
    app.post('/msg', async (req, res) => {
        if (!deviceAddress) {
            return res.status(503).json({ error: 'Device address not configured.' });
        }
        try {
            const settings = protocolSettings.msg?.protocolPropertyList || {};
            const url = new URL(`http://${deviceAddress}/msg`);
            Object.entries(settings).forEach(([key, value]) => url.searchParams.append(key, value));
            const fetchRes = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(req.body),
                timeout: 5000
            });
            if (!fetchRes.ok) {
                return res.status(fetchRes.status).json({ error: 'Device command failed.' });
            }
            res.set('Content-Type', 'application/json');
            fetchRes.body.pipe(res);
        } catch (err) {
            res.status(502).json({ error: 'Failed to send message to device.' });
        }
    });

    // Health endpoint
    app.get('/healthz', (_req, res) => {
        res.json({ status: 'ok', phase: currentPhase });
    });

    app.listen(Number(SERVER_PORT), SERVER_HOST, () => {
        // Server running
    });
}

// Polyfill fetch for Node.js (since node-fetch v3 is ESM only, use undici)
const { fetch } = require('undici');
global.fetch = fetch;

main().catch(() => process.exit(1));