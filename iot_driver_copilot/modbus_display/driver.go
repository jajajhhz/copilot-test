package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/goburrow/modbus"
)

type DeviceStatus struct {
	DeviceAddress  int    `json:"device_address"`
	BaudRate       int    `json:"baud_rate"`
	CommFormat     string `json:"comm_format"`
	WorkMode       uint16 `json:"work_mode"`
	DisplayValue   string `json:"display_value"`
	ValueType      uint16 `json:"value_type"`
	Decimals       uint16 `json:"decimals"`
	DpMask         uint16 `json:"dp_mask"`
	BlinkMask      uint16 `json:"blink_mask"`
	BlinkPeriodMs  uint16 `json:"blink_period_ms"`
	lastUpdateTime time.Time `json:"-"`
}

type ModbusDriver struct {
	cfg      Config
	logger   *log.Logger

	handler  *modbus.RTUClientHandler
	client   modbus.Client

	mbusMu   sync.Mutex     // serialize modbus ops
	statusMu sync.RWMutex   // guard status
	status   DeviceStatus
}

func NewModbusDriver(cfg Config) *ModbusDriver {
	logger := log.New(os.Stdout, "[modbus-display] ", log.LstdFlags|log.Lmicroseconds)
	return &ModbusDriver{cfg: cfg, logger: logger}
}

func (d *ModbusDriver) buildHandler() *modbus.RTUClientHandler {
	h := modbus.NewRTUClientHandler(d.cfg.SerialPort)
	h.BaudRate = d.cfg.BaudRate
	h.DataBits = d.cfg.DataBits
	h.Parity = d.cfg.Parity
	h.StopBits = d.cfg.StopBits
	h.SlaveId = byte(d.cfg.SlaveId)
	h.Timeout = d.cfg.ModbusTimeout
	return h
}

func (d *ModbusDriver) ensureConnected(ctx context.Context) error {
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.handler == nil {
		d.handler = d.buildHandler()
	}
	// Connect if not connected
	if err := d.handler.Connect(); err != nil {
		return err
	}
	d.client = modbus.NewClient(d.handler)
	return nil
}

func (d *ModbusDriver) closeConn() {
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.handler != nil {
		_ = d.handler.Close()
	}
}

func (d *ModbusDriver) readU16(addr uint16) (uint16, error) {
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.client == nil {
		return 0, errors.New("modbus client not connected")
	}
	b, err := d.client.ReadHoldingRegisters(addr, 1)
	if err != nil {
		return 0, err
	}
	if len(b) < 2 {
		return 0, errors.New("short read")
	}
	return binary.BigEndian.Uint16(b), nil
}

func (d *ModbusDriver) readRegs(addr uint16, qty uint16) ([]byte, error) {
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.client == nil {
		return nil, errors.New("modbus client not connected")
	}
	b, err := d.client.ReadHoldingRegisters(addr, qty)
	if err != nil {
		return nil, err
	}
	return b, nil
}

func (d *ModbusDriver) writeU16(addr uint16, val uint16) error {
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.client == nil {
		return errors.New("modbus client not connected")
	}
	_, err := d.client.WriteSingleRegister(addr, val)
	return err
}

func (d *ModbusDriver) writeRegs(addr uint16, qty uint16, payload []byte) error {
	if int(qty)*2 != len(payload) {
		return fmt.Errorf("payload length mismatch: need %d bytes", int(qty)*2)
	}
	d.mbusMu.Lock()
	defer d.mbusMu.Unlock()
	if d.client == nil {
		return errors.New("modbus client not connected")
	}
	_, err := d.client.WriteMultipleRegisters(addr, qty, payload)
	return err
}

func (d *ModbusDriver) decodeCommFormat(code uint16) string {
	// Map simple codes to common formats
	switch code {
	case 0:
		return "8N1"
	case 1:
		return "8E1"
	case 2:
		return "8O1"
	case 3:
		return "8N2"
	case 4:
		return "8E2"
	case 5:
		return "8O2"
	default:
		return fmt.Sprintf("code:%d", code)
	}
}

func (d *ModbusDriver) encodeCommFormatStr(s string) uint16 {
	s = strings.ToUpper(strings.TrimSpace(s))
	switch s {
	case "8N1":
		return 0
	case "8E1":
		return 1
	case "8O1":
		return 2
	case "8N2":
		return 3
	case "8E2":
		return 4
	case "8O2":
		return 5
	default:
		return 0
	}
}

