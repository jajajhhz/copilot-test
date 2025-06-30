package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/blackjack/webcam"
)

type CameraConfig struct {
	DevicePath string
	Format     string
	Width      uint32
	Height     uint32
	Fps        uint32
}

type CameraState struct {
	sync.Mutex
	webcam         *webcam.Webcam
	streaming      bool
	capturing      bool
	format         uint32
	width, height  uint32
	fps            uint32
	formatDesc     webcam.PixelFormat
	stopStreamChan chan struct{}
}

var (
	cameraState = &CameraState{}
)

func getenv(key string, def string) string {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	return val
}

func getenvUint32(key string, def uint32) uint32 {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	res, err := strconv.ParseUint(val, 10, 32)
	if err != nil {
		return def
	}
	return uint32(res)
}

func getenvInt(key string, def int) int {
	val := os.Getenv(key)
	if val == "" {
		return def
	}
	res, err := strconv.Atoi(val)
	if err != nil {
		return def
	}
	return res
}

func findFormatDesc(cam *webcam.Webcam, format string) (webcam.PixelFormat, error) {
	formats := cam.GetSupportedFormats()
	for f, desc := range formats {
		if strings.Contains(strings.ToUpper(desc), strings.ToUpper(format)) {
			return f, nil
		}
	}
	return 0, errors.New("format not supported")
}

func openCamera(cfg CameraConfig) (*webcam.Webcam, webcam.PixelFormat, error) {
	cam, err := webcam.Open(cfg.DevicePath)
	if err != nil {
		return nil, 0, err
	}
	formatDesc, err := findFormatDesc(cam, cfg.Format)
	if err != nil {
		cam.Close()
		return nil, 0, err
	}
	framesizes := cam.GetSupportedFrameSizes(formatDesc)
	var (
		bestW, bestH uint32
		bestDist     uint32 = ^uint32(0)
	)
	for _, size := range framesizes {
		if size.MaxWidth >= cfg.Width && size.MaxHeight >= cfg.Height {
			dist := (size.MaxWidth-cfg.Width)*(size.MaxWidth-cfg.Width) + (size.MaxHeight-cfg.Height)*(size.MaxHeight-cfg.Height)
			if dist < bestDist {
				bestW = size.MaxWidth
				bestH = size.MaxHeight
				bestDist = dist
			}
		}
	}
	if bestW == 0 || bestH == 0 {
		bestW = framesizes[0].MaxWidth
		bestH = framesizes[0].MaxHeight
	}
	f, w, h, err := cam.SetImageFormat(formatDesc, bestW, bestH)
	if err != nil {
		cam.Close()
		return nil, 0, err
	}
	cam.SetFPS(cfg.Fps)
	return cam, f, nil
}

func startVideoCapture(cfg CameraConfig) error {
	cameraState.Lock()
	defer cameraState.Unlock()
	if cameraState.capturing {
		return nil
	}
	cam, formatDesc, err := openCamera(cfg)
	if err != nil {
		return err
	}
	if _, err := cam.StartStreaming(); err != nil {
		cam.Close()
		return err
	}
	cameraState.webcam = cam
	cameraState.capturing = true
	cameraState.formatDesc = formatDesc
	cameraState.width = cfg.Width
	cameraState.height = cfg.Height
	cameraState.fps = cfg.Fps
	return nil
}

func stopVideoCapture() error {
	cameraState.Lock()
	defer cameraState.Unlock()
	if !cameraState.capturing {
		return nil
	}
	if cameraState.streaming && cameraState.stopStreamChan != nil {
		close(cameraState.stopStreamChan)
		cameraState.stopStreamChan = nil
	}
	cameraState.streaming = false
	cameraState.capturing = false
	if cameraState.webcam != nil {
		cameraState.webcam.StopStreaming()
		cameraState.webcam.Close()
		cameraState.webcam = nil
	}
	return nil
}

func streamMJPEG(w http.ResponseWriter, r *http.Request, cfg CameraConfig) {
	cameraState.Lock()
	if !cameraState.capturing {
		cameraState.Unlock()
		http.Error(w, "Camera not capturing", http.StatusServiceUnavailable)
		return
	}
	if cameraState.streaming {
		cameraState.Unlock()
		http.Error(w, "Camera already streaming", http.StatusConflict)
		return
	}
	cameraState.streaming = true
	cameraState.stopStreamChan = make(chan struct{})
	stopChan := cameraState.stopStreamChan
	cam := cameraState.webcam
	cameraState.Unlock()

	boundary := "frame"
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "close")

	frameInterval := time.Duration(1e9 / cfg.Fps)
	ticker := time.NewTicker(frameInterval)
	defer ticker.Stop()

	for {
		select {
		case <-stopChan:
			return
		case <-ticker.C:
			err := cam.WaitForFrame(2)
			switch err.(type) {
			case nil:
			case *webcam.Timeout:
				continue
			default:
				return
			}
			frame, err := cam.ReadFrame()
			if len(frame) == 0 {
				continue
			}
			img, err := yuyvToJPEG(frame, cfg.Width, cfg.Height)
			if err != nil {
				continue
			}
			var buf bytes.Buffer
			if err := jpeg.Encode(&buf, img, nil); err != nil {
				continue
			}
			fmt.Fprintf(w, "--%s\r\n", boundary)
			fmt.Fprintf(w, "Content-Type: image/jpeg\r\n")
			fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", buf.Len())
			w.Write(buf.Bytes())
			fmt.Fprintf(w, "\r\n")
			flusher, ok := w.(http.Flusher)
			if ok {
				flusher.Flush()
			}
		case <-r.Context().Done():
			stopVideoCapture()
			return
		}
	}
}

