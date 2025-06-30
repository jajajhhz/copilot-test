package main

import (
	"bytes"
	"encoding/json"
	"errors"
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

type VideoState struct {
	sync.Mutex
	Started   bool
	Format    string
	Width     int
	Height    int
	FPS       int
	DeviceID  int
	Capture   *gocv.VideoCapture
}

var (
	state = VideoState{
		Started:  false,
		Format:   "mjpeg",
		Width:    640,
		Height:   480,
		FPS:      15,
		DeviceID: 0,
		Capture:  nil,
	}

	serverHost = getEnv("SERVER_HOST", "0.0.0.0")
	serverPort = getEnv("SERVER_PORT", "8080")
	deviceId   = getEnvAsInt("DEVICE_ID", 0)
	defaultFmt = getEnv("DEFAULT_FORMAT", "mjpeg")
	defaultW   = getEnvAsInt("DEFAULT_WIDTH", 640)
	defaultH   = getEnvAsInt("DEFAULT_HEIGHT", 480)
	defaultFPS = getEnvAsInt("DEFAULT_FPS", 15)
)

func main() {
	state.DeviceID = deviceId
	state.Format = defaultFmt
	state.Width = defaultW
	state.Height = defaultH
	state.FPS = defaultFPS

	http.HandleFunc("/capture/start", handleCaptureStart)
	http.HandleFunc("/capture/stop", handleCaptureStop)
	http.HandleFunc("/video/start", handleVideoStart)
	http.HandleFunc("/video/stop", handleVideoStop)
	http.HandleFunc("/video/stream", handleVideoStream)
	http.HandleFunc("/stream", handleStream)

	addr := fmt.Sprintf("%s:%s", serverHost, serverPort)
	log.Printf("USB Camera HTTP server started at %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}

func handleCaptureStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
		return
	}
	opt := parseVideoOptions(r)
	if err := startCapture(opt); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "capture started"})
}

func handleCaptureStop(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
		return
	}
	stopCapture()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "capture stopped"})
}

func handleVideoStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
		return
	}
	opt := parseVideoOptions(r)
	if err := startCapture(opt); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "video started"})
}

func handleVideoStop(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Only POST supported", http.StatusMethodNotAllowed)
		return
	}
	stopCapture()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "video stopped"})
}

func handleVideoStream(w http.ResponseWriter, r *http.Request) {
	streamHandler(w, r)
}

func handleStream(w http.ResponseWriter, r *http.Request) {
	streamHandler(w, r)
}

func streamHandler(w http.ResponseWriter, r *http.Request) {
	format := strings.ToLower(r.URL.Query().Get("format"))
	if format == "" {
		format = state.Format
	}
	if format != "mjpeg" {
		http.Error(w, "Only MJPEG streaming is supported in this driver", http.StatusBadRequest)
		return
	}
	width := getIntQuery(r, "width", state.Width)
	height := getIntQuery(r, "height", state.Height)
	fps := getIntQuery(r, "fps", state.FPS)

	opt := VideoOptions{
		Format: format,
		Width:  width,
		Height: height,
		FPS:    fps,
	}

	state.Lock()
	wasStarted := state.Started
	state.Unlock()

	if !wasStarted {
		if err := startCapture(opt); err != nil {
			http.Error(w, fmt.Sprintf("Unable to start camera: %v", err), http.StatusInternalServerError)
			return
		}
	}

	boundary := "mjpegframeboundary"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)

	mw := multipart.NewWriter(w)
	mw.SetBoundary(boundary)

	ticker := time.NewTicker(time.Second / time.Duration(opt.FPS))
	defer ticker.Stop()
	defer r.Body.Close()

	for {
		select {
		case <-ticker.C:
			state.Lock()
			if !state.Started || state.Capture == nil {
				state.Unlock()
				return
			}
			img := gocv.NewMat()
			ok := state.Capture.Read(&img)
			state.Unlock()
			if !ok || img.Empty() {
				img.Close()
				continue
			}
			buf, err := toJPEGBuffer(img)
			img.Close()
			if err != nil {
				continue
			}
			// Write multipart frame
			fmt.Fprintf(w, "--%s\r\n", boundary)
			fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
			fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf.Bytes()))
			w.Write(buf.Bytes())
			fmt.Fprintf(w, "\r\n")
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
		case <-r.Context().Done():
			return
		}
	}
}

type VideoOptions struct {
	Format string
	Width  int
	Height int
	FPS    int
}

func parseVideoOptions(r *http.Request) VideoOptions {
	format := strings.ToLower(r.URL.Query().Get("format"))
	if format == "" {
		format = state.Format
	}
	width := getIntQuery(r, "width", state.Width)
	height := getIntQuery(r, "height", state.Height)
	fps := getIntQuery(r, "fps", state.FPS)
	return VideoOptions{
		Format: format,
		Width:  width,
		Height: height,
		FPS:    fps,
	}
}

func startCapture(opt VideoOptions) error {
	state.Lock()
	defer state.Unlock()
	if state.Started {
		updateCaptureProperties(opt)
		return nil
	}
	cap, err := gocv.OpenVideoCapture(state.DeviceID)
	if err != nil {
		return fmt.Errorf("failed to open video capture: %v", err)
	}
	if !cap.IsOpened() {
		return errors.New("could not open USB camera device")
	}
	if err := cap.Set(gocv.VideoCaptureFrameWidth, float64(opt.Width)); err != nil {
		log.Printf("failed to set width: %v", err)
	}
	if err := cap.Set(gocv.VideoCaptureFrameHeight, float64(opt.Height)); err != nil {
		log.Printf("failed to set height: %v", err)
	}
	if err := cap.Set(gocv.VideoCaptureFPS, float64(opt.FPS)); err != nil {
		log.Printf("failed to set fps: %v", err)
	}
	state.Capture = cap
	state.Started = true
	state.Format = opt.Format
	state.Width = opt.Width
	state.Height = opt.Height
	state.FPS = opt.FPS
	return nil
}

func updateCaptureProperties(opt VideoOptions) {
	if state.Capture == nil {
		return
	}
	state.Capture.Set(gocv.VideoCaptureFrameWidth, float64(opt.Width))
	state.Capture.Set(gocv.VideoCaptureFrameHeight, float64(opt.Height))
	state.Capture.Set(gocv.VideoCaptureFPS, float64(opt.FPS))
	state.Format = opt.Format
	state.Width = opt.Width
	state.Height = opt.Height
	state.FPS = opt.FPS
}

func stopCapture() {
	state.Lock()
	defer state.Unlock()
	if state.Capture != nil {
		state.Capture.Close()
		state.Capture = nil
	}
	state.Started = false
}

func toJPEGBuffer(mat gocv.Mat) (*bytes.Buffer, error) {
	img, err := mat.ToImage()
	if err != nil {
		return nil, err
	}
	buf := &bytes.Buffer{}
	err = jpeg.Encode(buf, img.(image.Image), &jpeg.Options{Quality: 80})
	return buf, err
}

func getIntQuery(r *http.Request, key string, fallback int) int {
	val := r.URL.Query().Get(key)
	if val == "" {
		return fallback
	}
	v, err := strconv.Atoi(val)
	if err != nil || v <= 0 {
		return fallback
	}
	return v
}

func getEnv(key, fallback string) string {
	val := os.Getenv(key)
	if val == "" {
		return fallback
	}
	return val
}

func getEnvAsInt(name string, fallback int) int {
	valStr := os.Getenv(name)
	if valStr == "" {
		return fallback
	}
	val, err := strconv.Atoi(valStr)
	if err != nil {
		return fallback
	}
	return val
}