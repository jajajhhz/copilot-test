import logging
import signal
import sys
import threading
import time
from typing import Optional, Tuple

from flask import Flask, Response
from pymodbus.client import ModbusSerialClient

from config import Config


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class DataBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._temp: Optional[float] = None
        self._temp_ts: Optional[float] = None
        self._hum: Optional[float] = None
        self._hum_ts: Optional[float] = None

    def set_temp(self, value: float):
        with self._lock:
            self._temp = value
            self._temp_ts = time.time()

    def set_hum(self, value: float):
        with self._lock:
            self._hum = value
            self._hum_ts = time.time()

    def get_temp(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self._temp, self._temp_ts

    def get_hum(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self._hum, self._hum_ts


class ModbusCollector(threading.Thread):
    def __init__(self, cfg: Config, buffer: DataBuffer):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.buffer = buffer
        self._stop_event = threading.Event()
        self._client = None
        self._backoff_current = self.cfg.backoff_initial_sec

    def _build_client(self) -> ModbusSerialClient:
        client = ModbusSerialClient(
            method='rtu',
            port=self.cfg.modbus_port,
            baudrate=self.cfg.modbus_baudrate,
            parity=self.cfg.modbus_parity,
            stopbits=self.cfg.modbus_stopbits,
            bytesize=self.cfg.modbus_bytesize,
            timeout=self.cfg.modbus_timeout_sec,
        )
        return client

    def _connect(self) -> bool:
        if self._client is None:
            self._client = self._build_client()
        try:
            ok = self._client.connect()
            if ok:
                logger.info("Modbus connected to %s (baud=%s, parity=%s, stop=%s, bytesize=%s)", 
                            self.cfg.modbus_port, self.cfg.modbus_baudrate, self.cfg.modbus_parity,
                            self.cfg.modbus_stopbits, self.cfg.modbus_bytesize)
                self._backoff_current = self.cfg.backoff_initial_sec
                return True
            else:
                logger.warning("Modbus connect() returned False")
                return False
        except Exception as e:
            logger.error("Modbus connect error: %s", e)
            return False

    def _disconnect(self):
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        finally:
            self._client = None
            logger.info("Modbus disconnected")

    def _combine_registers(self, regs, signed=False):
        if len(regs) == 1:
            val = regs[0]
            if signed and val >= 0x8000:
                val -= 0x10000
            return val
        elif len(regs) == 2:
            val = (regs[0] << 16) | regs[1]
            if signed and val >= 0x80000000:
                val -= 0x100000000
            return val
        else:
            # Should not happen with current cfg
            return None

    def _read_registers(self, address: int, count: int, slave: int, func: str):
        if func == 'holding':
            return self._client.read_holding_registers(address=address, count=count, slave=slave)
        else:
            return self._client.read_input_registers(address=address, count=count, slave=slave)

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            # Ensure connection
            if self._client is None:
                if not self._connect():
                    wait_s = min(self._backoff_current, self.cfg.backoff_max_sec)
                    logger.info("Retry connecting in %.2fs", wait_s)
                    self._stop_event.wait(wait_s)
                    self._backoff_current = min(self._backoff_current * 2, self.cfg.backoff_max_sec)
                    continue

            try:
                # Read temperature
                rr_t = self._read_registers(
                    address=self.cfg.temp_reg_addr,
                    count=self.cfg.reg_count,
                    slave=self.cfg.modbus_slave_id,
                    func=self.cfg.read_func,
                )
                if hasattr(rr_t, 'isError') and rr_t.isError():
                    raise IOError(f"Temp read error: {rr_t}")
                if not hasattr(rr_t, 'registers') or rr_t.registers is None:
                    raise IOError("Temp read returned no registers")
                raw_t = self._combine_registers(rr_t.registers, signed=self.cfg.signed_temp)
                if raw_t is None:
                    raise IOError("Temp register combine failed")
                val_t = float(raw_t) * float(self.cfg.scale_temp)
                self.buffer.set_temp(val_t)

                # Read humidity
                rr_h = self._read_registers(
                    address=self.cfg.hum_reg_addr,
                    count=self.cfg.reg_count,
                    slave=self.cfg.modbus_slave_id,
                    func=self.cfg.read_func,
                )
                if hasattr(rr_h, 'isError') and rr_h.isError():
                    raise IOError(f"Humidity read error: {rr_h}")
                if not hasattr(rr_h, 'registers') or rr_h.registers is None:
                    raise IOError("Humidity read returned no registers")
                raw_h = self._combine_registers(rr_h.registers, signed=self.cfg.signed_hum)
                if raw_h is None:
                    raise IOError("Humidity register combine failed")
                val_h = float(raw_h) * float(self.cfg.scale_hum)
                self.buffer.set_hum(val_h)

                logger.debug("Updated readings: temperature=%s, humidity=%s", val_t, val_h)

                # Successful cycle; wait for next poll
                self._stop_event.wait(self.cfg.poll_interval_sec)

            except Exception as e:
                logger.error("Polling error: %s", e)
                # Disconnect and backoff before retry
                self._disconnect()
                wait_s = min(self._backoff_current, self.cfg.backoff_max_sec)
                logger.info("Retry after error in %.2fs", wait_s)
                self._stop_event.wait(wait_s)
                self._backoff_current = min(self._backoff_current * 2, self.cfg.backoff_max_sec)

        # Cleanup on stop
        self._disconnect()
        logger.info("Collector stopped")


def create_app(cfg: Config, buffer: DataBuffer) -> Flask:
    app = Flask(__name__)

    @app.route('/temperature', methods=['GET'])
    def temperature():
        val, ts = buffer.get_temp()
        if val is None:
            return Response("no data\n", status=503, mimetype='text/plain')
        return Response(f"{val}\n", mimetype='text/plain')

    @app.route('/humidity', methods=['GET'])
    def humidity():
        val, ts = buffer.get_hum()
        if val is None:
            return Response("no data\n", status=503, mimetype='text/plain')
        return Response(f"{val}\n", mimetype='text/plain')

    return app


def main():
    cfg = Config()
    logger.info("Starting with %s", cfg)

    buffer = DataBuffer()
    collector = ModbusCollector(cfg, buffer)
    collector.start()

    app = create_app(cfg, buffer)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Signal %s received, shutting down...", signum)
        stop_event.set()
        collector.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run Flask in a thread to allow graceful shutdown
    server_thread = threading.Thread(
        target=lambda: app.run(host=cfg.http_host, port=cfg.http_port, threaded=True, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        collector.join(timeout=5.0)
        logger.info("Driver stopped")


if __name__ == '__main__':
    main()
