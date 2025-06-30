```go
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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

	"github.com/blackjack/webcam"
)

type VideoFormat string

const (
	FormatMJPEG VideoFormat = "MJPEG"
	FormatYUYV  VideoFormat = "YUYV"
)

type VideoState struct {
	mu       sync.Mutex
	running  bool
	format   VideoFormat
	width    uint32
	height   uint32
	fps      float64
	cancel   context.CancelFunc
}

var videoState = &VideoState{}

func getEnv(key, fallback string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return fallback
}

func parseUint32(s string, fallback uint32) uint32 {
	if v, err := strconv.ParseUint(s, 10, 32); err == nil {
		return uint32(v)
	}
	return fallback
}

func parseFloat64(s string, fallback float64) float64 {
	if v, err := strconv.ParseFloat(s, 64); err == nil {
		return v
	}
	return fallback
}

func getVideoFormat(f string) VideoFormat {
	switch strings.ToUpper(f) {
	case "MJPEG":
		return FormatMJPEG
	case "YUYV":
		return FormatYUYV
	default:
		return FormatMJPEG
	}
}

func getSupportedFormat(cam *webcam.Webcam, format VideoFormat) (webcam.PixelFormat, error) {
	formats := cam.GetSupportedFormats()
	for pf, desc := range formats {
		if (format == FormatMJPEG && strings.Contains(strings.ToUpper(desc), "MJPEG")) ||
			(format == FormatYUYV && strings.Contains(strings.ToUpper(desc), "YUYV")) {
			return pf, nil
		}
	}
	return 0, errors.New("requested format not supported by camera")
}

func getSupportedFrameSize(cam *webcam.Webcam, pf webcam.PixelFormat, width, height uint32) (webcam.FrameSize, error) {
	frames := cam.GetSupportedFrameSizes(pf)
	var best webcam.FrameSize
	bestDiff := uint32(0xFFFFFFFF)
	for _, f := range frames {
		diff := absDiff(f.MaxWidth, width) + absDiff(f.MaxHeight, height)
		if diff < bestDiff {
			bestDiff = diff
			best = f
		}
	}
	if best.MaxWidth == 0 || best.MaxHeight == 0 {
		return webcam.FrameSize{}, errors.New("no valid frame size found")
	}
	return best, nil
}

func absDiff(a, b uint32) uint32 {
	if a > b {
		return a - b
	}
	return b - a
}

func openCamera(dev string, format VideoFormat, width, height uint32, fps float64) (*webcam.Webcam, webcam.PixelFormat, error) {
	cam, err := webcam.Open(dev)
	if err != nil {
		return nil, 0, err
	}

	pf, err := getSupportedFormat(cam, format)
	if err != nil {
		cam.Close()
		return nil, 0, err
	}

	fs, err := getSupportedFrameSize(cam, pf, width, height)
	if err != nil {
		cam.Close()
		return nil, 0, err
	}

	// FPS: try to set closest supported
	intervals := cam.GetSupportedFrameIntervals(pf, fs)
	bestInterval := intervals[0]
	bestDiff := absDiff(uint32(float64(1e6)/float64(bestInterval.Numerator)/float64(bestInterval.Denominator)), uint32(fps))
	for _, i := range intervals {
		ival := float64(i.Denominator) / float64(i.Numerator)
		diff := absDiff(uint32(ival*1000), uint32(1000/fps))
		if diff < bestDiff {
			bestDiff = diff
			bestInterval = i
		}
	}

	_, _, _, err = cam.SetImageFormat(pf, fs.MaxWidth, fs.MaxHeight)
	if err != nil {
		cam.Close()
		return nil, 0, err
	}
	if err := cam.StartStreaming(); err != nil {
		cam.Close()
		return nil, 0, err
	}
	return cam, pf, nil
}

// API Handlers

func handleVideoStart(w http.ResponseWriter, r *http.Request) {
	format := getVideoFormat(r.URL.Query().Get("format"))
	width := parseUint32(r.URL.Query().Get("width"), parseUint32(getEnv("CAMERA_WIDTH", "640"), 640))
	height := parseUint32(r.URL.Query().Get("height"), parseUint32(getEnv("CAMERA_HEIGHT", "480"), 480))
	fps := parseFloat64(r.URL.Query().Get("fps"), parseFloat64(getEnv("CAMERA_FPS", "15"), 15))

	videoState.mu.Lock()
	defer videoState.mu.Unlock()
	if videoState.running {
		writeJSON(w, http.StatusOK, map[string]string{"status": "already running"})
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	videoState.running = true
	videoState.format = format
	videoState.width = width
	videoState.height = height
	videoState.fps = fps
	videoState.cancel = cancel
	writeJSON(w, http.StatusOK, map[string]string{"status": "started"})
	go func() {
		<-ctx.Done()
	}()
}

func handleVideoStop(w http.ResponseWriter, r *http.Request) {
	videoState.mu.Lock()
	defer videoState.mu.Unlock()
	if !videoState.running {
		writeJSON(w, http.StatusOK, map[string]string{"status": "already stopped"})
		return
	}
	if videoState.cancel != nil {
		videoState.cancel()
	}
	videoState.running = false
	videoState.cancel = nil
	writeJSON(w, http.StatusOK, map[string]string{"status": "stopped"})
}

func handleCaptureStart(w http.ResponseWriter, r *http.Request) {
	handleVideoStart(w, r)
}

func handleCaptureStop(w http.ResponseWriter, r *http.Request) {
	handleVideoStop(w, r)
}

// /video/stream and /stream
func handleStream(w http.ResponseWriter, r *http.Request) {
	videoState.mu.Lock()
	running := videoState.running
	format := videoState.format
	width := videoState.width
	height := videoState.height
	fps := videoState.fps
	videoState.mu.Unlock()

	if !running {
		http.Error(w, "video is not running, start video first", http.StatusBadRequest)
		return
	}

	devicePath := getEnv("CAMERA_DEVICE", "/dev/video0")
	cam, pf, err := openCamera(devicePath, format, width, height, fps)
	if err != nil {
		http.Error(w, "failed to open camera: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer cam.Close()

	if pf == webcam.PixelFormat(1196444237) { // MJPG fourcc
		w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
		streamMJPEG(cam, w)
	} else {
		w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
		streamYUYVasMJPEG(cam, width, height, w)
	}
}

func streamMJPEG(cam *webcam.Webcam, w http.ResponseWriter) {
	for {
		err := cam.WaitForFrame(5)
		switch err.(type) {
		case nil:
			frame, err := cam.ReadFrame()
			if err != nil {
				continue
			}
			if len(frame) == 0 {
				continue
			}
			w.Write([]byte("--frame\r\nContent-Type: image/jpeg\r\n\r\n"))
			w.Write(frame)
			w.Write([]byte("\r\n"))
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		case *webcam.Timeout:
			continue
		default:
			return
		}
	}
}

// YUYV to JPEG
func streamYUYVasMJPEG(cam *webcam.Webcam, width, height uint32, w http.ResponseWriter) {
	for {
		err := cam.WaitForFrame(5)
		switch err.(type) {
		case nil:
			frame, err := cam.ReadFrame()
			if err != nil {
				continue
			}
			if len(frame) == 0 {
				continue
			}
			img := yuyvToImage(frame, int(width), int(height))
			buf := new(bytes.Buffer)
			jpeg.Encode(buf, img, nil)
			w.Write([]byte("--frame\r\nContent-Type: image/jpeg\r\n\r\n"))
			w.Write(buf.Bytes())
			w.Write([]byte("\r\n"))
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		case *webcam.Timeout:
			continue
		default:
			return
		}
	}
}

// Convert YUYV (YUV422) to image.Image
func yuyvToImage(in []byte, width, height int) image.Image {
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	for i, j := 0, 0; i+3 < len(in) && j+1 < width*height; i, j = i+4, j+2 {
		y0 := in[i+0]
		u := in[i+1]
		y1 := in[i+2]
		v := in[i+3]
		img.Set(j%width, j/width, yuvToRGB(y0, u, v))
		img.Set((j+1)%width, (j+1)/width, yuvToRGB(y1, u, v))
	}
	return img
}

// YUV to RGBA
func yuvToRGB(y, u, v byte) image.Color {
	c := int(y) - 16
	d := int(u) - 128
	e := int(v) - 128

	r := clip((298*c + 409*e + 128) >> 8)
	g := clip((298*c - 100*d - 208*e + 128) >> 8)
	b := clip((298*c + 516*d + 128) >> 8)
	return image.RGBAColor{uint8(r), uint8(g), uint8(b), 255}
}

func clip(x int) int {
	if x < 0 {
		return 0
	}
	if x > 255 {
		return 255
	}
	return x
}

func writeJSON(w http.ResponseWriter, code int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(data)
}

func main() {
	addr := getEnv("HTTP_SERVER_HOST", "") + ":" + getEnv("HTTP_SERVER_PORT", "8080")

	http.HandleFunc("/video/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleVideoStart(w, r)
	})

	http.HandleFunc("/video/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleVideoStop(w, r)
	})

	http.HandleFunc("/capture/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleCaptureStart(w, r)
	})

	http.HandleFunc("/capture/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleCaptureStop(w, r)
	})

	http.HandleFunc("/video/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleStream(w, r)
	})

	http.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		handleStream(w, r)
	})

	log.Printf("Starting USB Camera HTTP driver server at %s", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("failed to start server: %v", err)
	}
}
```
**Note:**  
- Requires the [github.com/blackjack/webcam](https://github.com/blackjack/webcam) Go package for V4L2 camera access.
- Configurable via environment variables:
  - `HTTP_SERVER_HOST`, `HTTP_SERVER_PORT` (HTTP server bind address/port)
  - `CAMERA_DEVICE` (default `/dev/video0`)
  - `CAMERA_WIDTH`/`CAMERA_HEIGHT`/`CAMERA_FPS` (defaults: 640/480/15)
- All endpoints are available as described and stream via HTTP (MJPEG), accessible in browser or command-line tools like `curl`.