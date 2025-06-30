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
	"sync"
	"time"

	"gocv.io/x/gocv"
)

// Configurable via environment variables
var (
	serverHost         = getEnv("SERVER_HOST", "0.0.0.0")
	serverPort         = getEnv("SERVER_PORT", "8080")
	cameraID           = mustGetEnvInt("CAMERA_ID", 0) // usually 0, 1, ...
	defaultFormat      = getEnv("DEFAULT_FORMAT", "mjpeg")
	defaultWidth       = mustGetEnvInt("DEFAULT_WIDTH", 640)
	defaultHeight      = mustGetEnvInt("DEFAULT_HEIGHT", 480)
	defaultFPS         = mustGetEnvInt("DEFAULT_FPS", 30)
	streamBoundary     = "--frame"
	readTimeoutSeconds = mustGetEnvInt("HTTP_READ_TIMEOUT", 60)
)

func getEnv(key, def string) string {
	val := os.Getenv(key)
	if val == "" {
		val = def
	}
	return val
}

func mustGetEnvInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	i, err := strconv.Atoi(v)
	if err != nil {
		log.Fatalf("Invalid integer for %s: %v", key, err)
	}
	return i
}

type CameraConfig struct {
	Format string
	Width  int
	Height int
	FPS    int
}

type CameraState struct {
	mu         sync.Mutex
	running    bool
	cap        *gocv.VideoCapture
	config     CameraConfig
	cond       *sync.Cond
	frame      gocv.Mat
	lastUpdate time.Time
}

func NewCameraState() *CameraState {
	cs := &CameraState{
		running: false,
	}
	cs.cond = sync.NewCond(&cs.mu)
	return cs
}

var cameraState = NewCameraState()

func (cs *CameraState) StartCapture(cfg CameraConfig) error {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	if cs.running {
		return nil // already running
	}

	cap, err := gocv.OpenVideoCapture(cameraID)
	if err != nil {
		return fmt.Errorf("failed to open camera: %w", err)
	}
	if !cap.IsOpened() {
		return errors.New("camera device could not be opened")
	}

	// Set properties
	if cfg.Width > 0 {
		cap.Set(gocv.VideoCaptureFrameWidth, float64(cfg.Width))
	}
	if cfg.Height > 0 {
		cap.Set(gocv.VideoCaptureFrameHeight, float64(cfg.Height))
	}
	if cfg.FPS > 0 {
		cap.Set(gocv.VideoCaptureFPS, float64(cfg.FPS))
	}

	cs.config = cfg
	cs.cap = cap
	cs.running = true
	cs.lastUpdate = time.Now()
	cs.frame = gocv.NewMat()

	go cs.frameGrabber()

	return nil
}

func (cs *CameraState) frameGrabber() {
	for {
		cs.mu.Lock()
		if !cs.running || cs.cap == nil {
			cs.mu.Unlock()
			break
		}
		mat := gocv.NewMat()
		ok := cs.cap.Read(&mat)
		if !ok || mat.Empty() {
			mat.Close()
			cs.mu.Unlock()
			time.Sleep(100 * time.Millisecond)
			continue
		}
		if cs.frame.IsContinuous() {
			cs.frame.Close()
		}
		cs.frame = mat.Clone()
		cs.lastUpdate = time.Now()
		cs.cond.Broadcast()
		mat.Close()
		cs.mu.Unlock()

		time.Sleep(time.Second / time.Duration(cs.config.FPS))
	}
}

func (cs *CameraState) StopCapture() {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if !cs.running {
		return
	}
	cs.running = false
	if cs.cap != nil {
		cs.cap.Close()
		cs.cap = nil
	}
	if cs.frame.IsContinuous() {
		cs.frame.Close()
	}
	cs.cond.Broadcast()
}

func (cs *CameraState) GetFrameJPEG() ([]byte, error) {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if !cs.running || cs.frame.Empty() {
		return nil, errors.New("no frame available")
	}
	img, err := cs.frame.ToImage()
	if err != nil {
		return nil, err
	}
	buf := new(bytes.Buffer)
	err = jpeg.Encode(buf, img, nil)
	if err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// --------- API Handlers ---------

// POST /capture/start and /video/start
func startCaptureHandler(w http.ResponseWriter, r *http.Request) {
	format := r.URL.Query().Get("format")
	if format == "" {
		format = defaultFormat
	}
	width := getIntQuery(r, "width", defaultWidth)
	height := getIntQuery(r, "height", defaultHeight)
	fps := getIntQuery(r, "fps", defaultFPS)

	cfg := CameraConfig{Format: format, Width: width, Height: height, FPS: fps}

	err := cameraState.StartCapture(cfg)
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to start capture: %v", err), http.StatusInternalServerError)
		return
	}
	resp := map[string]interface{}{
		"status":  "started",
		"format":  format,
		"width":   width,
		"height":  height,
		"fps":     fps,
		"message": "Video capture started",
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// POST /capture/stop and /video/stop
func stopCaptureHandler(w http.ResponseWriter, r *http.Request) {
	cameraState.StopCapture()
	resp := map[string]interface{}{
		"status":  "stopped",
		"message": "Video capture stopped",
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// GET /video/stream and /stream
func streamHandler(w http.ResponseWriter, r *http.Request) {
	format := r.URL.Query().Get("format")
	if format == "" {
		format = defaultFormat
	}
	if format != "mjpeg" {
		http.Error(w, "Only MJPEG streaming is supported", http.StatusBadRequest)
		return
	}
	width := getIntQuery(r, "width", defaultWidth)
	height := getIntQuery(r, "height", defaultHeight)
	fps := getIntQuery(r, "fps", defaultFPS)

	err := cameraState.StartCapture(CameraConfig{Format: format, Width: width, Height: height, FPS: fps})
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to start capture: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+streamBoundary)
	w.Header().Set("Connection", "close")
	w.Header().Set("Cache-Control", "no-cache")
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}

	notify := w.(http.CloseNotifier).CloseNotify()
	for {
		select {
		case <-notify:
			return
		default:
			cs := cameraState
			cs.mu.Lock()
			for !cs.running || cs.frame.Empty() {
				cs.cond.Wait()
			}
			img, err := cs.frame.ToImage()
			cs.mu.Unlock()
			if err != nil {
				continue
			}
			buf := new(bytes.Buffer)
			err = jpeg.Encode(buf, img, nil)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "\r\n--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", streamBoundary, buf.Len())
			io.Copy(w, buf)
			flusher.Flush()
			time.Sleep(time.Second / time.Duration(fps))
		}
	}
}

func getIntQuery(r *http.Request, key string, def int) int {
	val := r.URL.Query().Get(key)
	if val == "" {
		return def
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return def
	}
	return i
}

// --------- ROUTER ---------

func main() {
	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		startCaptureHandler(w, r)
	})
	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		startCaptureHandler(w, r)
	})
	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		stopCaptureHandler(w, r)
	})
	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		stopCaptureHandler(w, r)
	})
	http.HandleFunc("/video/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Only GET supported", http.StatusMethodNotAllowed)
			return
		}
		streamHandler(w, r)
	})
	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Only GET supported", http.StatusMethodNotAllowed)
			return
		}
		streamHandler(w, r)
	})

	addr := fmt.Sprintf("%s:%s", serverHost, serverPort)
	srv := &http.Server{
		Addr:         addr,
		ReadTimeout:  time.Duration(readTimeoutSeconds) * time.Second,
		WriteTimeout: time.Duration(readTimeoutSeconds) * time.Second,
	}
	log.Printf("USB Camera HTTP driver listening on %s", addr)
	log.Fatal(srv.ListenAndServe())
}