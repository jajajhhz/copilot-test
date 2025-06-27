package main

import (
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

type VideoConfig struct {
	DeviceID   int
	Format     string
	Width      int
	Height     int
	FPS        int
}

type VideoCaptureState struct {
	sync.RWMutex
	Capturing      bool
	Streaming      bool
	Format         string
	Width          int
	Height         int
	FPS            int
	VideoCapture   *gocv.VideoCapture
	Frame          gocv.Mat
	Clients        map[chan []byte]struct{}
	StopChan       chan struct{}
	StoppedChan    chan struct{}
}

func getEnvInt(key string, def int) int {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	ret, err := strconv.Atoi(val)
	if err != nil {
		return def
	}
	return ret
}
func getEnvStr(key string, def string) string {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	return val
}

func parseResolution(resStr string) (int, int, error) {
	parts := strings.Split(resStr, "x")
	if len(parts) != 2 {
		return 0, 0, errors.New("invalid resolution format")
	}
	width, err1 := strconv.Atoi(parts[0])
	height, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil {
		return 0, 0, errors.New("invalid resolution values")
	}
	return width, height, nil
}

func parseVideoConfig(r *http.Request, def VideoConfig) VideoConfig {
	format := r.URL.Query().Get("format")
	if format == "" {
		format = def.Format
	}
	res := r.URL.Query().Get("resolution")
	width, height := def.Width, def.Height
	if res != "" {
		w, h, err := parseResolution(res)
		if err == nil {
			width, height = w, h
		}
	}
	fps := def.FPS
	fpsStr := r.URL.Query().Get("fps")
	if fpsStr != "" {
		if v, err := strconv.Atoi(fpsStr); err == nil {
			fps = v
		}
	}
	return VideoConfig{
		DeviceID: def.DeviceID,
		Format:   format,
		Width:    width,
		Height:   height,
		FPS:      fps,
	}
}

func startCapture(state *VideoCaptureState, config VideoConfig) error {
	state.Lock()
	defer state.Unlock()
	if state.Capturing {
		return nil
	}
	vc, err := gocv.OpenVideoCapture(config.DeviceID)
	if err != nil {
		return fmt.Errorf("cannot open device: %v", err)
	}
	vc.Set(gocv.VideoCaptureFrameWidth, float64(config.Width))
	vc.Set(gocv.VideoCaptureFrameHeight, float64(config.Height))
	vc.Set(gocv.VideoCaptureFPS, float64(config.FPS))
	state.VideoCapture = vc
	state.Frame = gocv.NewMat()
	state.Capturing = true
	state.Format = config.Format
	state.Width = config.Width
	state.Height = config.Height
	state.FPS = config.FPS
	state.StopChan = make(chan struct{})
	state.StoppedChan = make(chan struct{})
	go captureLoop(state)
	return nil
}

func stopCapture(state *VideoCaptureState) {
	state.Lock()
	if !state.Capturing {
		state.Unlock()
		return
	}
	close(state.StopChan)
	state.Unlock()
	<-state.StoppedChan
}

func captureLoop(state *VideoCaptureState) {
	defer func() {
		state.Lock()
		if state.VideoCapture != nil {
			state.VideoCapture.Close()
		}
		if state.Frame.IsContinuous() {
			state.Frame.Close()
		}
		state.Capturing = false
		state.Unlock()
		close(state.StoppedChan)
	}()

	delay := time.Duration(int64(1e9 / state.FPS))
	for {
		select {
		case <-state.StopChan:
			return
		default:
			if ok := state.VideoCapture.Read(&state.Frame); !ok || state.Frame.Empty() {
				time.Sleep(delay)
				continue
			}
			state.RLock()
			for client := range state.Clients {
				buf, err := gocv.IMEncode(gocv.JPEGFileExt, state.Frame)
				if err == nil {
					select {
					case client <- buf.GetBytes():
					default:
					}
				}
			}
			state.RUnlock()
			time.Sleep(delay)
		}
	}
}

func streamMJPEG(state *VideoCaptureState, w http.ResponseWriter, r *http.Request) {
	boundary := "mjpegframe"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}
	frameChan := make(chan []byte, 2)
	state.Lock()
	state.Clients[frameChan] = struct{}{}
	state.Unlock()
	defer func() {
		state.Lock()
		delete(state.Clients, frameChan)
		state.Unlock()
		close(frameChan)
	}()
	for {
		select {
		case <-r.Context().Done():
			return
		case frame := <-frameChan:
			_, _ = fmt.Fprintf(w, "--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", boundary, len(frame))
			_, _ = w.Write(frame)
			_, _ = w.Write([]byte("\r\n"))
			flusher.Flush()
		}
	}
}

