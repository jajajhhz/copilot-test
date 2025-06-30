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
	"mime/multipart"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"gocv.io/x/gocv"
)

type Config struct {
	CameraID      int
	HTTPHost      string
	HTTPPort      string
	DefaultWidth  int
	DefaultHeight int
	DefaultFormat string // "MJPEG" or "YUYV" or "H264" (MJPEG supported)
	DefaultFPS    int
}

type CameraServer struct {
	mu            sync.RWMutex
	capture       *gocv.VideoCapture
	isCapturing   bool
	format        string // "MJPEG"
	width         int
	height        int
	fps           int
	frameBuf      *gocv.Mat
	captureCond   *sync.Cond
	stopChan      chan struct{}
	activeStreams int
}

func getenvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}

func getenvStr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getenvCameraConfig() Config {
	return Config{
		CameraID:      getenvInt("CAMERA_ID", 0),
		HTTPHost:      getenvStr("HTTP_HOST", "0.0.0.0"),
		HTTPPort:      getenvStr("HTTP_PORT", "8080"),
		DefaultWidth:  getenvInt("DEFAULT_WIDTH", 640),
		DefaultHeight: getenvInt("DEFAULT_HEIGHT", 480),
		DefaultFormat: strings.ToUpper(getenvStr("DEFAULT_FORMAT", "MJPEG")),
		DefaultFPS:    getenvInt("DEFAULT_FPS", 15),
	}
}

func NewCameraServer(cfg Config) *CameraServer {
	return &CameraServer{
		format:      cfg.DefaultFormat,
		width:       cfg.DefaultWidth,
		height:      cfg.DefaultHeight,
		fps:         cfg.DefaultFPS,
		frameBuf:    nil,
		captureCond: sync.NewCond(&sync.Mutex{}),
		stopChan:    make(chan struct{}),
	}
}

func (cs *CameraServer) StartCapture(format string, width, height, fps int) error {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.isCapturing {
		return nil
	}
	if format == "" {
		format = cs.format
	}
	if width == 0 {
		width = cs.width
	}
	if height == 0 {
		height = cs.height
	}
	if fps == 0 {
		fps = cs.fps
	}
	cap, err := gocv.OpenVideoCapture(cs.getCameraID())
	if err != nil {
		return err
	}
	if !cap.IsOpened() {
		return errors.New("cannot open USB camera")
	}
	cap.Set(gocv.VideoCaptureFrameWidth, float64(width))
	cap.Set(gocv.VideoCaptureFrameHeight, float64(height))
	cap.Set(gocv.VideoCaptureFPS, float64(fps))
	cs.capture = cap
	cs.width = width
	cs.height = height
	cs.fps = fps
	cs.format = format
	cs.isCapturing = true
	cs.stopChan = make(chan struct{})
	go cs.captureLoop()
	return nil
}

func (cs *CameraServer) getCameraID() int {
	return getenvInt("CAMERA_ID", 0)
}

func (cs *CameraServer) StopCapture() {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if !cs.isCapturing {
		return
	}
	close(cs.stopChan)
	cs.isCapturing = false
	if cs.capture != nil {
		cs.capture.Close()
		cs.capture = nil
	}
	cs.captureCond.Broadcast()
}

func (cs *CameraServer) captureLoop() {
	mat := gocv.NewMat()
	defer mat.Close()
	delay := time.Second / time.Duration(cs.fps)
	for {
		select {
		case <-cs.stopChan:
			return
		default:
			if cs.capture == nil || !cs.capture.IsOpened() {
				time.Sleep(100 * time.Millisecond)
				continue
			}
			if ok := cs.capture.Read(&mat); !ok || mat.Empty() {
				time.Sleep(5 * time.Millisecond)
				continue
			}
			cs.captureCond.L.Lock()
			// Store a copy of the current frame
			if cs.frameBuf != nil {
				cs.frameBuf.Close()
			}
			frameCopy := mat.Clone()
			cs.frameBuf = &frameCopy
			cs.captureCond.Broadcast()
			cs.captureCond.L.Unlock()
			time.Sleep(delay)
		}
	}
}

func (cs *CameraServer) getLatestFrame() (*gocv.Mat, error) {
	cs.captureCond.L.Lock()
	defer cs.captureCond.L.Unlock()
	// Wait for frame to be available
	timeout := time.After(2 * time.Second)
	for cs.frameBuf == nil {
		cs.captureCond.Wait()
		select {
		case <-timeout:
			return nil, errors.New("timeout waiting for frame")
		default:
		}
	}
	if cs.frameBuf == nil || cs.frameBuf.Empty() {
		return nil, errors.New("no frame available")
	}
	frame := cs.frameBuf.Clone()
	return &frame, nil
}

