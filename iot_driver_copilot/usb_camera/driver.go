package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"log"
	"mime"
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
	ServerHost    string
	ServerPort    int
	DefaultWidth  int
	DefaultHeight int
	DefaultFormat string
}

type CameraState struct {
	mu           sync.Mutex
	capturing    bool
	streaming    bool
	videoCapture *gocv.VideoCapture
	format       string
	width        int
	height       int
}

type StartCaptureRequest struct {
	Format string `json:"format"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
}

type ResponseMessage struct {
	Message string `json:"message"`
}

var (
	cfg   Config
	state CameraState
)

const (
	FormatMJPEG = "mjpeg"
	FormatJPEG  = "jpeg"
	FormatYUYV  = "yuyv"
	FormatH264  = "h264" // placeholder, not implemented
)

func getenvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		i, err := strconv.Atoi(v)
		if err == nil {
			return i
		}
	}
	return def
}

func getenvStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func loadConfig() Config {
	return Config{
		CameraID:      getenvInt("CAMERA_ID", 0),
		ServerHost:    getenvStr("SERVER_HOST", "0.0.0.0"),
		ServerPort:    getenvInt("SERVER_PORT", 8080),
		DefaultWidth:  getenvInt("DEFAULT_WIDTH", 640),
		DefaultHeight: getenvInt("DEFAULT_HEIGHT", 480),
		DefaultFormat: getenvStr("DEFAULT_FORMAT", FormatMJPEG),
	}
}

func (s *CameraState) startCapture(format string, width, height int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.capturing {
		return nil
	}
	vcap, err := gocv.OpenVideoCapture(cfg.CameraID)
	if err != nil {
		return err
	}
	if ok := vcap.Set(gocv.VideoCaptureFrameWidth, float64(width)); !ok {
		vcap.Close()
		return errors.New("failed to set width")
	}
	if ok := vcap.Set(gocv.VideoCaptureFrameHeight, float64(height)); !ok {
		vcap.Close()
		return errors.New("failed to set height")
	}
	// Note: Format control is limited, gocv may not support h264/yuyv device negotiation
	s.videoCapture = vcap
	s.capturing = true
	s.format = strings.ToLower(format)
	s.width = width
	s.height = height
	return nil
}

func (s *CameraState) stopCapture() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.capturing {
		return nil
	}
	if s.videoCapture != nil {
		s.videoCapture.Close()
	}
	s.videoCapture = nil
	s.capturing = false
	s.streaming = false
	return nil
}

// /capture/start and /video/start
func captureStartHandler(w http.ResponseWriter, r *http.Request) {
	var req StartCaptureRequest
	if r.Header.Get("Content-Type") == "application/json" {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	format := req.Format
	if format == "" {
		format = r.URL.Query().Get("format")
	}
	if format == "" {
		format = cfg.DefaultFormat
	}
	width := req.Width
	height := req.Height
	if width == 0 {
		width = getenvInt("DEFAULT_WIDTH", cfg.DefaultWidth)
	}
	if height == 0 {
		height = getenvInt("DEFAULT_HEIGHT", cfg.DefaultHeight)
	}
	qw := r.URL.Query().Get("width")
	qh := r.URL.Query().Get("height")
	if qw != "" {
		if v, err := strconv.Atoi(qw); err == nil {
			width = v
		}
	}
	if qh != "" {
		if v, err := strconv.Atoi(qh); err == nil {
			height = v
		}
	}
	if err := state.startCapture(format, width, height); err != nil {
		http.Error(w, fmt.Sprintf("Failed to start capture: %v", err), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(ResponseMessage{Message: "Capture started"})
}

// /capture/stop and /video/stop
func captureStopHandler(w http.ResponseWriter, r *http.Request) {
	if err := state.stopCapture(); err != nil {
		http.Error(w, fmt.Sprintf("Failed to stop capture: %v", err), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(ResponseMessage{Message: "Capture stopped"})
}

// Both /stream and /video/stream
func streamHandler(w http.ResponseWriter, r *http.Request) {
	format := r.URL.Query().Get("format")
	if format == "" {
		format = state.format
	}
	if format == "" {
		format = cfg.DefaultFormat
	}
	width := state.width
	height := state.height
	qw := r.URL.Query().Get("width")
	qh := r.URL.Query().Get("height")
	if qw != "" {
		if v, err := strconv.Atoi(qw); err == nil {
			width = v
		}
	}
	if qh != "" {
		if v, err := strconv.Atoi(qh); err == nil {
			height = v
		}
	}
	// Only MJPEG supported for browser streaming
	format = strings.ToLower(format)
	if format != FormatMJPEG && format != FormatJPEG {
		format = FormatMJPEG
	}
	state.mu.Lock()
	if !state.capturing {
		// Try to start if not already capturing
		if err := state.startCapture(format, width, height); err != nil {
			state.mu.Unlock()
			http.Error(w, fmt.Sprintf("Failed to open camera: %v", err), http.StatusInternalServerError)
			return
		}
	}
	state.streaming = true
	vcap := state.videoCapture
	state.mu.Unlock()

	const boundary = "mjpegboundary"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")

	img := gocv.NewMat()
	defer img.Close()
	ctx := r.Context()
	tick := time.NewTicker(time.Second / 20) // ~20 FPS by default
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			if ok := vcap.Read(&img); !ok || img.Empty() {
				continue
			}
			buf, err := toJPEGBuffer(img)
			if err != nil {
				continue
			}
			mimeType := mime.TypeByExtension(".jpg")
			fmt.Fprintf(w, "--%s\r\n", boundary)
			fmt.Fprintf(w, "Content-Type: %s\r\n", mimeType)
			fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf))
			if _, err := w.Write(buf); err != nil {
				return
			}
			fmt.Fprintf(w, "\r\n")
			flusher, ok := w.(http.Flusher)
			if ok {
				flusher.Flush()
			}
		}
	}
}

func toJPEGBuffer(img gocv.Mat) ([]byte, error) {
	mat, err := img.ToImage()
	if err != nil {
		return nil, err
	}
	var b strings.Builder
	err = jpeg.Encode(&b, mat.(image.Image), &jpeg.Options{Quality: 80})
	if err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

func main() {
	cfg = loadConfig()

	http.HandleFunc("/video/stream", streamHandler)
	http.HandleFunc("/stream", streamHandler)
	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		captureStartHandler(w, r)
	})
	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		captureStartHandler(w, r)
	})
	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		captureStopHandler(w, r)
	})
	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		captureStopHandler(w, r)
	})

	srv := &http.Server{
		Addr:         fmt.Sprintf("%s:%d", cfg.ServerHost, cfg.ServerPort),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 0, // For long-lived stream connections
	}

	log.Printf("USB Camera HTTP server listening at http://%s:%d", cfg.ServerHost, cfg.ServerPort)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("HTTP server error: %v", err)
	}
}