func streamSingleJPEG(state *VideoCaptureState, w http.ResponseWriter, r *http.Request) {
	state.RLock()
	defer state.RUnlock()
	if !state.Capturing {
		http.Error(w, "Not capturing", http.StatusBadRequest)
		return
	}
	buf, err := gocv.IMEncode(gocv.JPEGFileExt, state.Frame)
	if err != nil {
		http.Error(w, "Failed to encode frame", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Write(buf.GetBytes())
}

func main() {
	defaultDeviceID := getEnvInt("USB_CAMERA_DEVICE_ID", 0)
	serverHost := getEnvStr("SHIFU_USB_CAMERA_SERVER_HOST", "0.0.0.0")
	serverPort := getEnvStr("SHIFU_USB_CAMERA_SERVER_PORT", "8080")
	defaultFormat := getEnvStr("USB_CAMERA_DEFAULT_FORMAT", "mjpeg")
	defaultResolution := getEnvStr("USB_CAMERA_DEFAULT_RESOLUTION", "640x480")
	defaultFPS := getEnvInt("USB_CAMERA_DEFAULT_FPS", 20)
	width, height, err := parseResolution(defaultResolution)
	if err != nil {
		width, height = 640, 480
	}

	state := &VideoCaptureState{
		Capturing:   false,
		Streaming:   false,
		Format:      defaultFormat,
		Width:       width,
		Height:      height,
		FPS:         defaultFPS,
		Clients:     make(map[chan []byte]struct{}),
	}

	defaultConfig := VideoConfig{
		DeviceID: defaultDeviceID,
		Format:   defaultFormat,
		Width:    width,
		Height:   height,
		FPS:      defaultFPS,
	}

	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST allowed", http.StatusMethodNotAllowed)
			return
		}
		config := defaultConfig
		if r.Header.Get("Content-Type") == "application/json" {
			var req struct {
				Format     string `json:"format"`
				Resolution string `json:"resolution"`
				FPS        int    `json:"fps"`
			}
			_ = json.NewDecoder(r.Body).Decode(&req)
			if req.Format != "" {
				config.Format = req.Format
			}
			if req.Resolution != "" {
				if w2, h2, err := parseResolution(req.Resolution); err == nil {
					config.Width, config.Height = w2, h2
				}
			}
			if req.FPS > 0 {
				config.FPS = req.FPS
			}
		} else {
			config = parseVideoConfig(r, defaultConfig)
		}
		err := startCapture(state, config)
		resp := make(map[string]interface{})
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			resp["success"] = false
			resp["error"] = err.Error()
		} else {
			resp["success"] = true
			resp["message"] = "Video capture started"
		}
		_ = json.NewEncoder(w).Encode(resp)
	})

	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST allowed", http.StatusMethodNotAllowed)
			return
		}
		stopCapture(state)
		resp := map[string]interface{}{
			"success": true,
			"message": "Capture stopped",
		}
		_ = json.NewEncoder(w).Encode(resp)
	})

	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST allowed", http.StatusMethodNotAllowed)
			return
		}
		config := parseVideoConfig(r, defaultConfig)
		err := startCapture(state, config)
		resp := make(map[string]interface{})
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			resp["success"] = false
			resp["error"] = err.Error()
		} else {
			resp["success"] = true
			resp["message"] = "Video capture started"
		}
		_ = json.NewEncoder(w).Encode(resp)
	})

	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Only POST allowed", http.StatusMethodNotAllowed)
			return
		}
		stopCapture(state)
		resp := map[string]interface{}{
			"success": true,
			"message": "Video capture stopped",
		}
		_ = json.NewEncoder(w).Encode(resp)
	})

	http.HandleFunc("/video/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Only GET allowed", http.StatusMethodNotAllowed)
			return
		}
		state.RLock()
		capturing := state.Capturing
		state.RUnlock()
		if !capturing {
			http.Error(w, "Video capture is not running. Please start capture first.", http.StatusBadRequest)
			return
		}
		streamMJPEG(state, w, r)
	})

	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Only GET allowed", http.StatusMethodNotAllowed)
			return
		}
		state.RLock()
		capturing := state.Capturing
		state.RUnlock()
		if !capturing {
			http.Error(w, "Video capture is not running. Please start capture first.", http.StatusBadRequest)
			return
		}
		streamMJPEG(state, w, r)
	})

	http.HandleFunc("/snapshot", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Only GET allowed", http.StatusMethodNotAllowed)
			return
		}
		streamSingleJPEG(state, w, r)
	})

	addr := fmt.Sprintf("%s:%s", serverHost, serverPort)
	log.Printf("USB Camera HTTP Driver starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}