func (d *ModbusDriver) applyLocalSerialFromCommFormat(s string) {
	// Update local handler serial parameters to match comm_format string
	cf := strings.ToUpper(strings.TrimSpace(s))
	var dataBits, stopBits int
	var parity string
	// Parse like "8N1"
	if len(cf) == 3 || len(cf) == 4 {
		// handle 8N1 or 8N2
		dataBits = int(cf[0] - '0')
		parity = string(cf[1])
		stopBits = int(cf[2] - '0')
		if len(cf) == 4 { // e.g., 8N10? not expected
			stopBits = int(cf[3] - '0')
		}
	}
	if dataBits >= 5 && dataBits <= 8 && (parity == "N" || parity == "E" || parity == "O") && (stopBits == 1 || stopBits == 2) {
		// Update config
		d.cfg.DataBits = dataBits
		d.cfg.Parity = parity
		d.cfg.StopBits = stopBits
		if d.handler != nil {
			d.handler.DataBits = dataBits
			d.handler.Parity = parity
			d.handler.StopBits = stopBits
		}
	}
}

func (d *ModbusDriver) encodeAsciiToRegs(s string, regs int) []byte {
	bs := []byte(s)
	maxBytes := regs * 2
	buf := make([]byte, maxBytes)
	// fill spaces
	for i := range buf {
		buf[i] = 0x20
	}
	copy(buf, bs[:min(len(bs), maxBytes)])
	// Convert big-endian pairs to register payload
	return buf
}

func (d *ModbusDriver) decodeAsciiFromRegs(b []byte) string {
	// b length is 2 * regs
	// Trim trailing spaces and nulls
	out := make([]byte, len(b))
	copy(out, b)
	// Convert directly to string of bytes (big-endian pairs already represent ASCII chars)
	// We interpret each byte as a character in sequence
	// Remove trailing spaces (0x20) and zeros
	trimIdx := len(out)
	for i := len(out) - 1; i >= 0; i-- {
		if out[i] == 0x00 || out[i] == 0x20 {
			trimIdx = i
		} else {
			break
		}
	}
	return string(out[:trimIdx])
}

func min(a, b int) int { if a < b { return a } ; return b }

func (d *ModbusDriver) pollLoop(ctx context.Context) {
	backoff := d.cfg.BackoffInitial
	for {
		if ctx.Err() != nil { return }
		if err := d.ensureConnected(ctx); err != nil {
			d.logger.Printf("connect failed: %v; retry in %v", err, backoff)
			select {
			case <-time.After(backoff):
				backoff *= 2
				if backoff > d.cfg.BackoffMax { backoff = d.cfg.BackoffMax }
				continue
			case <-ctx.Done():
				return
			}
		}
		// Connected: read status
		if err := d.readAndUpdateStatus(); err != nil {
			d.logger.Printf("poll error: %v", err)
			// Close and backoff
			d.closeConn()
			select {
			case <-time.After(backoff):
				backoff *= 2
				if backoff > d.cfg.BackoffMax { backoff = d.cfg.BackoffMax }
				continue
			case <-ctx.Done():
				return
			}
		}
		backoff = d.cfg.BackoffInitial
		// sleep until next poll
		select {
		case <-time.After(d.cfg.PollInterval):
			continue
		case <-ctx.Done():
			return
		}
	}
}

