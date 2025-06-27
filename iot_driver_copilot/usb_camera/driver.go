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

type CaptureState struct {
	sync.Mutex
	IsCapturing bool
	Format      string
	Width       int
	Height      int
	Fps         int
}

type StartCaptureRequest struct {
	Format string `json:"format"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
	Fps    int    `json:"fps"`
}

type Response struct {
	Status  string `json:"status"`
	Message string `json:"message,omitempty"`
}

var (
	videoDeviceID    int
	serverHost       string
	serverPort       string
	defaultFormat    string
	defaultWidth     int
	defaultHeight    int
	defaultFps       int
	state            = &CaptureState{}
	capture          *gocv.VideoCapture
	captureInitOnce  sync.Once
	streamClients    = make(map[chan []byte]struct{})
	streamClientsMux sync.Mutex
)

func getenvInt(key string, fallback int) int {
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

func getenvStr(key, fallback string) string {
	val := os.Getenv(key)
	if val == "" {
		return fallback
	}
	return val
}

func main() {
	videoDeviceID = getenvInt("USB_CAMERA_DEVICE_ID", 0)
	serverHost = getenvStr("HTTP_SERVER_HOST", "0.0.0.0")
	serverPort = getenvStr("HTTP_SERVER_PORT", "8080")
	defaultFormat = getenvStr("USB_CAMERA_DEFAULT_FORMAT", "MJPEG")
	defaultWidth = getenvInt("USB_CAMERA_DEFAULT_WIDTH", 640)
	defaultHeight = getenvInt("USB_CAMERA_DEFAULT_HEIGHT", 480)
	defaultFps = getenvInt("USB_CAMERA_DEFAULT_FPS", 30)

	state.Format = defaultFormat
	state.Width = defaultWidth
	state.Height = defaultHeight
	state.Fps = defaultFps

	http.HandleFunc("/capture/start", handleStartCapture)
	http.HandleFunc("/video/start", handleStartCapture)
	http.HandleFunc("/capture/stop", handleStopCapture)
	http.HandleFunc("/video/stop", handleStopCapture)
	http.HandleFunc("/video/stream", handleStream)
	http.HandleFunc("/stream", handleStream)

	log.Printf("Starting USB camera HTTP driver at %s:%s ...", serverHost, serverPort)
	log.Fatal(http.ListenAndServe(fmt.Sprintf("%s:%s", serverHost, serverPort), nil))
}

func initCapture(format string, width, height, fps int) error {
	var err error
	captureInitOnce.Do(func() {
		capture, err = gocv.OpenVideoCapture(videoDeviceID)
	})
	if err != nil || capture == nil {
		return errors.New("cannot open USB camera")
	}
	if ok := capture.Set(gocv.VideoCaptureFrameWidth, float64(width)); !ok {
		return errors.New("failed to set width")
	}
	if ok := capture.Set(gocv.VideoCaptureFrameHeight, float64(height)); !ok {
		return errors.New("failed to set height")
	}
	if ok := capture.Set(gocv.VideoCaptureFPS, float64(fps)); !ok {
		return errors.New("failed to set fps")
	}
	// Attempt to set format if supported
	if format != "" {
		fourcc := formatToFourCC(format)
		if fourcc != 0 {
			_ = capture.Set(gocv.VideoCaptureFOURCC, float64(fourcc))
		}
	}
	return nil
}

func releaseCapture() {
	if capture != nil {
		capture.Release()
		capture = nil
		captureInitOnce = sync.Once{}
	}
}

func formatToFourCC(format string) int {
	switch strings.ToUpper(format) {
	case "MJPEG":
		return gocv.VideoWriterFourcc('M', 'J', 'P', 'G')
	case "H264":
		return gocv.VideoWriterFourcc('H', '2', '6', '4')
	case "YUYV":
		return gocv.VideoWriterFourcc('Y', 'U', 'Y', 'V')
	default:
		return 0
	}
}

func handleStartCapture(w http.ResponseWriter, r *http.Request) {
	state.Lock()
	defer state.Unlock()
	if state.IsCapturing {
		resp := Response{Status: "ok", Message: "already capturing"}
		writeJSON(w, http.StatusOK, resp)
		return
	}
	var req StartCaptureRequest
	if r.Method == http.MethodPost {
		ct, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if ct == "application/json" {
			json.NewDecoder(r.Body).Decode(&req)
		} else {
			req.Format = r.FormValue("format")
			req.Width, _ = strconv.Atoi(r.FormValue("width"))
			req.Height, _ = strconv.Atoi(r.FormValue("height"))
			req.Fps, _ = strconv.Atoi(r.FormValue("fps"))
		}
	}
	if req.Format == "" {
		req.Format = defaultFormat
	}
	if req.Width == 0 {
		req.Width = defaultWidth
	}
	if req.Height == 0 {
		req.Height = defaultHeight
	}
	if req.Fps == 0 {
		req.Fps = defaultFps
	}
	err := initCapture(req.Format, req.Width, req.Height, req.Fps)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, Response{Status: "error", Message: err.Error()})
		return
	}
	state.Format = req.Format
	state.Width = req.Width
	state.Height = req.Height
	state.Fps = req.Fps
	state.IsCapturing = true
	writeJSON(w, http.StatusOK, Response{Status: "ok", Message: "capture started"})
}

func handleStopCapture(w http.ResponseWriter, r *http.Request) {
	state.Lock()
	defer state.Unlock()
	if !state.IsCapturing {
		writeJSON(w, http.StatusOK, Response{Status: "ok", Message: "already stopped"})
		return
	}
	releaseCapture()
	state.IsCapturing = false
	writeJSON(w, http.StatusOK, Response{Status: "ok", Message: "capture stopped"})
}

func handleStream(w http.ResponseWriter, r *http.Request) {
	state.Lock()
	if !state.IsCapturing {
		state.Unlock()
		http.Error(w, "Camera not capturing. Start capture first.", http.StatusServiceUnavailable)
		return
	}
	format := state.Format
	width := state.Width
	height := state.Height
	fps := state.Fps
	state.Unlock()

	query := r.URL.Query()
	if qf := query.Get("format"); qf != "" {
		format = qf
	}
	if wstr := query.Get("width"); wstr != "" {
		if wval, err := strconv.Atoi(wstr); err == nil && wval > 0 {
			width = wval
		}
	}
	if hstr := query.Get("height"); hstr != "" {
		if hval, err := strconv.Atoi(hstr); err == nil && hval > 0 {
			height = hval
		}
	}
	if fpsstr := query.Get("fps"); fpsstr != "" {
		if fpsval, err := strconv.Atoi(fpsstr); err == nil && fpsval > 0 {
			fps = fpsval
		}
	}

	if format == "" || strings.ToUpper(format) == "MJPEG" {
		serveMJPEG(w, r, width, height, fps)
		return
	}
	http.Error(w, "Only MJPEG streaming is currently supported", http.StatusNotImplemented)
}

func serveMJPEG(w http.ResponseWriter, r *http.Request, width, height, fps int) {
	boundary := "mjpegstream"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")

	// Re-initialize capture if needed
	err := initCapture("MJPEG", width, height, fps)
	if err != nil {
		http.Error(w, "Camera error: "+err.Error(), http.StatusInternalServerError)
		return
	}

	img := gocv.NewMat()
	defer img.Close()

	delay := time.Duration(1000/fps) * time.Millisecond

	for {
		if ok := capture.Read(&img); !ok || img.Empty() {
			time.Sleep(delay)
			continue
		}

		buf, err := matToJPEG(&img)
		if err != nil {
			continue
		}

		mw := multipart.NewWriter(w)
		mw.SetBoundary(boundary)

		// Write part manually (instead of mw.CreatePart, to avoid closing the multipart too early)
		fmt.Fprintf(w, "--%s\r\n", boundary)
		fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
		fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(buf))
		w.Write(buf)
		fmt.Fprintf(w, "\r\n")

		flusher, ok := w.(http.Flusher)
		if ok {
			flusher.Flush()
		}
		time.Sleep(delay)

		select {
		case <-r.Context().Done():
			return
		default:
		}
	}
}

func matToJPEG(img *gocv.Mat) ([]byte, error) {
	imgCopy, err := img.ToImage()
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	err = jpeg.Encode(&buf, imgCopy.(image.Image), nil)
	if err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeJSON(w http.ResponseWriter, status int, obj interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(obj)
}