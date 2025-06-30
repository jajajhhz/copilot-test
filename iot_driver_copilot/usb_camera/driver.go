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

	"golang.org/x/net/context"
	"gocv.io/x/gocv"
)

type Config struct {
	ServerHost    string
	ServerPort    string
	DeviceID      int
	DefaultWidth  int
	DefaultHeight int
	DefaultFormat string
	FPS           int
}

type VideoFormat string

const (
	FormatMJPEG VideoFormat = "MJPEG"
	FormatYUYV  VideoFormat = "YUYV"
	FormatH264  VideoFormat = "H264"
)

var (
	config      Config
	streamState = &StreamState{
		mtx:       &sync.RWMutex{},
		running:   false,
		format:    FormatMJPEG,
		width:     640,
		height:    480,
		fps:       15,
		videoChan: make(chan struct{}),
	}
)

type StreamState struct {
	mtx       *sync.RWMutex
	running   bool
	format    VideoFormat
	width     int
	height    int
	fps       int
	videoChan chan struct{}
}

func (s *StreamState) Start(format VideoFormat, width, height, fps int) error {
	s.mtx.Lock()
	defer s.mtx.Unlock()
	if s.running {
		return errors.New("stream already running")
	}
	s.running = true
	s.format = format
	s.width = width
	s.height = height
	s.fps = fps
	s.videoChan = make(chan struct{})
	return nil
}

func (s *StreamState) Stop() {
	s.mtx.Lock()
	defer s.mtx.Unlock()
	if s.running {
		close(s.videoChan)
	}
	s.running = false
}

func (s *StreamState) IsRunning() bool {
	s.mtx.RLock()
	defer s.mtx.RUnlock()
	return s.running
}

func (s *StreamState) GetConfig() (VideoFormat, int, int, int) {
	s.mtx.RLock()
	defer s.mtx.RUnlock()
	return s.format, s.width, s.height, s.fps
}

func getEnvInt(key string, fallback int) int {
	val := os.Getenv(key)
	if val == "" {
		return fallback
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return fallback
	}
	return i
}

func getEnvStr(key, fallback string) string {
	val := os.Getenv(key)
	if val == "" {
		return fallback
	}
	return val
}

func parseFormat(s string) VideoFormat {
	up := strings.ToUpper(s)
	switch up {
	case "MJPEG":
		return FormatMJPEG
	case "YUYV":
		return FormatYUYV
	case "H264":
		return FormatH264
	default:
		return FormatMJPEG
	}
}

