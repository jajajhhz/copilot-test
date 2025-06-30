package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/semaphore"
	"gocv.io/x/gocv"
)

// Configuration from environment variables
type Config struct {
	ServerHost   string
	ServerPort   string
	DeviceID     int
	DefaultWidth  int
	DefaultHeight int
	DefaultFPS    int
	DefaultFormat string // "mjpeg" or "yuyv" or "h264" (we'll only support MJPEG for browser compatibility)
}

func getEnvInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	i, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return i
}

func getEnvStr(key string, def string) string {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	return v
}

func LoadConfig() Config {
	return Config{
		ServerHost:    getEnvStr("SERVER_HOST", "0.0.0.0"),
		ServerPort:    getEnvStr("SERVER_PORT", "8080"),
		DeviceID:      getEnvInt("CAMERA_DEVICE_ID", 0),
		DefaultWidth:  getEnvInt("CAMERA_WIDTH", 640),
		DefaultHeight: getEnvInt("CAMERA_HEIGHT", 480),
		DefaultFPS:    getEnvInt("CAMERA_FPS", 15),
		DefaultFormat: strings.ToLower(getEnvStr("CAMERA_FORMAT", "mjpeg")),
	}
}

// CameraManager manages video capture and streaming
type CameraManager struct {
	deviceID int
	width    int
	height   int
	fps      int

	mtx         sync.Mutex
	capture     *gocv.VideoCapture
	running     bool
	format      string
	lastErr     error
	streamCount int
	sem         *semaphore.Weighted
}

func NewCameraManager(cfg Config) *CameraManager {
	return &CameraManager{
		deviceID: cfg.DeviceID,
		width:    cfg.DefaultWidth,
		height:   cfg.DefaultHeight,
		fps:      cfg.DefaultFPS,
		format:   cfg.DefaultFormat,
		sem:      semaphore.NewWeighted(1), // Only one capture at a time
	}
}

func (cm *CameraManager) Start(format string, width, height, fps int) error {
	cm.mtx.Lock()
	defer cm.mtx.Unlock()
	if cm.running {
		return nil // already running
	}

	capture, err := gocv.OpenVideoCapture(cm.deviceID)
	if err != nil {
		cm.lastErr = err
		return err
	}
	// Set properties
	if width > 0 {
		capture.Set(gocv.VideoCaptureFrameWidth, float64(width))
	}
	if height > 0 {
		capture.Set(gocv.VideoCaptureFrameHeight, float64(height))
	}
	if fps > 0 {
		capture.Set(gocv.VideoCaptureFPS, float64(fps))
	}

	// Try to read to ensure device is ready
	mat := gocv.NewMat()
	defer mat.Close()
	if ok := capture.Read(&mat); !ok {
		capture.Close()
		err := errors.New("cannot read from camera")
		cm.lastErr = err
		return err
	}
	cm.capture = capture
	cm.running = true
	cm.format = format
	cm.width = width
	cm.height = height
	cm.fps = fps
	cm.lastErr = nil
	return nil
}

func (cm *CameraManager) Stop() error {
	cm.mtx.Lock()
	defer cm.mtx.Unlock()
	if cm.capture != nil {
		cm.capture.Close()
	}
	cm.capture = nil
	cm.running = false
	cm.lastErr = nil
	return nil
}

func (cm *CameraManager) IsRunning() bool {
	cm.mtx.Lock()
	defer cm.mtx.Unlock()
	return cm.running
}

func (cm *CameraManager) GetLastError() error {
	cm.mtx.Lock()
	defer cm.mtx.Unlock()
	return cm.lastErr
}

