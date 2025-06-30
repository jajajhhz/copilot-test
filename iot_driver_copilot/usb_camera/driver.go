package main

import (
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

	"golang.org/x/sync/errgroup"
	"gocv.io/x/gocv"
)

type VideoState struct {
	mu             sync.RWMutex
	capturing      bool
	streamFormat   string
	width, height  int
	fps            int
	deviceID       int
	videoCapture   *gocv.VideoCapture
	stopCh         chan struct{}
	activeClients  int
}

var state = &VideoState{
	capturing:    false,
	streamFormat: "mjpeg",
	width:        640,
	height:       480,
	fps:          15,
	deviceID:     0,
	stopCh:       nil,
}

type StartCaptureRequest struct {
	Format   string `json:"format"`
	Width    int    `json:"width"`
	Height   int    `json:"height"`
	FPS      int    `json:"fps"`
	DeviceID int    `json:"device_id"`
}

func envInt(key string, def int) int {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return def
	}
	return i
}
func envString(key, def string) string {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	return val
}

// Open camera with current state configuration
func (v *VideoState) openCamera() error {
	if v.videoCapture != nil {
		return nil
	}
	cam, err := gocv.OpenVideoCapture(v.deviceID)
	if err != nil {
		return err
	}
	if ok := cam.Set(gocv.VideoCaptureFrameWidth, float64(v.width)); !ok {
		// continue
	}
	if ok := cam.Set(gocv.VideoCaptureFrameHeight, float64(v.height)); !ok {
		// continue
	}
	if v.fps > 0 {
		if ok := cam.Set(gocv.VideoCaptureFPS, float64(v.fps)); !ok {
			// continue
		}
	}
	v.videoCapture = cam
	return nil
}

func (v *VideoState) closeCamera() {
	if v.videoCapture != nil {
		v.videoCapture.Close()
		v.videoCapture = nil
	}
}

// /video/start and /capture/start: POST
func startCaptureHandler(w http.ResponseWriter, r *http.Request) {
	state.mu.Lock()
	defer state.mu.Unlock()

	if state.capturing {
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status":  "already capturing",
			"format":  state.streamFormat,
			"width":   state.width,
			"height":  state.height,
			"fps":     state.fps,
			"device":  state.deviceID,
		})
		return
	}

	req := StartCaptureRequest{}
	if r.Header.Get("Content-Type") == "application/json" {
		json.NewDecoder(r.Body).Decode(&req)
	} else {
		r.ParseForm()
		req.Format = r.FormValue("format")
		req.Width, _ = strconv.Atoi(r.FormValue("width"))
		req.Height, _ = strconv.Atoi(r.FormValue("height"))
		req.FPS, _ = strconv.Atoi(r.FormValue("fps"))
		req.DeviceID, _ = strconv.Atoi(r.FormValue("device_id"))
	}
	if req.Format != "" {
		state.streamFormat = strings.ToLower(req.Format)
	}
	if req.Width > 0 {
		state.width = req.Width
	}
	if req.Height > 0 {
		state.height = req.Height
	}
	if req.FPS > 0 {
		state.fps = req.FPS
	}
	if req.DeviceID >= 0 {
		state.deviceID = req.DeviceID
	}

	state.stopCh = make(chan struct{})
	if err := state.openCamera(); err != nil {
		state.capturing = false
		state.closeCamera()
		http.Error(w, "Failed to open camera: "+err.Error(), http.StatusInternalServerError)
		return
	}
	state.capturing = true
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "started",
		"format":  state.streamFormat,
		"width":   state.width,
		"height":  state.height,
		"fps":     state.fps,
		"device":  state.deviceID,
	})
}

// /video/stop and /capture/stop: POST
func stopCaptureHandler(w http.ResponseWriter, r *http.Request) {
	state.mu.Lock()
	defer state.mu.Unlock()
	if !state.capturing {
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"status": "not capturing",
		})
		return
	}
	close(state.stopCh)
	state.capturing = false
	state.closeCamera()
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status": "stopped",
	})
}

// Helper for MJPEG streaming
func streamMJPEG(w http.ResponseWriter, r *http.Request, width, height, fps int) error {
	boundary := "MJPEGBOUNDARY"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")

	img := gocv.NewMat()
	defer img.Close()

	state.mu.Lock()
	err := state.openCamera()
	if err != nil {
		state.mu.Unlock()
		return errors.New("camera open failed: " + err.Error())
	}
	state.mu.Unlock()

	interval := time.Second / time.Duration(fps)
	for {
		state.mu.RLock()
		capturing := state.capturing
		stopCh := state.stopCh
		state.mu.RUnlock()

		if !capturing {
			break
		}

		select {
		case <-stopCh:
			return nil
		default:
		}

		if ok := state.videoCapture.Read(&img); !ok {
			continue
		}
		if img.Empty() {
			continue
		}
		buf, err := gocv.IMEncode(".jpg", img)
		if err != nil || len(buf) == 0 {
			continue
		}

		fmt.Fprintf(w, "--%s\r\n", boundary)
		fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
		fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf))
		w.Write(buf)
		fmt.Fprintf(w, "\r\n")
		w.(http.Flusher).Flush()

		time.Sleep(interval)
	}
	return nil
}

// /video/stream and /stream: GET
func streamHandler(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	format := strings.ToLower(q.Get("format"))
	if format == "" {
		format = state.streamFormat
	}
	width := state.width
	height := state.height
	fps := state.fps

	if v := q.Get("width"); v != "" {
		if i, err := strconv.Atoi(v); err == nil && i > 0 {
			width = i
		}
	}
	if v := q.Get("height"); v != "" {
		if i, err := strconv.Atoi(v); err == nil && i > 0 {
			height = i
		}
	}
	if v := q.Get("fps"); v != "" {
		if i, err := strconv.Atoi(v); err == nil && i > 0 {
			fps = i
		}
	}

	state.mu.Lock()
	if !state.capturing {
		state.mu.Unlock()
		http.Error(w, "Video capture not started", http.StatusServiceUnavailable)
		return
	}
	state.activeClients++
	state.mu.Unlock()

	defer func() {
		state.mu.Lock()
		state.activeClients--
		state.mu.Unlock()
	}()

	if format == "mjpeg" || format == "" {
		err := streamMJPEG(w, r, width, height, fps)
		if err != nil {
			http.Error(w, "MJPEG streaming error: "+err.Error(), http.StatusInternalServerError)
			return
		}
	} else {
		http.Error(w, "unsupported stream format: "+format, http.StatusBadRequest)
	}
}

// Helper for liveness/readiness
func pingHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("OK"))
}

// API/route mapping
func main() {
	state.width = envInt("CAMERA_WIDTH", 640)
	state.height = envInt("CAMERA_HEIGHT", 480)
	state.fps = envInt("CAMERA_FPS", 15)
	state.deviceID = envInt("CAMERA_DEVICE_ID", 0)
	state.streamFormat = strings.ToLower(envString("CAMERA_FORMAT", "mjpeg"))

	host := envString("SERVER_HOST", "0.0.0.0")
	port := envInt("SERVER_PORT", 8080)

	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			startCaptureHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})
	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			startCaptureHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})
	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			stopCaptureHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})
	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			stopCaptureHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})

	http.HandleFunc("/video/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			streamHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})
	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			streamHandler(w, r)
			return
		}
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	})

	http.HandleFunc("/ping", pingHandler)
	http.HandleFunc("/healthz", pingHandler)

	addr := fmt.Sprintf("%s:%d", host, port)
	log.Printf("USB Camera HTTP server listening on %s\n", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatal(err)
	}
}