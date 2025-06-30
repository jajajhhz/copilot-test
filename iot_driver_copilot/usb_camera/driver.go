package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"image/jpeg"
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

type VideoFormat string

const (
	FormatMJPEG VideoFormat = "mjpeg"
	FormatYUYV  VideoFormat = "yuyv"
	FormatH264  VideoFormat = "h264"
)

type CaptureConfig struct {
	DeviceID   int
	Width      int
	Height     int
	FPS        int
	Format     VideoFormat
}

type CameraState struct {
	sync.Mutex
	Capture     *gocv.VideoCapture
	Config      CaptureConfig
	IsCapturing bool
}

var cameraState = &CameraState{}
var serverHost string
var serverPort string
var deviceID int
var defaultWidth int
var defaultHeight int
var defaultFPS int
var defaultFormat VideoFormat

func getEnvInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return defaultVal
	}
	return i
}

func getEnvStr(key, defaultVal string) string {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	return val
}

func getEnvFormat(key string, defaultVal VideoFormat) VideoFormat {
	val := strings.ToLower(os.Getenv(key))
	switch val {
	case "mjpeg":
		return FormatMJPEG
	case "yuyv":
		return FormatYUYV
	case "h264":
		return FormatH264
	default:
		return defaultVal
	}
}

func initConfig() {
	serverHost = getEnvStr("SHIFU_USB_CAMERA_SERVER_HOST", "0.0.0.0")
	serverPort = getEnvStr("SHIFU_USB_CAMERA_SERVER_PORT", "8080")
	deviceID = getEnvInt("SHIFU_USB_CAMERA_DEVICE_ID", 0)
	defaultWidth = getEnvInt("SHIFU_USB_CAMERA_DEFAULT_WIDTH", 640)
	defaultHeight = getEnvInt("SHIFU_USB_CAMERA_DEFAULT_HEIGHT", 480)
	defaultFPS = getEnvInt("SHIFU_USB_CAMERA_DEFAULT_FPS", 30)
	defaultFormat = getEnvFormat("SHIFU_USB_CAMERA_DEFAULT_FORMAT", FormatMJPEG)
}

func openCapture(config CaptureConfig) (*gocv.VideoCapture, error) {
	cap, err := gocv.OpenVideoCapture(config.DeviceID)
	if err != nil {
		return nil, fmt.Errorf("cannot open video capture: %v", err)
	}
	cap.Set(gocv.VideoCaptureFrameWidth, float64(config.Width))
	cap.Set(gocv.VideoCaptureFrameHeight, float64(config.Height))
	cap.Set(gocv.VideoCaptureFPS, float64(config.FPS))
	return cap, nil
}

func startCapture(config CaptureConfig) error {
	cameraState.Lock()
	defer cameraState.Unlock()
	if cameraState.IsCapturing {
		return nil
	}
	cap, err := openCapture(config)
	if err != nil {
		return err
	}
	cameraState.Capture = cap
	cameraState.Config = config
	cameraState.IsCapturing = true
	return nil
}

func stopCapture() error {
	cameraState.Lock()
	defer cameraState.Unlock()
	if cameraState.Capture != nil {
		cameraState.Capture.Close()
		cameraState.Capture = nil
	}
	cameraState.IsCapturing = false
	return nil
}

func getCaptureConfigFromRequest(r *http.Request) CaptureConfig {
	width := defaultWidth
	height := defaultHeight
	fps := defaultFPS
	format := defaultFormat

	q := r.URL.Query()
	if w := q.Get("width"); w != "" {
		if i, err := strconv.Atoi(w); err == nil {
			width = i
		}
	}
	if h := q.Get("height"); h != "" {
		if i, err := strconv.Atoi(h); err == nil {
			height = i
		}
	}
	if f := q.Get("fps"); f != "" {
		if i, err := strconv.Atoi(f); err == nil {
			fps = i
		}
	}
	if fm := q.Get("format"); fm != "" {
		switch strings.ToLower(fm) {
		case "mjpeg":
			format = FormatMJPEG
		case "yuyv":
			format = FormatYUYV
		case "h264":
			format = FormatH264
		}
	}

	return CaptureConfig{
		DeviceID: deviceID,
		Width:    width,
		Height:   height,
		FPS:      fps,
		Format:   format,
	}
}

func respondJSON(w http.ResponseWriter, code int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(data)
}