// MJPEG streaming
func (cm *CameraManager) StreamMJPEG(w http.ResponseWriter, frameRate int, width, height int) error {
	// Acquire streaming lock (one stream at a time for capture)
	if !cm.sem.TryAcquire(1) {
		http.Error(w, "Camera busy", http.StatusServiceUnavailable)
		return errors.New("camera busy")
	}
	defer cm.sem.Release(1)

	// Set up camera if not already running
	err := cm.Start("mjpeg", width, height, frameRate)
	if err != nil {
		http.Error(w, "Failed to start camera: "+err.Error(), http.StatusInternalServerError)
		return err
	}

	boundary := "mjpegstream"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
	w.Header().Set("Pragma", "no-cache")

	mat := gocv.NewMat()
	defer mat.Close()

	delay := time.Duration(0)
	if frameRate > 0 {
		delay = time.Second / time.Duration(frameRate)
	} else {
		delay = time.Second / 15
	}
	for {
		if cn, ok := w.(http.CloseNotifier); ok {
			select {
			case <-cn.CloseNotify():
				return nil
			default:
			}
		}
		ok := cm.capture.Read(&mat)
		if !ok || mat.Empty() {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		// Encode to JPEG
		buf, err := gocv.IMEncode(".jpg", mat)
		if err != nil {
			continue
		}
		// Write part
		var part bytes.Buffer
		part.WriteString(fmt.Sprintf("\r\n--%s\r\n", boundary))
		part.WriteString("Content-Type: image/jpeg\r\n")
		part.WriteString(fmt.Sprintf("Content-Length: %d\r\n\r\n", len(buf)))
		part.Write(buf)
		buf.Close()
		_, err = w.Write(part.Bytes())
		if err != nil {
			return nil
		}
		// Flush
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		time.Sleep(delay)
	}
}

// API Handlers

type StartStopResponse struct {
	Status  string `json:"status"`
	Message string `json:"message,omitempty"`
}

func parseVideoParams(r *http.Request, defFormat string, defWidth, defHeight, defFPS int) (string, int, int, int) {
	format := defFormat
	width := defWidth
	height := defHeight
	fps := defFPS

	q := r.URL.Query()
	if f := q.Get("format"); f != "" {
		format = strings.ToLower(f)
	}
	if w := q.Get("width"); w != "" {
		if wi, err := strconv.Atoi(w); err == nil && wi > 0 {
			width = wi
		}
	}
	if h := q.Get("height"); h != "" {
		if hi, err := strconv.Atoi(h); err == nil && hi > 0 {
			height = hi
		}
	}
	if f := q.Get("fps"); f != "" {
		if fi, err := strconv.Atoi(f); err == nil && fi > 0 {
			fps = fi
		}
	}
	return format, width, height, fps
}

func handleVideoStream(cm *CameraManager, cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		format, width, height, fps := parseVideoParams(r, cfg.DefaultFormat, cfg.DefaultWidth, cfg.DefaultHeight, cfg.DefaultFPS)
		if format != "mjpeg" {
			http.Error(w, "Only MJPEG format supported for browser streaming", http.StatusBadRequest)
			return
		}
		_ = cm.StreamMJPEG(w, fps, width, height)
	}
}

// /stream (GET) -- identical to /video/stream
func handleStream(cm *CameraManager, cfg Config) http.HandlerFunc {
	return handleVideoStream(cm, cfg)
}

// /capture/start (POST)
func handleCaptureStart(cm *CameraManager, cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		format, width, height, fps := parseVideoParams(r, cfg.DefaultFormat, cfg.DefaultWidth, cfg.DefaultHeight, cfg.DefaultFPS)
		if format != "mjpeg" {
			http.Error(w, "Only MJPEG format supported in this driver", http.StatusBadRequest)
			return
		}
		err := cm.Start(format, width, height, fps)
		resp := StartStopResponse{}
		if err == nil {
			resp.Status = "ok"
			resp.Message = "Video capture started"
			w.WriteHeader(http.StatusOK)
		} else {
			resp.Status = "error"
			resp.Message = err.Error()
			w.WriteHeader(http.StatusInternalServerError)
		}
		json.NewEncoder(w).Encode(resp)
	}
}

// /video/start (POST)
func handleVideoStart(cm *CameraManager, cfg Config) http.HandlerFunc {
	return handleCaptureStart(cm, cfg)
}

// /capture/stop (POST)
func handleCaptureStop(cm *CameraManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		err := cm.Stop()
		resp := StartStopResponse{}
		if err == nil {
			resp.Status = "ok"
			resp.Message = "Video capture stopped"
			w.WriteHeader(http.StatusOK)
		} else {
			resp.Status = "error"
			resp.Message = err.Error()
			w.WriteHeader(http.StatusInternalServerError)
		}
		json.NewEncoder(w).Encode(resp)
	}
}

// /video/stop (POST)
func handleVideoStop(cm *CameraManager) http.HandlerFunc {
	return handleCaptureStop(cm)
}

func main() {
	cfg := LoadConfig()
	cm := NewCameraManager(cfg)

	http.HandleFunc("/video/stream", handleVideoStream(cm, cfg))
	http.HandleFunc("/stream", handleStream(cm, cfg))
	http.HandleFunc("/capture/start", handleCaptureStart(cm, cfg))
	http.HandleFunc("/video/start", handleVideoStart(cm, cfg))
	http.HandleFunc("/capture/stop", handleCaptureStop(cm))
	http.HandleFunc("/video/stop", handleVideoStop(cm))

	addr := fmt.Sprintf("%s:%s", cfg.ServerHost, cfg.ServerPort)
	log.Printf("Starting USB Camera HTTP driver on %s (device: %d)", addr, cfg.DeviceID)
	log.Fatal(http.ListenAndServe(addr, nil))
}