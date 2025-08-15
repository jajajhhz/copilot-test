```go
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/blackjack/webcam"
)

type CameraConfig struct {
	DevicePath  string
	Format      string // "MJPEG" or "YUYV"
	Width       uint32
	Height      uint32
	FPS         uint32
}

type CameraState struct {
	mu        sync.Mutex
	running   bool
	webcam    *webcam.Webcam
	format    webcam.PixelFormat
	width     uint32
	height    uint32
	fps       uint32
	formatStr string
}

var (
	cameraConfig CameraConfig
	cameraState  CameraState
)

// --- ENV VARS ---
func loadEnvConfig() error {
	cameraConfig.DevicePath = os.Getenv("DEVICE_PATH")
	if cameraConfig.DevicePath == "" {
		cameraConfig.DevicePath = "/dev/video0"
	}
	cameraConfig.Format = strings.ToUpper(os.Getenv("CAMERA_FORMAT"))
	if cameraConfig.Format == "" {
		cameraConfig.Format = "MJPEG"
	}
	width := os.Getenv("CAMERA_WIDTH")
	height := os.Getenv("CAMERA_HEIGHT")
	fps := os.Getenv("CAMERA_FPS")
	cameraConfig.Width = 640
	cameraConfig.Height = 480
	cameraConfig.FPS = 15
	if width != "" {
		if w, err := strconv.Atoi(width); err == nil {
			cameraConfig.Width = uint32(w)
		}
	}
	if height != "" {
		if h, err := strconv.Atoi(height); err == nil {
			cameraConfig.Height = uint32(h)
		}
	}
	if fps != "" {
		if f, err := strconv.Atoi(fps); err == nil {
			cameraConfig.FPS = uint32(f)
		}
	}
	return nil
}

// --- CAMERA CONTROL ---
func openCamera() error {
	cameraState.mu.Lock()
	defer cameraState.mu.Unlock()
	if cameraState.running {
		return nil
	}
	cam, err := webcam.Open(cameraConfig.DevicePath)
	if err != nil {
		return err
	}
	formatDesc := cam.GetSupportedFormats()
	var pixFmt webcam.PixelFormat
	for k, v := range formatDesc {
		if (cameraConfig.Format == "MJPEG" && strings.Contains(v, "MJPEG")) ||
			(cameraConfig.Format == "YUYV" && strings.Contains(v, "YUYV")) {
			pixFmt = k
			break
		}
	}
	if pixFmt == 0 {
		cam.Close()
		return errors.New("unsupported camera format")
	}
	width, height, fps, err := selectFrameSizeAndFPS(cam, pixFmt)
	if err != nil {
		cam.Close()
		return err
	}
	_, _, _, err = cam.SetImageFormat(pixFmt, width, height)
	if err != nil {
		cam.Close()
		return err
	}
	err = cam.SetFramerate(fps)
	if err != nil {
		cam.Close()
		return err
	}
	if _, err := cam.StartStreaming(); err != nil {
		cam.Close()
		return err
	}
	cameraState.webcam = cam
	cameraState.format = pixFmt
	cameraState.width = width
	cameraState.height = height
	cameraState.fps = fps
	cameraState.formatStr = cameraConfig.Format
	cameraState.running = true
	return nil
}

func selectFrameSizeAndFPS(cam *webcam.Webcam, pixFmt webcam.PixelFormat) (uint32, uint32, uint32, error) {
	framesizes := cam.GetSupportedFrameSizes(pixFmt)
	var width, height uint32
	for _, size := range framesizes {
		if size.MaxWidth >= cameraConfig.Width && size.MaxHeight >= cameraConfig.Height {
			width = cameraConfig.Width
			height = cameraConfig.Height
			break
		}
	}
	if width == 0 || height == 0 {
		// fallback: first available
		width = framesizes[0].MaxWidth
		height = framesizes[0].MaxHeight
	}
	// FPS selection
	fps := cameraConfig.FPS
	return width, height, fps, nil
}

func closeCamera() error {
	cameraState.mu.Lock()
	defer cameraState.mu.Unlock()
	if cameraState.running && cameraState.webcam != nil {
		cameraState.webcam.StopStreaming()
		cameraState.webcam.Close()
		cameraState.webcam = nil
		cameraState.running = false
	}
	return nil
}

// --- HTTP HANDLERS ---
func jsonResponse(w http.ResponseWriter, code int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(data)
}

func handleStartCapture(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	// Optional format and resolution via query or body
	format := r.URL.Query().Get("format")
	width := r.URL.Query().Get("width")
	height := r.URL.Query().Get("height")
	fps := r.URL.Query().Get("fps")
	if format != "" {
		cameraConfig.Format = strings.ToUpper(format)
	}
	if width != "" {
		if wv, err := strconv.Atoi(width); err == nil {
			cameraConfig.Width = uint32(wv)
		}
	}
	if height != "" {
		if hv, err := strconv.Atoi(height); err == nil {
			cameraConfig.Height = uint32(hv)
		}
	}
	if fps != "" {
		if f, err := strconv.Atoi(fps); err == nil {
			cameraConfig.FPS = uint32(f)
		}
	}
	if err := openCamera(); err != nil {
		jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, http.StatusOK, map[string]string{"status": "capture started"})
}

func handleStartVideo(w http.ResponseWriter, r *http.Request) {
	handleStartCapture(w, r)
}

func handleStopCapture(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	if err := closeCamera(); err != nil {
		jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, http.StatusOK, map[string]string{"status": "capture stopped"})
}

func handleStopVideo(w http.ResponseWriter, r *http.Request) {
	handleStopCapture(w, r)
}

// --- STREAMING ---
func handleStream(w http.ResponseWriter, r *http.Request) {
	cameraState.mu.Lock()
	running := cameraState.running
	cameraState.mu.Unlock()
	if !running {
		http.Error(w, "Camera is not capturing", http.StatusServiceUnavailable)
		return
	}
	format := r.URL.Query().Get("format")
	if format == "" {
		format = cameraState.formatStr
	}
	format = strings.ToUpper(format)
	if format != "MJPEG" && format != "YUYV" {
		http.Error(w, "Only MJPEG or YUYV supported", http.StatusBadRequest)
		return
	}
	if format == "MJPEG" {
		streamMJPEG(w, r)
	} else {
		streamYUYV(w, r)
	}
}

// /video/stream and /stream are the same
func handleVideoStream(w http.ResponseWriter, r *http.Request) {
	handleStream(w, r)
}

func streamMJPEG(w http.ResponseWriter, r *http.Request) {
	boundary := "mjpegstream"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}
	cameraState.mu.Lock()
	cam := cameraState.webcam
	width := cameraState.width
	height := cameraState.height
	cameraState.mu.Unlock()
	for {
		err := cam.WaitForFrame(5)
		if err != nil && err != webcam.ErrTimeout {
			break
		}
		frame, err := cam.ReadFrame()
		if len(frame) == 0 {
			continue
		}
		if err != nil && err != webcam.ErrTimeout {
			break
		}
		// MJPEG frame is JPEG already
		fmt.Fprintf(w, "--%s\r\n", boundary)
		fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
		fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(frame))
		w.Write(frame)
		fmt.Fprintf(w, "\r\n")
		flusher.Flush()
	}
}

func streamYUYV(w http.ResponseWriter, r *http.Request) {
	boundary := "yuyvstream"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}
	cameraState.mu.Lock()
	cam := cameraState.webcam
	width := int(cameraState.width)
	height := int(cameraState.height)
	cameraState.mu.Unlock()
	for {
		err := cam.WaitForFrame(5)
		if err != nil && err != webcam.ErrTimeout {
			break
		}
		frame, err := cam.ReadFrame()
		if len(frame) == 0 {
			continue
		}
		if err != nil && err != webcam.ErrTimeout {
			break
		}
		img := yuyvToImage(frame, width, height)
		var buf []byte
		jpegBuf := &buf
		jpegWriter := &bufferWriter{buf: jpegBuf}
		_ = jpeg.Encode(jpegWriter, img, nil)
		fmt.Fprintf(w, "--%s\r\n", boundary)
		fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
		fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(*jpegBuf))
		w.Write(*jpegBuf)
		fmt.Fprintf(w, "\r\n")
		flusher.Flush()
	}
}

type bufferWriter struct {
	buf *[]byte
}

func (w *bufferWriter) Write(p []byte) (int, error) {
	*w.buf = append(*w.buf, p...)
	return len(p), nil
}

// YUYV422 to image.Image (RGB)
func yuyvToImage(frame []byte, width, height int) image.Image {
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	i := 0
	for y := 0; y < height; y++ {
		for x := 0; x < width; x += 2 {
			if i+4 > len(frame) {
				break
			}
			y0 := int(frame[i+0])
			u := int(frame[i+1])
			y1 := int(frame[i+2])
			v := int(frame[i+3])
			img.Set(x, y, yuvToRGB(y0, u, v))
			if x+1 < width {
				img.Set(x+1, y, yuvToRGB(y1, u, v))
			}
			i += 4
		}
	}
	return img
}

func yuvToRGB(y, u, v int) image.Color {
	c := y - 16
	d := u - 128
	e := v - 128
	r := clamp((298*c+409*e+128)>>8, 0, 255)
	g := clamp((298*c-100*d-208*e+128)>>8, 0, 255)
	b := clamp((298*c+516*d+128)>>8, 0, 255)
	return image.RGBAColor{R: uint8(r), G: uint8(g), B: uint8(b), A: 255}
}

func clamp(val, min, max int) int {
	if val < min {
		return min
	}
	if val > max {
		return max
	}
	return val
}

// --- MAIN ---
func main() {
	if err := loadEnvConfig(); err != nil {
		log.Fatalf("Config error: %v", err)
	}
	serverHost := os.Getenv("SERVER_HOST")
	serverPort := os.Getenv("SERVER_PORT")
	if serverPort == "" {
		serverPort = "8080"
	}
	addr := serverHost + ":" + serverPort

	http.HandleFunc("/capture/start", handleStartCapture)
	http.HandleFunc("/video/start", handleStartVideo)
	http.HandleFunc("/capture/stop", handleStopCapture)
	http.HandleFunc("/video/stop", handleStopVideo)
	http.HandleFunc("/video/stream", handleVideoStream)
	http.HandleFunc("/stream", handleStream)

	log.Printf("USB Camera HTTP driver starting on %s", addr)
	log.Printf("Device path: %s, Format: %s, Resolution: %dx%d, FPS: %d",
		cameraConfig.DevicePath, cameraConfig.Format, cameraConfig.Width, cameraConfig.Height, cameraConfig.FPS)

	log.Fatal(http.ListenAndServe(addr, nil))
}
```