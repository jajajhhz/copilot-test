import os
import asyncio
import logging
import yaml
import json
import signal
from typing import Dict, Any

import aiohttp
from aiohttp import web
from kubernetes import config, client
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

from asyncio_mqtt import Client as MQTTClient, MqttError

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DeviceShifu-RobotDog")

# Constants for EdgeDevice status phases
PHASE_PENDING = "Pending"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
PHASE_UNKNOWN = "Unknown"

# MQTT message structure
ALLOWED_COMMANDS = {'forward', 'backward', 'start', 'stop'}

# Read environment variables
EDGEDEVICE_NAME = os.environ.get("EDGEDEVICE_NAME")
EDGEDEVICE_NAMESPACE = os.environ.get("EDGEDEVICE_NAMESPACE")
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_COMMAND = os.environ.get("MQTT_TOPIC_COMMAND", "robotdog/command")
MQTT_TOPIC_STATUS = os.environ.get("MQTT_TOPIC_STATUS", "robotdog/status")

HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))

if not EDGEDEVICE_NAME or not EDGEDEVICE_NAMESPACE:
    logger.error("EDGEDEVICE_NAME and EDGEDEVICE_NAMESPACE environment variables are required.")
    exit(1)
if not MQTT_BROKER_HOST:
    logger.error("MQTT_BROKER_HOST environment variable is required.")
    exit(1)

CONFIG_MAP_INSTRUCTION_PATH = "/etc/edgedevice/config/instructions"

# Kubernetes API Setup
def get_k8s_api():
    try:
        config.load_incluster_config()
    except ConfigException:
        logger.error("Not running in-cluster. Exiting.")
        exit(1)
    return client.CustomObjectsApi()

# EdgeDevice CRD interaction
async def update_edgedevice_phase(api, phase: str):
    body = {"status": {"edgeDevicePhase": phase}}
    group = "shifu.edgenesis.io"
    version = "v1alpha1"
    plural = "edgedevices"
    try:
        api.patch_namespaced_custom_object_status(
            group=group,
            version=version,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=plural,
            name=EDGEDEVICE_NAME,
            body=body
        )
        logger.info(f"EdgeDevice status phase set to {phase}")
    except ApiException as e:
        logger.error(f"Failed to update EdgeDevice status: {e}")

async def get_edgedevice(api) -> Dict[str, Any]:
    group = "shifu.edgenesis.io"
    version = "v1alpha1"
    plural = "edgedevices"
    try:
        obj = api.get_namespaced_custom_object(
            group=group,
            version=version,
            namespace=EDGEDEVICE_NAMESPACE,
            plural=plural,
            name=EDGEDEVICE_NAME,
        )
        return obj
    except ApiException as e:
        logger.error(f"Could not fetch EdgeDevice: {e}")
        return {}

# ConfigMap instructions
def load_instruction_settings() -> Dict[str, Any]:
    try:
        with open(CONFIG_MAP_INSTRUCTION_PATH, "r") as f:
            data = yaml.safe_load(f)
            return data if data else {}
    except Exception as e:
        logger.warning(f"Could not load instruction settings: {e}")
        return {}

# DeviceShifu implementation
class RobotDogDeviceShifu:
    def __init__(self):
        self.k8s_api = get_k8s_api()
        self.mqtt_connected = False
        self.status_payload = {"status": "unknown"}
        self.mqtt_client = None
        self.settings = load_instruction_settings()
        self.mqtt_status_task = None
        self.mqtt_command_lock = asyncio.Lock()

    async def update_phase(self, phase: str):
        await update_edgedevice_phase(self.k8s_api, phase)

    async def start_mqtt(self):
        while True:
            try:
                async with MQTTClient(
                    hostname=MQTT_BROKER_HOST,
                    port=MQTT_BROKER_PORT,
                    username=MQTT_USERNAME if MQTT_USERNAME else None,
                    password=MQTT_PASSWORD if MQTT_PASSWORD else None,
                    client_id=f"shifu-robotdog-{EDGEDEVICE_NAME}"
                ) as client:
                    self.mqtt_client = client
                    self.mqtt_connected = True
                    await self.update_phase(PHASE_RUNNING)
                    logger.info("Connected to MQTT broker.")

                    async with client.filtered_messages(MQTT_TOPIC_STATUS) as messages:
                        await client.subscribe(MQTT_TOPIC_STATUS)
                        async for msg in messages:
                            try:
                                self.status_payload = json.loads(msg.payload.decode())
                                logger.info(f"Received status: {self.status_payload}")
                            except Exception as e:
                                logger.warning(f"Invalid status payload: {e}")

            except MqttError as e:
                self.mqtt_connected = False
                await self.update_phase(PHASE_FAILED)
                logger.error(f"MQTT connection error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                self.mqtt_connected = False
                await self.update_phase(PHASE_FAILED)
                logger.error(f"MQTT fatal error: {e}")
                await asyncio.sleep(5)

    async def send_command(self, command: str) -> bool:
        if not self.mqtt_connected or self.mqtt_client is None:
            logger.error("MQTT not connected.")
            return False
        async with self.mqtt_command_lock:
            payload = json.dumps({"command": command})
            try:
                await self.mqtt_client.publish(MQTT_TOPIC_COMMAND, payload)
                logger.info(f"Published command: {payload}")
                return True
            except Exception as e:
                logger.error(f"Failed to publish command: {e}")
                return False

    async def get_status(self) -> Dict[str, Any]:
        # Return last known status
        return self.status_payload

    def get_instruction_settings(self, api_name: str) -> Dict[str, Any]:
        # Parse protocolPropertyList for api_name
        api_settings = self.settings.get(api_name, {})
        return api_settings.get("protocolPropertyList", {})

# HTTP Server
robot_shifu = RobotDogDeviceShifu()
routes = web.RouteTableDef()

@routes.get("/robot/status")
async def get_robot_status(request):
    # Optionally use instruction settings
    _settings = robot_shifu.get_instruction_settings("robot_status")
    status = await robot_shifu.get_status()
    return web.json_response(status)

@routes.post("/robot/command")
async def post_robot_command(request):
    # Optionally use instruction settings
    _settings = robot_shifu.get_instruction_settings("robot_command")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON request body"}, status=400)
    command = body.get("command", "").lower()
    if command not in ALLOWED_COMMANDS:
        return web.json_response({"error": f"Invalid command: {command}"}, status=400)
    success = await robot_shifu.send_command(command)
    if success:
        return web.json_response({"result": "success"})
    else:
        return web.json_response({"error": "Failed to send command"}, status=500)

async def healthz(request):
    return web.Response(text="ok")

routes.get("/healthz")(healthz)

async def start_background_tasks(app):
    # Start MQTT listener
    app['mqtt_task'] = asyncio.create_task(robot_shifu.start_mqtt())

async def cleanup_background_tasks(app):
    app['mqtt_task'].cancel()
    try:
        await app['mqtt_task']
    except asyncio.CancelledError:
        pass

def main():
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    loop = asyncio.get_event_loop()

    # Graceful shutdown
    def shutdown():
        for task in asyncio.Task.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    try:
        web.run_app(app, host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT)
    except Exception as e:
        logger.error(f"HTTP server exited: {e}")

if __name__ == "__main__":
    main()