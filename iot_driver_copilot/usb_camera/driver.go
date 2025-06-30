package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"image"
	"image/jpeg"
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
	FormatMJPEG VideoFormat = "mjpeg"
	FormatYUYV  VideoFormat = "yuyv"
	FormatH264  VideoFormat = "h264"
	FormatRaw   VideoFormat = "raw"
)

type CaptureState struct {
	sync.Mutex
	Started     bool
	Format      VideoFormat
	Width       int
	Height      int
	DeviceID    int
	FrameRate   int
	Mat         *gocv.Mat
	Webcam      *gocv.VideoCapture
	Subscribers map[chan []byte]struct{}
}

var (
	captureState = &CaptureState{
		Started:     false,
		Format:      FormatMJPEG,
		Width:       640,
		Height:      480,
		DeviceID:    0,
		FrameRate:   15,
		Subscribers: make(map[chan []byte]struct{}),
	}
)

func getenvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return def
}

func getenvStr(key string, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getenvFormat(key string, def VideoFormat) VideoFormat {
	v := strings.ToLower(os.Getenv(key))
	switch v {
	case "mjpeg":
		return FormatMJPEG
	case "yuyv":
		return FormatYUYV
	case "h264":
		return FormatH264
	case "raw":
		return FormatRaw
	}
	return def
}

func startCapture(format VideoFormat, width, height, deviceID, frameRate int) error {
	captureState.Lock()
	defer captureState.Unlock()

	if captureState.Started {
		return nil
	}
	webcam, err := gocv.OpenVideoCapture(deviceID)
	if err != nil {
		return errors.New("failed to open video capture device")
	}
	webcam.Set(gocv.VideoCaptureFrameWidth, float64(width))
	webcam.Set(gocv.VideoCaptureFrameHeight, float64(height))
	webcam.Set(gocv.VideoCaptureFPS, float64(frameRate))

	ok, err := webcam.Read(&gocv.Mat{})
	if !ok || err != nil {
		webcam.Close()
		return errors.New("failed to read from camera")
	}

	captureState.Webcam = webcam
	captureState.Format = format
	captureState.Width = width
	captureState.Height = height
	captureState.DeviceID = deviceID
	captureState.FrameRate = frameRate
	captureState.Started = true

	go streamFrames()
	return nil
}

func stopCapture() {
	captureState.Lock()
	defer captureState.Unlock()
	if captureState.Webcam != nil {
		captureState.Webcam.Close()
	}
	captureState.Started = false
	for ch := range captureState.Subscribers {
		close(ch)
		delete(captureState.Subscribers, ch)
	}
}

func streamFrames() {
	mat := gocv.NewMat()
	defer mat.Close()

	ticker := time.NewTicker(time.Duration(1000/captureState.FrameRate) * time.Millisecond)
	defer ticker.Stop()

	for captureState.Started {
		<-ticker.C
		if ok := captureState.Webcam.Read(&mat); !ok || mat.Empty() {
			continue
		}
		buf, err := matToMJPEG(mat)
		if err != nil {
			continue
		}
		captureState.Lock()
		for ch := range captureState.Subscribers {
			select {
			case ch <- buf:
			default:
			}
		}
		captureState.Unlock()
	}
}

func matToMJPEG(mat gocv.Mat) ([]byte, error) {
	img, err := mat.ToImage()
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	err = jpeg.Encode(&buf, img, nil)
	if err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func addSubscriber(ch chan []byte) {
	captureState.Lock()
	captureState.Subscribers[ch] = struct{}{}
	captureState.Unlock()
}

func removeSubscriber(ch chan []byte) {
	captureState.Lock()
	delete(captureState.Subscribers, ch)
	captureState.Unlock()
}

// API Handlers

func handleStartCapture(w http.ResponseWriter, r *http.Request) {
	format := getenvFormat("VIDEO_FORMAT", FormatMJPEG)
	width := getenvInt("CAMERA_WIDTH", 640)
	height := getenvInt("CAMERA_HEIGHT", 480)
	deviceID := getenvInt("CAMERA_DEVICE_ID", 0)
	frameRate := getenvInt("CAMERA_FRAMERATE", 15)

	if r.Method == "POST" {
		if err := r.ParseForm(); err == nil {
			if f := r.FormValue("format"); f != "" {
				format = getenvFormat("IGNORE", VideoFormat(f))
			}
			if wv := r.FormValue("width"); wv != "" {
				if wi, err := strconv.Atoi(wv); err == nil {
					width = wi
				}
			}
			if hv := r.FormValue("height"); hv != "" {
				if hi, err := strconv.Atoi(hv); err == nil {
					height = hi
				}
			}
			if frv := r.FormValue("framerate"); frv != "" {
				if fri, err := strconv.Atoi(frv); err == nil {
					frameRate = fri
				}
			}
			if dv := r.FormValue("device_id"); dv != "" {
				if di, err := strconv.Atoi(dv); err == nil {
					deviceID = di
				}
			}
		}
	}
	err := startCapture(format, width, height, deviceID, frameRate)
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		json.NewEncoder(w).Encode(map[string]interface{}{"status": "error", "error": err.Error()})
		return
	}
	json.NewEncoder(w).Encode(map[string]interface{}{"status": "started", "format": format, "width": width, "height": height, "framerate": frameRate})
}

func handleStopCapture(w http.ResponseWriter, r *http.Request) {
	stopCapture()
	json.NewEncoder(w).Encode(map[string]interface{}{"status": "stopped"})
}

func streamMJPEG(w http.ResponseWriter, r *http.Request) {
	w.Header().Add("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	ch := make(chan []byte, 30)
	addSubscriber(ch)
	defer removeSubscriber(ch)

	notify := w.(http.CloseNotifier).CloseNotify()
loop:
	for {
		select {
		case frame, ok := <-ch:
			if !ok {
				break loop
			}
			w.Write([]byte("--frame\r\n"))
			w.Write([]byte("Content-Type: image/jpeg\r\n"))
			w.Write([]byte("Content-Length: " + strconv.Itoa(len(frame)) + "\r\n\r\n"))
			w.Write(frame)
			w.Write([]byte("\r\n"))
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		case <-notify:
			break loop
		}
	}
}

// API Routing

func main() {
	host := getenvStr("HTTP_SERVER_HOST", "0.0.0.0")
	port := getenvStr("HTTP_SERVER_PORT", "8080")

	http.HandleFunc("/capture/start", handleStartCapture)
	http.HandleFunc("/video/start", handleStartCapture)
	http.HandleFunc("/capture/stop", handleStopCapture)
	http.HandleFunc("/video/stop", handleStopCapture)
	http.HandleFunc("/video/stream", streamMJPEG)
	http.HandleFunc("/stream", streamMJPEG)

	serverAddr := host + ":" + port
	log.Printf("USB Camera HTTP server starting at %s ...", serverAddr)
	log.Fatal(http.ListenAndServe(serverAddr, nil))
}