func yuyvToJPEG(frame []byte, width, height uint32) (image.Image, error) {
	img := image.NewRGBA(image.Rect(0, 0, int(width), int(height)))
	i := 0
	for y := 0; y < int(height); y++ {
		for x := 0; x < int(width); x += 2 {
			if i+4 > len(frame) {
				return nil, errors.New("frame size mismatch")
			}
			y0 := int(frame[i+0])
			u := int(frame[i+1])
			y1 := int(frame[i+2])
			v := int(frame[i+3])

			r0, g0, b0 := yuvToRgb(y0, u, v)
			r1, g1, b1 := yuvToRgb(y1, u, v)

			img.SetRGBA(x, y, image.RGBAColor{uint8(r0), uint8(g0), uint8(b0), 255})
			if x+1 < int(width) {
				img.SetRGBA(x+1, y, image.RGBAColor{uint8(r1), uint8(g1), uint8(b1), 255})
			}
			i += 4
		}
	}
	return img, nil
}

func yuvToRgb(y, u, v int) (int, int, int) {
	c := y - 16
	d := u - 128
	e := v - 128
	r := (298*c + 409*e + 128) >> 8
	g := (298*c - 100*d - 208*e + 128) >> 8
	b := (298*c + 516*d + 128) >> 8
	if r < 0 {
		r = 0
	}
	if r > 255 {
		r = 255
	}
	if g < 0 {
		g = 0
	}
	if g > 255 {
		g = 255
	}
	if b < 0 {
		b = 0
	}
	if b > 255 {
		b = 255
	}
	return r, g, b
}

func handleVideoStream(w http.ResponseWriter, r *http.Request) {
	cfg := getConfigFromQuery(r)
	if err := startVideoCapture(cfg); err != nil {
		http.Error(w, "Failed to start video: "+err.Error(), http.StatusInternalServerError)
		return
	}
	streamMJPEG(w, r, cfg)
}

func handleStream(w http.ResponseWriter, r *http.Request) {
	handleVideoStream(w, r)
}

func handleCaptureStart(w http.ResponseWriter, r *http.Request) {
	cfg := getConfigFromRequest(r)
	if err := startVideoCapture(cfg); err != nil {
		http.Error(w, "Failed to start video: "+err.Error(), http.StatusInternalServerError)
		return
	}
	resp := map[string]string{"status": "started"}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func handleCaptureStop(w http.ResponseWriter, r *http.Request) {
	if err := stopVideoCapture(); err != nil {
		http.Error(w, "Failed to stop video: "+err.Error(), http.StatusInternalServerError)
		return
	}
	resp := map[string]string{"status": "stopped"}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func handleVideoStart(w http.ResponseWriter, r *http.Request) {
	handleCaptureStart(w, r)
}

func handleVideoStop(w http.ResponseWriter, r *http.Request) {
	handleCaptureStop(w, r)
}

func getConfigFromQuery(r *http.Request) CameraConfig {
	devicePath := getenv("CAMERA_DEVICE_PATH", "/dev/video0")
	format := r.URL.Query().Get("format")
	if format == "" {
		format = getenv("CAMERA_FORMAT", "YUYV")
	}
	width := getenvUint32("CAMERA_WIDTH", 640)
	height := getenvUint32("CAMERA_HEIGHT", 480)
	fps := getenvUint32("CAMERA_FPS", 15)
	if w := r.URL.Query().Get("width"); w != "" {
		if wi, err := strconv.ParseUint(w, 10, 32); err == nil {
			width = uint32(wi)
		}
	}
	if h := r.URL.Query().Get("height"); h != "" {
		if hi, err := strconv.ParseUint(h, 10, 32); err == nil {
			height = uint32(hi)
		}
	}
	if f := r.URL.Query().Get("fps"); f != "" {
		if fi, err := strconv.ParseUint(f, 10, 32); err == nil {
			fps = uint32(fi)
		}
	}
	return CameraConfig{
		DevicePath: devicePath,
		Format:     format,
		Width:      width,
		Height:     height,
		Fps:        fps,
	}
}

func getConfigFromRequest(r *http.Request) CameraConfig {
	cfg := getConfigFromQuery(r)
	if r.Method == "POST" && strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
		var data struct {
			DevicePath string `json:"devicePath"`
			Format     string `json:"format"`
			Width      uint32 `json:"width"`
			Height     uint32 `json:"height"`
			Fps        uint32 `json:"fps"`
		}
		defer r.Body.Close()
		json.NewDecoder(r.Body).Decode(&data)
		if data.DevicePath != "" {
			cfg.DevicePath = data.DevicePath
		}
		if data.Format != "" {
			cfg.Format = data.Format
		}
		if data.Width != 0 {
			cfg.Width = data.Width
		}
		if data.Height != 0 {
			cfg.Height = data.Height
		}
		if data.Fps != 0 {
			cfg.Fps = data.Fps
		}
	}
	return cfg
}

func main() {
	serverHost := getenv("SHIFU_USB_CAMERA_HTTP_SERVER_HOST", "")
	serverPort := getenv("SHIFU_USB_CAMERA_HTTP_SERVER_PORT", "8080")
	http.HandleFunc("/video/stream", handleVideoStream)
	http.HandleFunc("/stream", handleStream)
	http.HandleFunc("/capture/start", handleCaptureStart)
	http.HandleFunc("/capture/stop", handleCaptureStop)
	http.HandleFunc("/video/start", handleVideoStart)
	http.HandleFunc("/video/stop", handleVideoStop)
	addr := fmt.Sprintf("%s:%s", serverHost, serverPort)
	log.Printf("USB Camera HTTP driver starting at %s\n", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}