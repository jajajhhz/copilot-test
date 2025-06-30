package main

import (
	"bytes"
	"encoding/json"
	"errors"
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

	"gocv.io/x/gocv"
)

type CaptureConfig struct {
	DeviceIndex int
	Format      string // "MJPEG", "YUYV", "H264"
	Width       int
	Height      int
	FPS         int
}

type CameraState struct {
	cap          *gocv.VideoCapture
	capMutex     sync.RWMutex
	isCapturing  bool
	format       string
	width        int
	height       int
	fps          int
	deviceIndex  int
	stopCapture  chan struct{}
	streamClients map[chan []byte]struct{}
	streamMutex  sync.Mutex
}

var (
	state = &CameraState{
		isCapturing:   false,
		format:        "MJPEG",
		width:         640,
		height:        480,
		fps:           30,
		deviceIndex:   0,
		stopCapture:   make(chan struct{}),
		streamClients: make(map[chan []byte]struct{}),
	}
)

func getEnvInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(val)
	if err != nil {
		return defaultVal
	}
	return n
}

func getEnvStr(key, defaultVal string) string {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	return val
}

func openCamera(cfg CaptureConfig) (*gocv.VideoCapture, error) {
	cap, err := gocv.OpenVideoCapture(cfg.DeviceIndex)
	if err != nil {
		return nil, err
	}
	if !cap.IsOpened() {
		return nil, errors.New("could not open camera")
	}
	_ = cap.Set(gocv.VideoCaptureFrameWidth, float64(cfg.Width))
	_ = cap.Set(gocv.VideoCaptureFrameHeight, float64(cfg.Height))
	_ = cap.Set(gocv.VideoCaptureFPS, float64(cfg.FPS))
	return cap, nil
}

func startCapture(cfg CaptureConfig) error {
	state.capMutex.Lock()
	defer state.capMutex.Unlock()

	if state.isCapturing {
		return nil
	}

	cap, err := openCamera(cfg)
	if err != nil {
		return err
	}
	state.cap = cap
	state.isCapturing = true
	state.format = cfg.Format
	state.width = cfg.Width
	state.height = cfg.Height
	state.fps = cfg.FPS
	state.deviceIndex = cfg.DeviceIndex
	state.stopCapture = make(chan struct{})
	go streamBroadcaster()
	return nil
}

func stopCapture() {
	state.capMutex.Lock()
	defer state.capMutex.Unlock()
	if state.isCapturing {
		close(state.stopCapture)
		state.cap.Close()
		state.cap = nil
		state.isCapturing = false
		// Clean up all clients
		state.streamMutex.Lock()
		for ch := range state.streamClients {
			close(ch)
		}
		state.streamClients = make(map[chan []byte]struct{})
		state.streamMutex.Unlock()
	}
}

func streamBroadcaster() {
	fps := state.fps
	interval := time.Second / time.Duration(fps)
	mat := gocv.NewMat()
	defer mat.Close()
	for {
		select {
		case <-state.stopCapture:
			return
		default:
		}
		state.capMutex.RLock()
		if !state.cap.Read(&mat) {
			state.capMutex.RUnlock()
			time.Sleep(interval)
			continue
		}
		state.capMutex.RUnlock()

		buf := new(bytes.Buffer)
		img, err := mat.ToImage()
		if err != nil {
			continue
		}
		err = jpeg.Encode(buf, img, &jpeg.Options{Quality: 80})
		if err != nil {
			continue
		}
		jpegBytes := buf.Bytes()

		// Broadcast frame to all clients
		state.streamMutex.Lock()
		for ch := range state.streamClients {
			select {
			case ch <- jpegBytes:
			default:
			}
		}
		state.streamMutex.Unlock()
		time.Sleep(interval)
	}
}

func parseCaptureConfig(r *http.Request) CaptureConfig {
	format := strings.ToUpper(r.URL.Query().Get("format"))
	if format != "MJPEG" && format != "YUYV" && format != "H264" && format != "" {
		format = "MJPEG"
	}
	if format == "" {
		format = state.format
	}
	width := state.width
	height := state.height
	fps := state.fps
	deviceIndex := state.deviceIndex
	if v := r.URL.Query().Get("width"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			width = n
		}
	}
	if v := r.URL.Query().Get("height"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			height = n
		}
	}
	if v := r.URL.Query().Get("fps"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			fps = n
		}
	}
	if v := r.URL.Query().Get("device"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			deviceIndex = n
		}
	}
	return CaptureConfig{
		DeviceIndex: deviceIndex,
		Format:      format,
		Width:       width,
		Height:      height,
		FPS:         fps,
	}
}

