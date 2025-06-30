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

	"gocv.io/x/gocv"
)

type VideoFormat string

const (
	FormatMJPEG VideoFormat = "MJPEG"
	FormatYUYV  VideoFormat = "YUYV"
	FormatH264  VideoFormat = "H264"
)

type CameraConfig struct {
	DeviceID   int
	Width      int
	Height     int
	FPS        int
	VideoFmt   VideoFormat
}

type CameraState struct {
	sync.Mutex
	Capturing   bool
	Streamers   map[chan []byte]struct{}
	Config      CameraConfig
	Capture     *gocv.VideoCapture
	Frame       gocv.Mat
	StopChan    chan struct{}
}

var camState = &CameraState{
	Streamers: make(map[chan []byte]struct{}),
}

func getEnvInt(name string, def int) int {
	s := os.Getenv(name)
	if s == "" {
		return def
	}
	val, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return val
}

func getEnvStr(name string, def string) string {
	s := os.Getenv(name)
	if s == "" {
		return def
	}
	return s
}

func getEnvFormat(name string, def VideoFormat) VideoFormat {
	s := os.Getenv(name)
	if s == "" {
		return def
	}
	switch strings.ToUpper(s) {
	case "MJPEG":
		return FormatMJPEG
	case "YUYV":
		return FormatYUYV
	case "H264":
		return FormatH264
	default:
		return def
	}
}

func loadCameraConfigFromEnv() CameraConfig {
	return CameraConfig{
		DeviceID: getEnvInt("CAMERA_DEVICE_ID", 0),
		Width:    getEnvInt("CAMERA_WIDTH", 640),
		Height:   getEnvInt("CAMERA_HEIGHT", 480),
		FPS:      getEnvInt("CAMERA_FPS", 15),
		VideoFmt: getEnvFormat("CAMERA_VIDEO_FORMAT", FormatMJPEG),
	}
}

func openCamera(cfg CameraConfig) (*gocv.VideoCapture, error) {
	cap, err := gocv.OpenVideoCapture(cfg.DeviceID)
	if err != nil {
		return nil, err
	}
	// Set resolution, fps if possible
	cap.Set(gocv.VideoCaptureFrameWidth, float64(cfg.Width))
	cap.Set(gocv.VideoCaptureFrameHeight, float64(cfg.Height))
	cap.Set(gocv.VideoCaptureFPS, float64(cfg.FPS))
	return cap, nil
}

func startCapture(cfg CameraConfig) error {
	camState.Lock()
	defer camState.Unlock()
	if camState.Capturing {
		return nil
	}
	cap, err := openCamera(cfg)
	if err != nil {
		return err
	}
	mat := gocv.NewMat()
	camState.Capture = cap
	camState.Frame = mat
	camState.Capturing = true
	camState.Config = cfg
	camState.StopChan = make(chan struct{})
	go captureLoop()
	return nil
}

func stopCapture() {
	camState.Lock()
	defer camState.Unlock()
	if !camState.Capturing {
		return
	}
	close(camState.StopChan)
	time.Sleep(50 * time.Millisecond)
	camState.Capturing = false
	if camState.Capture != nil {
		camState.Capture.Close()
		camState.Capture = nil
	}
	if camState.Frame.IsContinuous() {
		camState.Frame.Close()
	}
}