func parseQueryInt(r *http.Request, key string, fallback int) int {
	v := r.URL.Query().Get(key)
	if v == "" {
		return fallback
	}
	if i, err := strconv.Atoi(v); err == nil {
		return i
	}
	return fallback
}

func parseQueryStr(r *http.Request, key string, fallback string) string {
	v := r.URL.Query().Get(key)
	if v == "" {
		return fallback
	}
	return v
}

func (cs *CameraServer) handleStartCapture(w http.ResponseWriter, r *http.Request) {
	width := parseQueryInt(r, "width", cs.width)
	height := parseQueryInt(r, "height", cs.height)
	fps := parseQueryInt(r, "fps", cs.fps)
	format := parseQueryStr(r, "format", cs.format)
	err := cs.StartCapture(format, width, height, fps)
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

func (cs *CameraServer) handleStopCapture(w http.ResponseWriter, r *http.Request) {
	cs.StopCapture()
	resp := map[string]interface{}{
		"status":  "stopped",
		"message": "Video capture stopped",
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (cs *CameraServer) handleStreamMJPEG(w http.ResponseWriter, r *http.Request) {
	cs.mu.RLock()
	isCap := cs.isCapturing
	cs.mu.RUnlock()
	if !isCap {
		http.Error(w, "Camera is not capturing", http.StatusConflict)
		return
	}
	boundary := "mjpegstream"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")
	notify := w.(http.CloseNotifier).CloseNotify()

	cs.mu.Lock()
	cs.activeStreams++
	cs.mu.Unlock()

	defer func() {
		cs.mu.Lock()
		cs.activeStreams--
		cs.mu.Unlock()
	}()

	for {
		select {
		case <-notify:
			return
		default:
			frame, err := cs.getLatestFrame()
			if err != nil {
				time.Sleep(10 * time.Millisecond)
				continue
			}
			buf, err := matToJPEG(frame)
			frame.Close()
			if err != nil {
				time.Sleep(10 * time.Millisecond)
				continue
			}
			fmt.Fprintf(w, "--%s\r\n", boundary)
			fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
			fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf.Bytes()))
			w.Write(buf.Bytes())
			fmt.Fprintf(w, "\r\n")
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
			time.Sleep(time.Second / time.Duration(cs.fps))
		}
	}
}

func (cs *CameraServer) handleStream(w http.ResponseWriter, r *http.Request) {
	format := parseQueryStr(r, "format", cs.format)
	switch strings.ToUpper(format) {
	case "MJPEG", "JPEG":
		cs.handleStreamMJPEG(w, r)
	default:
		http.Error(w, "Unsupported stream format. Only MJPEG supported.", http.StatusBadRequest)
	}
}

func (cs *CameraServer) handleVideoStream(w http.ResponseWriter, r *http.Request) {
	cs.handleStream(w, r)
}

func matToJPEG(mat *gocv.Mat) (*bytes.Buffer, error) {
	img, err := mat.ToImage()
	if err != nil {
		return nil, err
	}
	buf := new(bytes.Buffer)
	opt := &jpeg.Options{Quality: 80}
	if err := jpeg.Encode(buf, img, opt); err != nil {
		return nil, err
	}
	return buf, nil
}

func (cs *CameraServer) handleRoot(w http.ResponseWriter, r *http.Request) {
	w.Write([]byte("USB Camera HTTP Driver - Endpoints:\n" +
		"GET /video/stream - MJPEG video stream\n" +
		"GET /stream - MJPEG video stream\n" +
		"POST /video/start - Start video capture\n" +
		"POST /video/stop - Stop video capture\n" +
		"POST /capture/start - Start video capture\n" +
		"POST /capture/stop - Stop video capture\n"))
}

func main() {
	// Make sure we have OpenCV support
	cfg := getenvCameraConfig()
	cs := NewCameraServer(cfg)

	mux := http.NewServeMux()

	mux.HandleFunc("/", cs.handleRoot)
	mux.HandleFunc("/video/stream", cs.handleVideoStream)
	mux.HandleFunc("/stream", cs.handleStream)
	mux.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		cs.handleStartCapture(w, r)
	})
	mux.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		cs.handleStartCapture(w, r)
	})
	mux.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		cs.handleStopCapture(w, r)
	})
	mux.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
			return
		}
		cs.handleStopCapture(w, r)
	})

	addr := fmt.Sprintf("%s:%s", cfg.HTTPHost, cfg.HTTPPort)
	log.Printf("Starting USB Camera HTTP Driver on %s ...", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}