func handleStartCapture(w http.ResponseWriter, r *http.Request) {
	cfg := parseCaptureConfig(r)
	err := startCapture(cfg)
	res := make(map[string]interface{})
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		res["success"] = false
		res["error"] = err.Error()
	} else {
		w.WriteHeader(http.StatusOK)
		res["success"] = true
		res["message"] = "Video capture started"
	}
	json.NewEncoder(w).Encode(res)
}

func handleStopCapture(w http.ResponseWriter, r *http.Request) {
	stopCapture()
	res := map[string]interface{}{
		"success": true,
		"message": "Video capture stopped",
	}
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(res)
}

func getFrameJPEG() ([]byte, error) {
	state.capMutex.RLock()
	defer state.capMutex.RUnlock()
	if !state.isCapturing || state.cap == nil {
		return nil, errors.New("capture not started")
	}
	mat := gocv.NewMat()
	defer mat.Close()
	if !state.cap.Read(&mat) || mat.Empty() {
		return nil, errors.New("unable to read frame")
	}
	img, err := mat.ToImage()
	if err != nil {
		return nil, err
	}
	buf := new(bytes.Buffer)
	err = jpeg.Encode(buf, img, &jpeg.Options{Quality: 80})
	if err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func handleStreamMJPEG(w http.ResponseWriter, r *http.Request) {
	state.capMutex.RLock()
	if !state.isCapturing {
		state.capMutex.RUnlock()
		http.Error(w, "Video not capturing", http.StatusServiceUnavailable)
		return
	}
	state.capMutex.RUnlock()

	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}

	ch := make(chan []byte, 32)
	state.streamMutex.Lock()
	state.streamClients[ch] = struct{}{}
	state.streamMutex.Unlock()
	defer func() {
		state.streamMutex.Lock()
		delete(state.streamClients, ch)
		close(ch)
		state.streamMutex.Unlock()
	}()

	for {
		select {
		case frame, ok := <-ch:
			if !ok {
				return
			}
			_, _ = w.Write([]byte("--frame\r\nContent-Type: image/jpeg\r\n\r\n"))
			_, _ = w.Write(frame)
			_, _ = w.Write([]byte("\r\n"))
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func handleSingleFrame(w http.ResponseWriter, r *http.Request) {
	frame, err := getFrameJPEG()
	if err != nil {
		http.Error(w, "Failed to get frame: "+err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.WriteHeader(http.StatusOK)
	w.Write(frame)
}

// API: /video/stream (GET) - Alias for /stream (GET)
func handleVideoStream(w http.ResponseWriter, r *http.Request) {
	handleStreamMJPEG(w, r)
}

// API: /stream (GET) - MJPEG streaming
func handleStream(w http.ResponseWriter, r *http.Request) {
	handleStreamMJPEG(w, r)
}

// API: /capture/start (POST) - Start capture
func handleCaptureStart(w http.ResponseWriter, r *http.Request) {
	handleStartCapture(w, r)
}

// API: /capture/stop (POST) - Stop capture
func handleCaptureStop(w http.ResponseWriter, r *http.Request) {
	handleStopCapture(w, r)
}

// API: /video/start (POST) - Start video
func handleVideoStart(w http.ResponseWriter, r *http.Request) {
	handleStartCapture(w, r)
}

// API: /video/stop (POST) - Stop video
func handleVideoStop(w http.ResponseWriter, r *http.Request) {
	handleStopCapture(w, r)
}

func main() {
	// Read config from env
	host := getEnvStr("SHIFU_USB_CAMERA_HTTP_HOST", "0.0.0.0")
	port := getEnvStr("SHIFU_USB_CAMERA_HTTP_PORT", "8080")
	state.deviceIndex = getEnvInt("SHIFU_USB_CAMERA_DEVICE_INDEX", 0)
	state.width = getEnvInt("SHIFU_USB_CAMERA_WIDTH", 640)
	state.height = getEnvInt("SHIFU_USB_CAMERA_HEIGHT", 480)
	state.fps = getEnvInt("SHIFU_USB_CAMERA_FPS", 30)
	state.format = strings.ToUpper(getEnvStr("SHIFU_USB_CAMERA_FORMAT", "MJPEG"))

	mux := http.NewServeMux()
	mux.HandleFunc("/video/stream", handleVideoStream)
	mux.HandleFunc("/stream", handleStream)
	mux.HandleFunc("/capture/start", handleCaptureStart)
	mux.HandleFunc("/capture/stop", handleCaptureStop)
	mux.HandleFunc("/video/start", handleVideoStart)
	mux.HandleFunc("/video/stop", handleVideoStop)

	// Optional: single frame endpoint for debugging
	mux.HandleFunc("/frame.jpg", handleSingleFrame)

	srvAddr := host + ":" + port
	log.Printf("USB Camera HTTP Driver running on %s", srvAddr)
	if err := http.ListenAndServe(srvAddr, mux); err != nil {
		log.Fatal(err)
	}
}