func captureLoop() {
	for {
		camState.Lock()
		if !camState.Capturing || camState.Capture == nil {
			camState.Unlock()
			return
		}
		cap := camState.Capture
		frame := &camState.Frame
		camState.Unlock()

		if ok := cap.Read(frame); !ok || frame.Empty() {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		// Send frame to all streamers
		buf, err := matToJPEG(*frame)
		if err == nil {
			camState.Lock()
			for ch := range camState.Streamers {
				select {
				case ch <- buf:
				default:
				}
			}
			camState.Unlock()
		}

		select {
		case <-camState.StopChan:
			return
		default:
		}
	}
}

func matToJPEG(mat gocv.Mat) ([]byte, error) {
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

func withCapture(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		camState.Lock()
		active := camState.Capturing
		camState.Unlock()
		if !active {
			http.Error(w, "Camera not capturing", http.StatusServiceUnavailable)
			return
		}
		next(w, r)
	}
}

func parseConfigFromRequest(r *http.Request) CameraConfig {
	cfg := loadCameraConfigFromEnv()
	q := r.URL.Query()
	if deviceID := q.Get("device_id"); deviceID != "" {
		if id, err := strconv.Atoi(deviceID); err == nil {
			cfg.DeviceID = id
		}
	}
	if width := q.Get("width"); width != "" {
		if w, err := strconv.Atoi(width); err == nil {
			cfg.Width = w
		}
	}
	if height := q.Get("height"); height != "" {
		if h, err := strconv.Atoi(height); err == nil {
			cfg.Height = h
		}
	}
	if fps := q.Get("fps"); fps != "" {
		if f, err := strconv.Atoi(fps); err == nil {
			cfg.FPS = f
		}
	}
	if fmt := q.Get("format"); fmt != "" {
		cfg.VideoFmt = getEnvFormat("DUMMY", VideoFormat(strings.ToUpper(fmt)))
	}
	return cfg
}

// POST /capture/start or /video/start
func StartCaptureHandler(w http.ResponseWriter, r *http.Request) {
	var req struct {
		DeviceID int         `json:"device_id"`
		Width    int         `json:"width"`
		Height   int         `json:"height"`
		FPS      int         `json:"fps"`
		Format   VideoFormat `json:"format"`
	}
	if r.Header.Get("Content-Type") == "application/json" {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	cfg := loadCameraConfigFromEnv()
	if req.DeviceID != 0 {
		cfg.DeviceID = req.DeviceID
	}
	if req.Width != 0 {
		cfg.Width = req.Width
	}
	if req.Height != 0 {
		cfg.Height = req.Height
	}
	if req.FPS != 0 {
		cfg.FPS = req.FPS
	}
	if req.Format != "" {
		cfg.VideoFmt = req.Format
	}
	qcfg := parseConfigFromRequest(r)
	cfg.DeviceID = qcfg.DeviceID
	cfg.Width = qcfg.Width
	cfg.Height = qcfg.Height
	cfg.FPS = qcfg.FPS
	cfg.VideoFmt = qcfg.VideoFmt

	err := startCapture(cfg)
	resp := make(map[string]interface{})
	if err != nil {
		resp["status"] = "error"
		resp["error"] = err.Error()
		w.WriteHeader(http.StatusInternalServerError)
	} else {
		resp["status"] = "ok"
		resp["config"] = cfg
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

// POST /capture/stop or /video/stop
func StopCaptureHandler(w http.ResponseWriter, r *http.Request) {
	stopCapture()
	resp := map[string]interface{}{
		"status": "ok",
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

// GET /video/stream and /stream
func StreamHandler(w http.ResponseWriter, r *http.Request) {
	format := r.URL.Query().Get("format")
	if format == "" {
		format = string(camState.Config.VideoFmt)
	}
	format = strings.ToUpper(format)
	switch format {
	case "MJPEG":
		streamMJPEG(w, r)
	default:
		http.Error(w, "Only MJPEG format supported in HTTP stream", http.StatusNotImplemented)
	}
}

func streamMJPEG(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming not supported", http.StatusInternalServerError)
		return
	}
	frameCh := make(chan []byte, 1)
	camState.Lock()
	camState.Streamers[frameCh] = struct{}{}
	camState.Unlock()
	defer func() {
		camState.Lock()
		delete(camState.Streamers, frameCh)
		camState.Unlock()
		close(frameCh)
	}()
	timeout := time.Duration(getEnvInt("STREAM_CLIENT_TIMEOUT_SEC", 0))
	if timeout == 0 {
		timeout = 0
	} else {
		timeout = timeout * time.Second
	}
	ticker := time.NewTicker(time.Second / time.Duration(camState.Config.FPS))
	defer ticker.Stop()
	for {
		select {
		case buf, ok := <-frameCh:
			if !ok {
				return
			}
			fmt.Fprintf(w, "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", len(buf))
			_, _ = w.Write(buf)
			_, _ = w.Write([]byte("\r\n"))
			flusher.Flush()
		case <-r.Context().Done():
			return
		case <-ticker.C:
		case <-time.After(timeout):
			return
		}
	}
}

func main() {
	host := getEnvStr("HTTP_SERVER_HOST", "")
	port := getEnvStr("HTTP_SERVER_PORT", "8080")

	http.HandleFunc("/capture/start", StartCaptureHandler)
	http.HandleFunc("/video/start", StartCaptureHandler)

	http.HandleFunc("/capture/stop", StopCaptureHandler)
	http.HandleFunc("/video/stop", StopCaptureHandler)

	http.HandleFunc("/video/stream", withCapture(StreamHandler))
	http.HandleFunc("/stream", withCapture(StreamHandler))

	addr := fmt.Sprintf("%s:%s", host, port)
	log.Printf("USB Camera HTTP driver starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}