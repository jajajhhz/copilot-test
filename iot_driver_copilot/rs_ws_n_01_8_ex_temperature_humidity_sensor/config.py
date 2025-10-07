import os

def _get_env_str(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return val


def _get_env_int(name, default=None, required=False):
    val = os.getenv(name)
    if val is None or val == "":
        if required and default is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Invalid int for {name}: {val}")


def _get_env_float(name, default=None, required=False):
    val = os.getenv(name)
    if val is None or val == "":
        if required and default is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return default
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"Invalid float for {name}: {val}")


def _get_env_bool(name, default=False):
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "t", "yes", "y", "on")


class Config:
    def __init__(self):
        # HTTP server configuration
        self.http_host = _get_env_str("HTTP_HOST", "0.0.0.0")
        self.http_port = _get_env_int("HTTP_PORT", 8000)

        # Modbus RTU serial configuration
        self.modbus_port = _get_env_str("MODBUS_PORT", "/dev/ttyUSB0")
        self.modbus_baudrate = _get_env_int("MODBUS_BAUDRATE", 9600)
        self.modbus_parity = _get_env_str("MODBUS_PARITY", "N").upper()
        if self.modbus_parity not in ("N", "E", "O"):
            raise ValueError("MODBUS_PARITY must be one of N, E, O")
        self.modbus_stopbits = _get_env_int("MODBUS_STOPBITS", 1)
        self.modbus_bytesize = _get_env_int("MODBUS_BYTESIZE", 8)
        self.modbus_slave_id = _get_env_int("MODBUS_SLAVE_ID", 1)
        self.modbus_timeout_sec = _get_env_float("MODBUS_TIMEOUT_SEC", 2.0)

        # Polling and resilience
        self.poll_interval_sec = _get_env_float("POLL_INTERVAL_SEC", 2.0)
        self.backoff_initial_sec = _get_env_float("BACKOFF_INITIAL_SEC", 1.0)
        self.backoff_max_sec = _get_env_float("BACKOFF_MAX_SEC", 30.0)

        # Register selection
        self.read_func = _get_env_str("MODBUS_FUNC", "holding").strip().lower()
        if self.read_func not in ("holding", "input"):
            raise ValueError("MODBUS_FUNC must be 'holding' or 'input'")
        self.temp_reg_addr = _get_env_int("TEMP_REG_ADDR", 1)
        self.hum_reg_addr = _get_env_int("HUM_REG_ADDR", 2)
        self.reg_count = _get_env_int("REG_COUNT", 1)
        if self.reg_count not in (1, 2):
            raise ValueError("REG_COUNT must be 1 or 2")

        # Scaling and signedness (set scale to 1.0 to return raw device value)
        self.scale_temp = _get_env_float("SCALE_TEMP", 1.0)
        self.scale_hum = _get_env_float("SCALE_HUM", 1.0)
        self.signed_temp = _get_env_bool("SIGNED_TEMP", True)
        self.signed_hum = _get_env_bool("SIGNED_HUM", False)

    def __repr__(self):
        return (
            f"Config(http_host={self.http_host}, http_port={self.http_port}, "
            f"modbus_port={self.modbus_port}, baudrate={self.modbus_baudrate}, parity={self.modbus_parity}, "
            f"stopbits={self.modbus_stopbits}, bytesize={self.modbus_bytesize}, slave={self.modbus_slave_id}, "
            f"timeout={self.modbus_timeout_sec}, poll_interval={self.poll_interval_sec}, "
            f"backoff_initial={self.backoff_initial_sec}, backoff_max={self.backoff_max_sec}, "
            f"func={self.read_func}, temp_reg={self.temp_reg_addr}, hum_reg={self.hum_reg_addr}, reg_count={self.reg_count}, "
            f"scale_temp={self.scale_temp}, scale_hum={self.scale_hum}, signed_temp={self.signed_temp}, signed_hum={self.signed_hum})"
        )