func (d *ModbusDriver) readAndUpdateStatus() error {
	// Read core config
	var err error
	st := DeviceStatus{}
	// These reads are independent; errors should abort to trigger reconnect
	if v, e := d.readU16(d.cfg.RegDeviceAddress); e == nil { st.DeviceAddress = int(v) } else { err = e }
	if v, e := d.readU16(d.cfg.RegBaudRate); e == nil { st.BaudRate = int(v) } else { err = e }
	if v, e := d.readU16(d.cfg.RegCommFormat); e == nil { st.CommFormat = d.decodeCommFormat(v) } else { err = e }
	if v, e := d.readU16(d.cfg.RegWorkMode); e == nil { st.WorkMode = v } else { err = e }
	if v, e := d.readU16(d.cfg.RegValueType); e == nil { st.ValueType = v } else { err = e }
	if v, e := d.readU16(d.cfg.RegDecimals); e == nil { st.Decimals = v } else { err = e }
	if v, e := d.readU16(d.cfg.RegDpMask); e == nil { st.DpMask = v } else { err = e }
	if v, e := d.readU16(d.cfg.RegBlinkMask); e == nil { st.BlinkMask = v } else { err = e }
	if v, e := d.readU16(d.cfg.RegBlinkPeriodMs); e == nil { st.BlinkPeriodMs = v } else { err = e }
	// display value registers
	regQty := uint16(d.cfg.DisplayValueRegs)
	if b, e := d.readRegs(d.cfg.RegDisplayValueStart, regQty); e == nil {
		st.DisplayValue = d.decodeAsciiFromRegs(b)
	} else { err = e }

	if err != nil {
		return err
	}
	st.lastUpdateTime = time.Now()
	// Update state
	d.statusMu.Lock()
	d.status = st
	d.statusMu.Unlock()
	// Reflect into runtime config for slave id/baud/format if changed
	if d.cfg.SlaveId != st.DeviceAddress || d.cfg.BaudRate != st.BaudRate || d.cfg.CommFormatString() != st.CommFormat {
		// Update runtime configuration (no write to device here; we are reading device's current settings)
		d.cfg.SlaveId = st.DeviceAddress
		d.cfg.BaudRate = st.BaudRate
		d.applyLocalSerialFromCommFormat(st.CommFormat)
		if d.handler != nil {
			d.handler.SlaveId = byte(st.DeviceAddress)
			d.handler.BaudRate = st.BaudRate
		}
	}
	return nil
}

func (c Config) CommFormatString() string {
	// Derive from DataBits/Parity/StopBits
	return fmt.Sprintf("%d%s%d", c.DataBits, c.Parity, c.StopBits)
}

// HTTP Handlers
func (d *ModbusDriver) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	d.statusMu.RLock()
	st := d.status
	d.statusMu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(st)
}

type commConfigReq struct {
	DeviceAddress *int   `json:"device_address"`
	BaudRate      *int   `json:"baud_rate"`
	CommFormat    *string `json:"comm_format"`
}

func (d *ModbusDriver) handleCommConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	var req commConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil { http.Error(w, "invalid json", http.StatusBadRequest); return }
	// Apply in safe order: comm_format -> baud_rate -> device_address
	// Write to device registers then update local handler
	if req.CommFormat != nil {
		code := d.encodeCommFormatStr(*req.CommFormat)
		if err := d.writeU16(d.cfg.RegCommFormat, code); err != nil {
			d.logger.Printf("write comm_format failed: %v", err)
			http.Error(w, "device write error", http.StatusInternalServerError); return
		}
		// Update local serial params
		d.applyLocalSerialFromCommFormat(*req.CommFormat)
	}
	if req.BaudRate != nil {
		if *req.BaudRate <= 0 { http.Error(w, "invalid baud_rate", http.StatusBadRequest); return }
		if err := d.writeU16(d.cfg.RegBaudRate, uint16(*req.BaudRate)); err != nil {
			d.logger.Printf("write baud_rate failed: %v", err)
			http.Error(w, "device write error", http.StatusInternalServerError); return
		}
		if d.handler != nil { d.handler.BaudRate = *req.BaudRate }
	}
	if req.DeviceAddress != nil {
		if *req.DeviceAddress < 1 || *req.DeviceAddress > 247 { http.Error(w, "invalid device_address", http.StatusBadRequest); return }
		if err := d.writeU16(d.cfg.RegDeviceAddress, uint16(*req.DeviceAddress)); err != nil {
			d.logger.Printf("write device_address failed: %v", err)
			http.Error(w, "device write error", http.StatusInternalServerError); return
		}
		if d.handler != nil { d.handler.SlaveId = byte(*req.DeviceAddress) }
	}
	// Update status cache
	d.statusMu.Lock()
	if req.DeviceAddress != nil { d.status.DeviceAddress = *req.DeviceAddress }
	if req.BaudRate != nil { d.status.BaudRate = *req.BaudRate }
	if req.CommFormat != nil { d.status.CommFormat = *req.CommFormat }
	d.statusMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