func apiStartCapture(w http.ResponseWriter, r *http.Request) {
	var conf CaptureConfig
	if r.Header.Get("Content-Type") == "application/json" && r.Method == http.MethodPost {
		type Req struct {
			Width  int    `json:"width,omitempty"`
			Height int    `json:"height,omitempty"`
			FPS    int    `json:"fps,omitempty"`
			Format string `json:"format,omitempty"`
		}
		var req Req
		json.NewDecoder(r.Body).Decode(&req)
		conf = CaptureConfig{
			DeviceID: deviceID,
			Width:    defaultWidth,
			Height:   defaultHeight,
			FPS:      defaultFPS,
			Format:   defaultFormat,
		}
		if req.Width > 0 {
			conf.Width = req.Width
		}
		if req.Height > 0 {
			conf.Height = req.Height
		}
		if req.FPS > 0 {
			conf.FPS = req.FPS
		}
		if req.Format != "" {
			switch strings.ToLower(req.Format) {
			case "mjpeg":
				conf.Format = FormatMJPEG
			case "yuyv":
				conf.Format = FormatYUYV
			case "h264":
				conf.Format = FormatH264
			}
		}
	} else {
		conf = getCaptureConfigFromRequest(r)
	}

	err := startCapture(conf)
	if err != nil {
		respondJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	respondJSON(w, http.StatusOK, map[string]any{
		"status": "capture started",
		"config": conf,
	})
}

func apiStopCapture(w http.ResponseWriter, r *http.Request) {
	err := stopCapture()
	if err != nil {
		respondJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	respondJSON(w, http.StatusOK, map[string]any{
		"status": "capture stopped",
	})
}

// /video/start and /capture/start
func apiVideoStart(w http.ResponseWriter, r *http.Request) { apiStartCapture(w, r) }
func apiCaptureStart(w http.ResponseWriter, r *http.Request) { apiStartCapture(w, r) }

// /video/stop and /capture/stop
func apiVideoStop(w http.ResponseWriter, r *http.Request) { apiStopCapture(w, r) }
func apiCaptureStop(w http.ResponseWriter, r *http.Request) { apiStopCapture(w, r) }

func streamMJPEG(w http.ResponseWriter, r *http.Request, cap *gocv.VideoCapture, fps int) {
	multipartWriter := multipart.NewWriter(w)
	boundary := multipartWriter.Boundary()
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	_ = multipartWriter.Close()

	img := gocv.NewMat()
	defer img.Close()

	interval := time.Second / time.Duration(fps)
	for {
		cameraState.Lock()
		active := cameraState.IsCapturing && cameraState.Capture == cap
		cameraState.Unlock()
		if !active {
			break
		}

		if ok := cap.Read(&img); !ok || img.Empty() {
			time.Sleep(interval)
			continue
		}
		buf := new(bytes.Buffer)
		if err := jpeg.Encode(buf, img.ToImage(), nil); err != nil {
			log.Printf("jpeg encode error: %v", err)
			continue
		}
		fmt.Fprintf(w, "--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", boundary, buf.Len())
		w.Write(buf.Bytes())
		w.Write([]byte("\r\n"))
		if flusher, ok := w.(http.Flusher); ok {
			flusher.Flush()
		}
		time.Sleep(interval)
		select {
		case <-r.Context().Done():
			return
		default:
		}
	}
}

func streamSingleFrame(w http.ResponseWriter, r *http.Request, cap *gocv.VideoCapture) {
	img := gocv.NewMat()
	defer img.Close()
	if ok := cap.Read(&img); !ok || img.Empty() {
		respondJSON(w, http.StatusInternalServerError, map[string]any{
			"error": "cannot capture frame",
		})
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	jpeg.Encode(w, img.ToImage(), nil)
}

// /video/stream and /stream
func apiVideoStream(w http.ResponseWriter, r *http.Request) {
	cameraState.Lock()
	cap := cameraState.Capture
	conf := cameraState.Config
	active := cameraState.IsCapturing && cap != nil
	cameraState.Unlock()
	if !active {
		respondJSON(w, http.StatusConflict, map[string]any{"error": "capture not started"})
		return
	}

	format := conf.Format
	if f := r.URL.Query().Get("format"); f != "" {
		switch strings.ToLower(f) {
		case "mjpeg":
			format = FormatMJPEG
		case "yuyv":
			format = FormatYUYV
		case "h264":
			format = FormatH264
		}
	}
	switch format {
	case FormatMJPEG:
		streamMJPEG(w, r, cap, conf.FPS)
	case FormatYUYV, FormatH264:
		// Not implemented: streaming raw YUYV/H264 in HTTP (needs encoding/packaging support)
		respondJSON(w, http.StatusNotImplemented, map[string]any{"error": "format not supported in HTTP stream"})
	default:
		respondJSON(w, http.StatusBadRequest, map[string]any{"error": "unknown format"})
	}
}

func apiStream(w http.ResponseWriter, r *http.Request) {
	apiVideoStream(w, r)
}

func main() {
	initConfig()

	http.HandleFunc("/video/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			apiVideoStream(w, r)
			return
		}
		http.NotFound(w, r)
	})
	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			apiStream(w, r)
			return
		}
		http.NotFound(w, r)
	})
	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			apiVideoStart(w, r)
			return
		}
		http.NotFound(w, r)
	})
	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			apiCaptureStart(w, r)
			return
		}
		http.NotFound(w, r)
	})
	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			apiVideoStop(w, r)
			return
		}
		http.NotFound(w, r)
	})
	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			apiCaptureStop(w, r)
			return
		}
		http.NotFound(w, r)
	})

	addr := fmt.Sprintf("%s:%s", serverHost, serverPort)
	log.Printf("USB Camera HTTP driver listening on %s", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("HTTP server failed: %v", err)
	}
}