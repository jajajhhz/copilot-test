package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"log"
	"mime"
	"mime/multipart"
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

type StreamState struct {
	sync.Mutex
	Started      bool
	VideoCapture *gocv.VideoCapture
	Config       VideoConfig
}

var (
	streamState = &StreamState{}
)

func getEnvInt(name string, defaultVal int) int {
	valStr := os.Getenv(name)
	if valStr == "" {
		return defaultVal
	}
	val, err := strconv.Atoi(valStr)
	if err != nil {
		return defaultVal
	}
	return val
}

func getEnvStr(name string, defaultVal string) string {
	val := os.Getenv(name)
	if val == "" {
		return defaultVal
	}
	return val
}

func parseVideoConfigFromQuery(r *http.Request) VideoConfig {
	deviceID := getEnvInt("CAMERA_DEVICE_ID", 0)
	width := getEnvInt("CAMERA_WIDTH", 640)
	height := getEnvInt("CAMERA_HEIGHT", 480)
	fps := getEnvInt("CAMERA_FPS", 15)
	format := getEnvStr("CAMERA_FORMAT", "MJPEG")

	q := r.URL.Query()
	if v := q.Get("device_id"); v != "" {
		if id, err := strconv.Atoi(v); err == nil {
			deviceID = id
		}
	}
	if v := q.Get("width"); v != "" {
		if w, err := strconv.Atoi(v); err == nil {
			width = w
		}
	}
	if v := q.Get("height"); v != "" {
		if h, err := strconv.Atoi(v); err == nil {
			height = h
		}
	}
	if v := q.Get("fps"); v != "" {
		if f, err := strconv.Atoi(v); err == nil {
			fps = f
		}
	}
	if v := q.Get("format"); v != "" {
		format = strings.ToUpper(v)
	}
	return VideoConfig{
		DeviceID: deviceID,
		Width:    width,
		Height:   height,
		FPS:      fps,
		Format:   format,
	}
}

func startCapture(config VideoConfig) error {
	streamState.Lock()
	defer streamState.Unlock()
	if streamState.Started {
		return nil
	}
	vc, err := gocv.OpenVideoCapture(config.DeviceID)
	if err != nil {
		return fmt.Errorf("failed to open camera: %v", err)
	}
	if !vc.IsOpened() {
		vc.Close()
		return errors.New("camera not opened")
	}
	vc.Set(gocv.VideoCaptureFrameWidth, float64(config.Width))
	vc.Set(gocv.VideoCaptureFrameHeight, float64(config.Height))
	vc.Set(gocv.VideoCaptureFPS, float64(config.FPS))
	streamState.VideoCapture = vc
	streamState.Config = config
	streamState.Started = true
	return nil
}

func stopCapture() error {
	streamState.Lock()
	defer streamState.Unlock()
	if !streamState.Started {
		return nil
	}
	if streamState.VideoCapture != nil {
		streamState.VideoCapture.Close()
	}
	streamState.VideoCapture = nil
	streamState.Started = false
	return nil
}

func respondJSON(w http.ResponseWriter, v interface{}, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func handleStartCapture(w http.ResponseWriter, r *http.Request) {
	var config VideoConfig
	if err := json.NewDecoder(r.Body).Decode(&config); err != nil {
		config = parseVideoConfigFromQuery(r)
	}
	if config.Format == "" {
		config.Format = getEnvStr("CAMERA_FORMAT", "MJPEG")
	}
	if err := startCapture(config); err != nil {
		respondJSON(w, map[string]string{"error": err.Error()}, http.StatusInternalServerError)
		return
	}
	respondJSON(w, map[string]string{"status": "capture started"}, http.StatusOK)
}

func handleStopCapture(w http.ResponseWriter, r *http.Request) {
	if err := stopCapture(); err != nil {
		respondJSON(w, map[string]string{"error": err.Error()}, http.StatusInternalServerError)
		return
	}
	respondJSON(w, map[string]string{"status": "capture stopped"}, http.StatusOK)
}

func handleStartVideo(w http.ResponseWriter, r *http.Request) {
	config := parseVideoConfigFromQuery(r)
	if err := startCapture(config); err != nil {
		respondJSON(w, map[string]string{"error": err.Error()}, http.StatusInternalServerError)
		return
	}
	respondJSON(w, map[string]string{"status": "video started"}, http.StatusOK)
}

func handleStopVideo(w http.ResponseWriter, r *http.Request) {
	if err := stopCapture(); err != nil {
		respondJSON(w, map[string]string{"error": err.Error()}, http.StatusInternalServerError)
		return
	}
	respondJSON(w, map[string]string{"status": "video stopped"}, http.StatusOK)
}

func serveMJPEG(w http.ResponseWriter, r *http.Request, config VideoConfig) {
	boundary := "mjpegboundary"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")

	if err := startCapture(config); err != nil {
		http.Error(w, "Failed to start camera: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer stopCapture()

	mw := multipart.NewWriter(w)
	_ = mw.SetBoundary(boundary)
	vc := streamState.VideoCapture
	img := gocv.NewMat()
	defer img.Close()
	fps := config.FPS
	if fps <= 0 {
		fps = 15
	}
	delay := time.Second / time.Duration(fps)

	for {
		if !vc.Read(&img) {
			http.Error(w, "Failed to read image", http.StatusInternalServerError)
			return
		}
		buf := &bytes.Buffer{}
		jpegOpts := &jpeg.Options{Quality: 80}
		imgRGB, err := img.ToImage()
		if err != nil {
			http.Error(w, "Failed to convert image", http.StatusInternalServerError)
			return
		}
		if err = jpeg.Encode(buf, imgRGB, jpegOpts); err != nil {
			http.Error(w, "Failed to encode jpeg", http.StatusInternalServerError)
			return
		}
		fmt.Fprintf(w, "--%s\r\n", boundary)
		fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
		fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", buf.Len())
		if _, err := w.Write(buf.Bytes()); err != nil {
			return
		}
		fmt.Fprintf(w, "\r\n")
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		time.Sleep(delay)
		select {
		case <-r.Context().Done():
			return
		default:
		}
	}
}

func handleVideoStream(w http.ResponseWriter, r *http.Request) {
	config := parseVideoConfigFromQuery(r)
	format := config.Format
	if format == "" {
		format = "MJPEG"
	}
	if strings.ToUpper(format) == "MJPEG" {
		serveMJPEG(w, r, config)
		return
	}
	http.Error(w, "Unsupported format: "+format, http.StatusBadRequest)
}

func handleStream(w http.ResponseWriter, r *http.Request) {
	handleVideoStream(w, r)
}

func main() {
	host := getEnvStr("HTTP_SERVER_HOST", "0.0.0.0")
	port := getEnvStr("HTTP_SERVER_PORT", "8080")
	addr := host + ":" + port

	http.HandleFunc("/video/stream", handleVideoStream)
	http.HandleFunc("/stream", handleStream)
	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Invalid method", http.StatusMethodNotAllowed)
			return
		}
		handleStartCapture(w, r)
	})
	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Invalid method", http.StatusMethodNotAllowed)
			return
		}
		handleStopCapture(w, r)
	})
	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Invalid method", http.StatusMethodNotAllowed)
			return
		}
		handleStartVideo(w, r)
	})
	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Invalid method", http.StatusMethodNotAllowed)
			return
		}
		handleStopVideo(w, r)
	})

	log.Printf("USB Camera HTTP driver listening at %s\n", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
}