type displayConfigReq struct {
	ValueType *uint16 `json:"value_type"`
	Decimals  *uint16 `json:"decimals"`
	WorkMode  *uint16 `json:"work_mode"`
}

func (d *ModbusDriver) handleDisplayConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	var req displayConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil { http.Error(w, "invalid json", http.StatusBadRequest); return }
	if req.ValueType != nil {
		if err := d.writeU16(d.cfg.RegValueType, *req.ValueType); err != nil { d.logger.Printf("write value_type failed: %v", err); http.Error(w, "device write error", http.StatusInternalServerError); return }
	}
	if req.Decimals != nil {
		if err := d.writeU16(d.cfg.RegDecimals, *req.Decimals); err != nil { d.logger.Printf("write decimals failed: %v", err); http.Error(w, "device write error", http.StatusInternalServerError); return }
	}
	if req.WorkMode != nil {
		if err := d.writeU16(d.cfg.RegWorkMode, *req.WorkMode); err != nil { d.logger.Printf("write work_mode failed: %v", err); http.Error(w, "device write error", http.StatusInternalServerError); return }
	}
	// Update cache
	d.statusMu.Lock()
	if req.ValueType != nil { d.status.ValueType = *req.ValueType }
	if req.Decimals != nil { d.status.Decimals = *req.Decimals }
	if req.WorkMode != nil { d.status.WorkMode = *req.WorkMode }
	d.statusMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

type displayValueReq struct {
	DisplayValue string `json:"display_value"`
}

func (d *ModbusDriver) handleDisplayValue(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	var req displayValueReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil { http.Error(w, "invalid json", http.StatusBadRequest); return }
	val := strings.TrimSpace(req.DisplayValue)
	if val == "" { http.Error(w, "display_value required", http.StatusBadRequest); return }
	payload := d.encodeAsciiToRegs(val, d.cfg.DisplayValueRegs)
	qty := uint16(d.cfg.DisplayValueRegs)
	if err := d.writeRegs(d.cfg.RegDisplayValueStart, qty, payload); err != nil {
		d.logger.Printf("write display_value failed: %v", err)
		http.Error(w, "device write error", http.StatusInternalServerError); return
	}
	// Update cache
	d.statusMu.Lock(); d.status.DisplayValue = val; d.statusMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

type blinkPeriodReq struct {
	BlinkPeriodMs *uint16 `json:"blink_period_ms"`
}

func (d *ModbusDriver) handleBlinkPeriod(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	var req blinkPeriodReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil { http.Error(w, "invalid json", http.StatusBadRequest); return }
	if req.BlinkPeriodMs == nil { http.Error(w, "blink_period_ms required", http.StatusBadRequest); return }
	if err := d.writeU16(d.cfg.RegBlinkPeriodMs, *req.BlinkPeriodMs); err != nil {
		d.logger.Printf("write blink_period_ms failed: %v", err)
		http.Error(w, "device write error", http.StatusInternalServerError); return
	}
	// Update cache
	d.statusMu.Lock(); d.status.BlinkPeriodMs = *req.BlinkPeriodMs; d.statusMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

func (d *ModbusDriver) runHTTP(ctx context.Context) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", d.handleStatus)
	mux.HandleFunc("/blink/period", d.handleBlinkPeriod)
	mux.HandleFunc("/display/config", d.handleDisplayConfig)
	mux.HandleFunc("/display/value", d.handleDisplayValue)
	mux.HandleFunc("/comm/config", d.handleCommConfig)

	srv := &http.Server{ Addr: d.cfg.HTTPAddr(), Handler: mux }
	go func() {
		d.logger.Printf("HTTP server listening on %s", d.cfg.HTTPAddr())
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			d.logger.Printf("http server error: %v", err)
		}
	}()
	go func() {
		<-ctx.Done()
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
	}()
	return srv
}

func main() {
	cfg := LoadConfig()
	drv := NewModbusDriver(cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start HTTP
	_ = drv.runHTTP(ctx)

	// Start poller
	go drv.pollLoop(ctx)

	// Handle shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigCh
	drv.logger.Printf("signal received: %v; shutting down", sig)
	cancel()
	// allow background to finish
	time.Sleep(1 * time.Second)
	drv.closeConn()
	drv.logger.Printf("shutdown complete")
}
