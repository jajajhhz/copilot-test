import os
import asyncio
import yaml
import json
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from kubernetes import config as k8s_config
from kubernetes import client as k8s_client
import paho.mqtt.client as mqtt
import uvicorn

# Environment Variables (Required)
EDGEDEVICE_NAME = os.environ["EDGEDEVICE_NAME"]
EDGEDEVICE_NAMESPACE = os.environ["EDGEDEVICE_NAMESPACE"]
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))
MQTT_BROKER_HOST = os.environ["MQTT_BROKER_HOST"]
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_STATUS_TOPIC = os.environ.get("MQTT_STATUS_TOPIC", "robotdog/status")
MQTT_COMMAND_TOPIC = os.environ.get("MQTT_COMMAND_TOPIC", "robotdog/command")
INSTRUCTION_CONFIG_PATH = "/etc/edgedevice/config/instructions"

# DeviceShifu CRD API
CRD_GROUP = "shifu.edgenesis.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "edgedevices"

# Device phase enums
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
PHASE_UNKNOWN = "Unknown"

class CommandReq(BaseModel):
    command: str

# Settings loader
def load_instruction_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}

# Kubernetes CRD status updater
class EdgeDeviceStatusUpdater:
    def __init__(self, name: str, namespace: str):
        try:
            k8s_config.load_incluster_config()
        except Exception:
            # For testing outside k8s
            k8s_config.load_kube_config()
        self.api = k8s_client.CustomObjectsApi()
        self.name = name
        self.namespace = namespace

    def get_edgedevice(self):
        return self.api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=self.namespace,
            plural=CRD_PLURAL,
            name=self.name
        )

    def patch_phase(self, phase: str):
        body = {"status": {"edgeDevicePhase": phase}}
        self.api.patch_namespaced_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=self.namespace,
            plural=CRD_PLURAL,
            name=self.name,
            body=body
        )

    def get_address(self) -> Optional[str]:
        try:
            obj = self.get_edgedevice()
            return obj.get("spec", {}).get("address")
        except Exception:
            return None

# MQTT Handler
class RobotDogMQTTClient:
    def __init__(self, broker_host, broker_port, status_topic, command_topic, username="", password=""):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.status_topic = status_topic
        self.command_topic = command_topic
        self.username = username
        self.password = password
        self.status: Optional[Dict[str, Any]] = None
        self.connected = False
        self._loop = asyncio.get_event_loop()
        self._client = mqtt.Client()
        if self.username:
            self._client.username_pw_set(self.username, self.password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            client.subscribe(self.status_topic)
        else:
            self.connected = False

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
            self.status = json.loads(payload)
        except Exception:
            self.status = None

    def connect(self):
        def loop():
            try:
                self._client.connect(self.broker_host, self.broker_port, 60)
                self._client.loop_forever()
            except Exception:
                self.connected = False
        loop_thread = asyncio.get_event_loop().run_in_executor(None, loop)
        return loop_thread

    def publish_command(self, command: str):
        payload = json.dumps({"command": command})
        self._client.publish(self.command_topic, payload)

    def get_status(self):
        return self.status

# FASTAPI App
app = FastAPI()
instruction_config = load_instruction_config(INSTRUCTION_CONFIG_PATH)
edgedevice_status_updater = EdgeDeviceStatusUpdater(EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE)
robotdog_mqtt_client: Optional[RobotDogMQTTClient] = None

# Supported commands
SUPPORTED_COMMANDS = {"forward", "backward", "start", "stop"}

async def update_device_phase_loop():
    last_phase = None
    while True:
        # Detect actual phase
        phase = PHASE_UNKNOWN
        try:
            address = edgedevice_status_updater.get_address()
            if not address:
                phase = PHASE_UNKNOWN
            elif robotdog_mqtt_client is None:
                phase = PHASE_PENDING
            elif robotdog_mqtt_client.connected:
                phase = PHASE_RUNNING
            else:
                phase = PHASE_FAILED
        except Exception:
            phase = PHASE_UNKNOWN

        if phase != last_phase:
            try:
                edgedevice_status_updater.patch_phase(phase)
            except Exception:
                pass
            last_phase = phase
        await asyncio.sleep(2)

@app.on_event("startup")
async def startup_event():
    global robotdog_mqtt_client
    # Load MQTT settings from config if present
    protocol_settings = instruction_config.get("robotdog", {}).get("protocolPropertyList", {})
    broker_host = protocol_settings.get("broker_host", MQTT_BROKER_HOST)
    broker_port = int(protocol_settings.get("broker_port", MQTT_BROKER_PORT))
    status_topic = protocol_settings.get("status_topic", MQTT_STATUS_TOPIC)
    command_topic = protocol_settings.get("command_topic", MQTT_COMMAND_TOPIC)
    username = protocol_settings.get("username", MQTT_USERNAME)
    password = protocol_settings.get("password", MQTT_PASSWORD)
    robotdog_mqtt_client = RobotDogMQTTClient(
        broker_host=broker_host,
        broker_port=broker_port,
        status_topic=status_topic,
        command_topic=command_topic,
        username=username,
        password=password,
    )
    asyncio.create_task(robotdog_mqtt_client.connect())
    asyncio.create_task(update_device_phase_loop())

@app.get("/robot/status")
async def get_robot_status():
    if robotdog_mqtt_client is None or not robotdog_mqtt_client.connected:
        raise HTTPException(status_code=503, detail="Device not connected")
    status = robotdog_mqtt_client.get_status()
    if status is None:
        return JSONResponse({"status": "unknown"}, status_code=200)
    return JSONResponse(content=status, status_code=200)

@app.post("/robot/command")
async def post_robot_command(cmd: CommandReq):
    if robotdog_mqtt_client is None or not robotdog_mqtt_client.connected:
        raise HTTPException(status_code=503, detail="Device not connected")
    command = cmd.command.lower()
    if command not in SUPPORTED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unsupported command: {command}")
    try:
        robotdog_mqtt_client.publish_command(command)
        return {"result": "sent", "command": command}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))

if __name__ == "__main__":
    uvicorn.run("main:app", host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT)