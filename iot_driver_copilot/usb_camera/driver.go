package main

import (
	"bytes"
	"context"
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
	DefaultFormat           = FormatMJPEG
)

type Resolution struct {
	Width  int
	Height int
}

type CameraState struct {
	mu           sync.Mutex
	capturing    bool
	streamCancel context.CancelFunc
	format       VideoFormat
	resolution   Resolution
	deviceID     int
}

func (cs *CameraState) StartCapture(format VideoFormat, resolution Resolution) error {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.capturing {
		return errors.New("capture already in progress")
	}
	cs.format = format
	cs.resolution = resolution
	cs.capturing = true
	return nil
}

func (cs *CameraState) StopCapture() {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.streamCancel != nil {
		cs.streamCancel()
		cs.streamCancel = nil
	}
	cs.capturing = false
}

func (cs *CameraState) IsCapturing() bool {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	return cs.capturing
}

func (cs *CameraState) SetStreamCancel(cancel context.CancelFunc) {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	cs.streamCancel = cancel
}

func parseFormat(s string) VideoFormat {
	switch strings.ToUpper(s) {
	case "MJPEG":
		return FormatMJPEG
	case "H264":
		return FormatH264
	case "YUYV":
		return FormatYUYV
	default:
		return DefaultFormat
	}
}

func parseResolution(qs map[string][]string) Resolution {
	w, _ := strconv.Atoi(getFirst(qs, "width", "640"))
	h, _ := strconv.Atoi(getFirst(qs, "height", "480"))
	return Resolution{Width: w, Height: h}
}

func getFirst(qs map[string][]string, key string, def string) string {
	if v, ok := qs[key]; ok && len(v) > 0 && v[0] != "" {
		return v[0]
	}
	return def
}

func parseDeviceID() int {
	deviceStr := os.Getenv("CAMERA_DEVICE_ID")
	if deviceStr == "" {
		deviceStr = "0"
	}
	deviceID, err := strconv.Atoi(deviceStr)
	if err != nil {
		return 0
	}
	return deviceID
}

func streamMJPEG(w http.ResponseWriter, req *http.Request, cs *CameraState, format VideoFormat, resolution Resolution) {
	ctx, cancel := context.WithCancel(req.Context())
	cs.SetStreamCancel(cancel)
	defer cancel()

	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")

	webcam, err := gocv.OpenVideoCapture(cs.deviceID)
	if err != nil {
		http.Error(w, "Unable to open camera device", http.StatusInternalServerError)
		return
	}
	defer webcam.Close()
	webcam.Set(gocv.VideoCaptureFrameWidth, float64(resolution.Width))
	webcam.Set(gocv.VideoCaptureFrameHeight, float64(resolution.Height))

	img := gocv.NewMat()
	defer img.Close()

	buf := &bytes.Buffer{}
	for cs.IsCapturing() {
		select {
		case <-ctx.Done():
			return
		default:
		}
		if ok := webcam.Read(&img); !ok || img.Empty() {
			time.Sleep(20 * time.Millisecond)
			continue
		}
		buf.Reset()
		jpgOpt := &jpeg.Options{Quality: 80}
		imgMat, _ := img.ToImage()
		if err := jpeg.Encode(buf, imgMat, jpgOpt); err != nil {
			continue
		}
		fmt.Fprintf(w, "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", buf.Len())
		if _, err := w.Write(buf.Bytes()); err != nil {
			return
		}
		fmt.Fprint(w, "\r\n")
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}
}

func basicJSON(w http.ResponseWriter, status int, data map[string]interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

func startHandler(cs *CameraState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		format := parseFormat(getFirst(r.URL.Query(), "format", string(DefaultFormat)))
		res := parseResolution(r.URL.Query())
		err := cs.StartCapture(format, res)
		if err != nil {
			basicJSON(w, http.StatusBadRequest, map[string]interface{}{
				"error": err.Error(),
			})
			return
		}
		basicJSON(w, http.StatusOK, map[string]interface{}{
			"message": "capture started",
			"format":  format,
			"width":   res.Width,
			"height":  res.Height,
		})
	}
}

func stopHandler(cs *CameraState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cs.StopCapture()
		basicJSON(w, http.StatusOK, map[string]interface{}{
			"message": "capture stopped",
		})
	}
}

// For streaming endpoints: /stream and /video/stream
func streamHandler(cs *CameraState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !cs.IsCapturing() {
			http.Error(w, "Video capture not started. Start capture first.", http.StatusBadRequest)
			return
		}
		format := parseFormat(getFirst(r.URL.Query(), "format", string(cs.format)))
		res := parseResolution(r.URL.Query())
		streamMJPEG(w, r, cs, format, res)
	}
}

// POST /capture/start and /video/start
func captureStartHandler(cs *CameraState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		format := parseFormat(getFirst(r.URL.Query(), "format", string(DefaultFormat)))
		res := parseResolution(r.URL.Query())
		err := cs.StartCapture(format, res)
		if err != nil {
			basicJSON(w, http.StatusBadRequest, map[string]interface{}{
				"error": err.Error(),
			})
			return
		}
		basicJSON(w, http.StatusOK, map[string]interface{}{
			"message": "video started",
			"format":  format,
			"width":   res.Width,
			"height":  res.Height,
		})
	}
}

// POST /capture/stop and /video/stop
func captureStopHandler(cs *CameraState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cs.StopCapture()
		basicJSON(w, http.StatusOK, map[string]interface{}{
			"message": "video stopped",
		})
	}
}

func main() {
	host := os.Getenv("SHIFU_HTTP_HOST")
	if host == "" {
		host = "0.0.0.0"
	}
	port := os.Getenv("SHIFU_HTTP_PORT")
	if port == "" {
		port = "8080"
	}
	addr := fmt.Sprintf("%s:%s", host, port)
	deviceID := parseDeviceID()

	cameraState := &CameraState{
		capturing:  false,
		format:     DefaultFormat,
		resolution: Resolution{Width: 640, Height: 480},
		deviceID:   deviceID,
	}

	http.HandleFunc("/video/stream", streamHandler(cameraState))
	http.HandleFunc("/stream", streamHandler(cameraState))

	http.HandleFunc("/capture/start", captureStartHandler(cameraState))
	http.HandleFunc("/video/start", captureStartHandler(cameraState))

	http.HandleFunc("/capture/stop", captureStopHandler(cameraState))
	http.HandleFunc("/video/stop", captureStopHandler(cameraState))

	log.Printf("Starting USB Camera HTTP driver on %s (device ID: %d)", addr, deviceID)
	log.Fatal(http.ListenAndServe(addr, nil))
}