func main() {
	config = Config{
		ServerHost:    getEnvStr("SERVER_HOST", "0.0.0.0"),
		ServerPort:    getEnvStr("SERVER_PORT", "8080"),
		DeviceID:      getEnvInt("CAMERA_DEVICE_ID", 0),
		DefaultWidth:  getEnvInt("CAMERA_DEFAULT_WIDTH", 640),
		DefaultHeight: getEnvInt("CAMERA_DEFAULT_HEIGHT", 480),
		DefaultFormat: getEnvStr("CAMERA_DEFAULT_FORMAT", "MJPEG"),
		FPS:           getEnvInt("CAMERA_FPS", 15),
	}
	streamState.width = config.DefaultWidth
	streamState.height = config.DefaultHeight
	streamState.format = parseFormat(config.DefaultFormat)
	streamState.fps = config.FPS

	http.HandleFunc("/video/stream", videoStreamHandler)
	http.HandleFunc("/stream", videoStreamHandler)
	http.HandleFunc("/capture/start", startCaptureHandler)
	http.HandleFunc("/video/start", startCaptureHandler)
	http.HandleFunc("/video/stop", stopCaptureHandler)
	http.HandleFunc("/capture/stop", stopCaptureHandler)

	addr := fmt.Sprintf("%s:%s", config.ServerHost, config.ServerPort)
	log.Printf("Starting USB camera HTTP server at %s ...", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}

func startCaptureHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST method allowed", http.StatusMethodNotAllowed)
		return
	}

	// Parse optional params
	var (
		width, height, fps int
		format             VideoFormat
	)
	width = config.DefaultWidth
	height = config.DefaultHeight
	fps = config.FPS
	format = parseFormat(config.DefaultFormat)

	if r.Header.Get("Content-Type") == "application/json" {
		var req struct {
			Format   string `json:"format"`
			Width    int    `json:"width"`
			Height   int    `json:"height"`
			FPS      int    `json:"fps"`
			Quality  int    `json:"quality"`
			Bitrate  int    `json:"bitrate"`
		}
		json.NewDecoder(r.Body).Decode(&req)
		if req.Format != "" {
			format = parseFormat(req.Format)
		}
		if req.Width > 0 {
			width = req.Width
		}
		if req.Height > 0 {
			height = req.Height
		}
		if req.FPS > 0 {
			fps = req.FPS
		}
	} else {
		r.ParseForm()
		if f := r.FormValue("format"); f != "" {
			format = parseFormat(f)
		}
		if wv := r.FormValue("width"); wv != "" {
			if iv, err := strconv.Atoi(wv); err == nil {
				width = iv
			}
		}
		if hv := r.FormValue("height"); hv != "" {
			if iv, err := strconv.Atoi(hv); err == nil {
				height = iv
			}
		}
		if fpsv := r.FormValue("fps"); fpsv != "" {
			if iv, err := strconv.Atoi(fpsv); err == nil {
				fps = iv
			}
		}
	}

	err := streamState.Start(format, width, height, fps)
	if err != nil {
		http.Error(w, fmt.Sprintf("Capture already started: %v", err), http.StatusConflict)
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

func stopCaptureHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST method allowed", http.StatusMethodNotAllowed)
		return
	}
	streamState.Stop()
	resp := map[string]interface{}{
		"status":  "stopped",
		"message": "Video capture stopped",
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func videoStreamHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Only GET method allowed", http.StatusMethodNotAllowed)
		return
	}

	// Query params for stream config (overrides)
	width := config.DefaultWidth
	height := config.DefaultHeight
	fps := config.FPS
	format := parseFormat(config.DefaultFormat)

	q := r.URL.Query()
	if f := q.Get("format"); f != "" {
		format = parseFormat(f)
	}
	if wv := q.Get("width"); wv != "" {
		if iv, err := strconv.Atoi(wv); err == nil {
			width = iv
		}
	}
	if hv := q.Get("height"); hv != "" {
		if iv, err := strconv.Atoi(hv); err == nil {
			height = iv
		}
	}
	if fpsv := q.Get("fps"); fpsv != "" {
		if iv, err := strconv.Atoi(fpsv); err == nil {
			fps = iv
		}
	}

	ctx := r.Context()
	// If not running, start with requested config
	if !streamState.IsRunning() {
		streamState.Start(format, width, height, fps)
		defer streamState.Stop()
	} else {
		// Use the running config for stream
		format, width, height, fps = streamState.GetConfig()
	}

	switch format {
	case FormatMJPEG:
		serveMJPEGStream(ctx, w, config.DeviceID, width, height, fps)
	default:
		http.Error(w, "Unsupported video format for HTTP stream", http.StatusNotImplemented)
	}
}

func serveMJPEGStream(ctx context.Context, w http.ResponseWriter, deviceID, width, height, fps int) {
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	w.Header().Set("Cache-Control", "no-cache")
	mw := multipart.NewWriter(w)
	boundary := mw.Boundary()
	mw.Close() // only need boundary string

	cam, err := gocv.OpenVideoCapture(deviceID)
	if err != nil {
		http.Error(w, fmt.Sprintf("Cannot open camera: %v", err), http.StatusInternalServerError)
		return
	}
	defer cam.Close()

	cam.Set(gocv.VideoCaptureFrameWidth, float64(width))
	cam.Set(gocv.VideoCaptureFrameHeight, float64(height))
	cam.Set(gocv.VideoCaptureFPS, float64(fps))

	img := gocv.NewMat()
	defer img.Close()

	interval := time.Duration(1e9 / fps)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-streamState.videoChan:
			return
		case <-ticker.C:
			if ok := cam.Read(&img); !ok {
				continue
			}
			if img.Empty() {
				continue
			}
			buf, err := matToJPEG(&img, 80)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "--%s\r\n", boundary)
			fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
			fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf))
			w.Write(buf)
			fmt.Fprintf(w, "\r\n")
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
	}
}

func matToJPEG(mat *gocv.Mat, quality int) ([]byte, error) {
	img, err := mat.ToImage()
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	err = jpeg.Encode(&buf, img, &jpeg.Options{Quality: quality})
	if err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}