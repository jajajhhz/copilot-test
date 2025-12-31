package main

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPHost string
	HTTPPort int

	SerialPort string
	SlaveId    int
	BaudRate   int
	DataBits   int
	Parity     string // "N", "E", "O"
	StopBits   int

	ModbusTimeout   time.Duration
	PollInterval    time.Duration
	BackoffInitial  time.Duration
	BackoffMax      time.Duration

	RegDeviceAddress      uint16
	RegBaudRate           uint16
	RegCommFormat         uint16
	RegWorkMode           uint16
	RegValueType          uint16
	RegDecimals           uint16
	RegDpMask             uint16
	RegBlinkMask          uint16
	RegBlinkPeriodMs      uint16
	RegDisplayValueStart  uint16
	DisplayValueRegs      int
}

func getenv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("missing required env: %s", key)
	}
	return v
}

func getenvInt(key string) int {
	v := getenv(key)
	i, err := strconv.Atoi(v)
	if err != nil {
		log.Fatalf("invalid int for %s: %v", key, err)
	}
	return i
}

func getenvUint16(key string) uint16 {
	v := getenv(key)
	i, err := strconv.Atoi(v)
	if err != nil {
		log.Fatalf("invalid uint16 for %s: %v", key, err)
	}
	if i < 0 || i > 0xFFFF {
		log.Fatalf("out of range uint16 for %s", key)
	}
	return uint16(i)
}

func getenvDurationMs(key string) time.Duration {
	ms := getenvInt(key)
	return time.Duration(ms) * time.Millisecond
}

func LoadConfig() Config {
	cfg := Config{
		HTTPHost: getenv("HTTP_HOST"),
		HTTPPort: getenvInt("HTTP_PORT"),

		SerialPort: getenv("SERIAL_PORT"),
		SlaveId:    getenvInt("SLAVE_ID"),
		BaudRate:   getenvInt("BAUD_RATE"),
		DataBits:   getenvInt("DATA_BITS"),
		Parity:     strings.ToUpper(getenv("PARITY")),
		StopBits:   getenvInt("STOP_BITS"),

		ModbusTimeout:  getenvDurationMs("MODBUS_TIMEOUT_MS"),
		PollInterval:   getenvDurationMs("POLL_INTERVAL_MS"),
		BackoffInitial: getenvDurationMs("BACKOFF_INITIAL_MS"),
		BackoffMax:     getenvDurationMs("BACKOFF_MAX_MS"),

		RegDeviceAddress:     getenvUint16("REG_ADDR_DEVICE_ADDRESS"),
		RegBaudRate:          getenvUint16("REG_ADDR_BAUD_RATE"),
		RegCommFormat:        getenvUint16("REG_ADDR_COMM_FORMAT"),
		RegWorkMode:          getenvUint16("REG_ADDR_WORK_MODE"),
		RegValueType:         getenvUint16("REG_ADDR_VALUE_TYPE"),
		RegDecimals:          getenvUint16("REG_ADDR_DECIMALS"),
		RegDpMask:            getenvUint16("REG_ADDR_DP_MASK"),
		RegBlinkMask:         getenvUint16("REG_ADDR_BLINK_MASK"),
		RegBlinkPeriodMs:     getenvUint16("REG_ADDR_BLINK_PERIOD_MS"),
		RegDisplayValueStart: getenvUint16("REG_ADDR_DISPLAY_VALUE_START"),
		DisplayValueRegs:     getenvInt("REG_DISPLAY_VALUE_REGS"),
	}

	if cfg.Parity != "N" && cfg.Parity != "E" && cfg.Parity != "O" {
		log.Fatalf("invalid PARITY: %s (expected N/E/O)", cfg.Parity)
	}
	if cfg.DataBits < 5 || cfg.DataBits > 8 {
		log.Fatalf("DATA_BITS must be 5..8")
	}
	if cfg.StopBits != 1 && cfg.StopBits != 2 {
		log.Fatalf("STOP_BITS must be 1 or 2")
	}
	if cfg.DisplayValueRegs <= 0 {
		log.Fatalf("REG_DISPLAY_VALUE_REGS must be >0")
	}
	return cfg
}

func (c Config) HTTPAddr() string { return fmt.Sprintf("%s:%d", c.HTTPHost, c.HTTPPort) }