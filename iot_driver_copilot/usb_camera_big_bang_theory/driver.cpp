#include <iostream>
#include <cstdlib>
#include <thread>
#include <atomic>
#include <mutex>
#include <vector>
#include <condition_variable>
#include <cstring>
#include <map>
#include <sstream>
#include <algorithm>

#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <errno.h>

#define DEFAULT_HTTP_PORT 8080
#define DEFAULT_SERVER_HOST "0.0.0.0"
#define DEFAULT_VIDEO_DEVICE "/dev/video0"
#define MAX_CLIENTS 32
#define BOUNDARY "usb_cam_mjpeg_boundary"
#define MJPEG_FRAME_TIMEOUT_MS 100

// Utility: trim
static inline std::string trim(const std::string& s) {
    auto start = s.begin();
    while (start != s.end() && std::isspace(*start)) start++;
    auto end = s.end();
    do {
        end--;
    } while (std::distance(start, end) > 0 && std::isspace(*end));
    return std::string(start, end+1);
}

// Camera abstraction
class USBCamera {
public:
    USBCamera(const std::string& dev, int width, int height, const std::string& pixel_format)
    : devname(dev), width(width), height(height), pixel_format(pixel_format), fd(-1), buffers(nullptr), n_buffers(0), capturing(false) {}

    ~USBCamera() {
        stopCapture();
        closeDevice();
    }

    bool openDevice() {
        fd = open(devname.c_str(), O_RDWR | O_NONBLOCK, 0);
        return fd != -1;
    }

    void closeDevice() {
        if (fd != -1) {
            close(fd);
            fd = -1;
        }
    }

    bool initDevice() {
        struct v4l2_capability cap;
        if (ioctl(fd, VIDIOC_QUERYCAP, &cap) == -1) return false;

        struct v4l2_format fmt;
        memset(&fmt, 0, sizeof(fmt));
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;

        if (pixel_format == "mjpeg" || pixel_format == "MJPEG") {
            fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
        } else {
            fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
        }
        fmt.fmt.pix.width = width;
        fmt.fmt.pix.height = height;

        if (ioctl(fd, VIDIOC_S_FMT, &fmt) == -1) return false;

        // Request buffers
        struct v4l2_requestbuffers req;
        memset(&req, 0, sizeof(req));
        req.count = 4;
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        req.memory = V4L2_MEMORY_MMAP;
        if (ioctl(fd, VIDIOC_REQBUFS, &req) == -1) return false;
        if (req.count < 2) return false;

        buffers = (buffer*)calloc(req.count, sizeof(*buffers));
        if (!buffers) return false;

        for (n_buffers = 0; n_buffers < req.count; ++n_buffers) {
            struct v4l2_buffer buf;
            memset(&buf, 0, sizeof(buf));
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = n_buffers;
            if (ioctl(fd, VIDIOC_QUERYBUF, &buf) == -1) return false;

            buffers[n_buffers].length = buf.length;
            buffers[n_buffers].start = mmap(NULL, buf.length,
                                            PROT_READ | PROT_WRITE, MAP_SHARED,
                                            fd, buf.m.offset);
            if (buffers[n_buffers].start == MAP_FAILED) return false;
        }

        return true;
    }

    bool startCapture() {
        if (capturing) return true;

        for (unsigned int i = 0; i < n_buffers; ++i) {
            struct v4l2_buffer buf;
            memset(&buf, 0, sizeof(buf));
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (ioctl(fd, VIDIOC_QBUF, &buf) == -1) return false;
        }
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (ioctl(fd, VIDIOC_STREAMON, &type) == -1) return false;
        capturing = true;
        return true;
    }

    void stopCapture() {
        if (!capturing) return;
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        ioctl(fd, VIDIOC_STREAMOFF, &type);
        capturing = false;
    }

    bool readFrame(std::vector<uint8_t>& out, std::string* out_fmt = nullptr) {
        fd_set fds;
        struct timeval tv;
        FD_ZERO(&fds);
        FD_SET(fd, &fds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        int r = select(fd + 1, &fds, NULL, NULL, &tv);
        if (r == -1) return false;
        if (r == 0) return false;

        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (ioctl(fd, VIDIOC_DQBUF, &buf) == -1) return false;
        out.resize(buf.bytesused);
        memcpy(out.data(), buffers[buf.index].start, buf.bytesused);

        if (out_fmt) {
            if (pixel_format == "mjpeg" || pixel_format == "MJPEG")
                *out_fmt = "jpeg";
            else
                *out_fmt = "yuyv";
        }

        ioctl(fd, VIDIOC_QBUF, &buf);
        return true;
    }

    int getWidth() const { return width; }
    int getHeight() const { return height; }
    std::string getPixelFormat() const { return pixel_format; }

private:
    struct buffer {
        void   *start;
        size_t length;
    };
    std::string devname;
    int width, height;
    std::string pixel_format;
    int fd;
    buffer *buffers;
    unsigned int n_buffers;
    bool capturing;
};

// HTTP server
class HTTPServer {
public:
    HTTPServer(const std::string& host, int port, USBCamera& camera)
    : host(host), port(port), camera(camera), running(false), capture_on(false) {}

    void start() {
        running = true;
        int server_fd = socket(AF_INET, SOCK_STREAM, 0);
        if (server_fd < 0) exit(1);

        int enable = 1;
        setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(int));

        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = inet_addr(host.c_str());
        addr.sin_port = htons(port);

        if (bind(server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) exit(1);
        if (listen(server_fd, MAX_CLIENTS) < 0) exit(1);

        std::thread([this] { this->captureLoop(); }).detach();

        while (running) {
            struct sockaddr_in cli_addr;
            socklen_t cli_len = sizeof(cli_addr);
            int client_fd = accept(server_fd, (struct sockaddr*)&cli_addr, &cli_len);
            if (client_fd < 0) continue;
            std::thread([this, client_fd]() { this->handleClient(client_fd); }).detach();
        }
        close(server_fd);
    }

    void stop() {
        running = false;
    }

private:
    std::string host;
    int port;
    USBCamera& camera;
    std::atomic<bool> running;
    std::atomic<bool> capture_on;
    std::mutex capture_mutex;
    std::condition_variable capture_cv;

    // For /camera/stream clients
    struct StreamClient {
        int fd;
        std::string format;
        StreamClient(int f, const std::string& fmt) : fd(f), format(fmt) {}
    };
    std::mutex stream_clients_mutex;
    std::vector<StreamClient> stream_clients;

    // For frame buffer (single frame snapshot)
    std::mutex frame_mutex;
    std::vector<uint8_t> last_frame;
    std::string last_frame_fmt;

    // For reading HTTP requests
    static std::string readLine(int fd) {
        std::string line;
        char c;
        while (read(fd, &c, 1) == 1) {
            if (c == '\r') continue;
            if (c == '\n') break;
            line += c;
        }
        return line;
    }

    static void sendHTTP(int fd, const std::string& data) {
        size_t sent = 0;
        while (sent < data.size()) {
            ssize_t n = write(fd, data.data()+sent, data.size()-sent);
            if (n <= 0) break;
            sent += n;
        }
    }

    static void sendHTTP(int fd, const uint8_t* data, size_t sz) {
        size_t sent = 0;
        while (sent < sz) {
            ssize_t n = write(fd, data+sent, sz-sent);
            if (n <= 0) break;
            sent += n;
        }
    }

    // Parse query string
    static std::map<std::string, std::string> parseQuery(const std::string& uri) {
        std::map<std::string, std::string> params;
        auto qm = uri.find('?');
        if (qm == std::string::npos) return params;
        std::string q = uri.substr(qm+1);
        std::istringstream iss(q);
        std::string kv;
        while (std::getline(iss, kv, '&')) {
            auto eq = kv.find('=');
            if (eq != std::string::npos) {
                params[kv.substr(0,eq)] = kv.substr(eq+1);
            }
        }
        return params;
    }

    // Parse path without query
    static std::string uriPath(const std::string& uri) {
        size_t qm = uri.find('?');
        return uri.substr(0, qm == std::string::npos ? uri.size() : qm);
    }

    void handleClient(int client_fd) {
        std::string reqline = readLine(client_fd);
        if (reqline.empty()) {
            close(client_fd);
            return;
        }
        std::istringstream iss(reqline);
        std::string method, uri, ver;
        iss >> method >> uri >> ver;

        // Read headers (ignore for now)
        std::map<std::string, std::string> headers;
        while (true) {
            std::string h = readLine(client_fd);
            if (h.empty()) break;
            auto p = h.find(':');
            if (p != std::string::npos) {
                headers[trim(h.substr(0,p))] = trim(h.substr(p+1));
            }
        }

        // POST data
        std::string body;
        if (method == "POST") {
            auto it = headers.find("Content-Length");
            if (it != headers.end()) {
                int clen = std::stoi(it->second);
                body.resize(clen);
                int got = 0;
                while (got < clen) {
                    int r = read(client_fd, &body[got], clen-got);
                    if (r <= 0) break;
                    got += r;
                }
            }
        }

        if (method == "GET" && uriPath(uri) == "/camera/frame") {
            handleCameraFrame(client_fd, parseQuery(uri));
        } else if (method == "GET" && uriPath(uri) == "/camera/stream") {
            handleCameraStream(client_fd, parseQuery(uri));
        } else if (method == "POST" && uriPath(uri) == "/camera/start") {
            handleCameraStart(client_fd, body);
        } else if (method == "POST" && uriPath(uri) == "/camera/stop") {
            handleCameraStop(client_fd);
        } else {
            std::string resp = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n";
            sendHTTP(client_fd, resp);
            close(client_fd);
        }
    }

    // /camera/start
    void handleCameraStart(int fd, const std::string&) {
        {
            std::lock_guard<std::mutex> lk(capture_mutex);
            if (!capture_on) {
                capture_on = true;
                capture_cv.notify_all();
            }
        }
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"result\":\"started\"}";
        sendHTTP(fd, resp);
        close(fd);
    }

    // /camera/stop
    void handleCameraStop(int fd) {
        {
            std::lock_guard<std::mutex> lk(capture_mutex);
            capture_on = false;
        }
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"result\":\"stopped\"}";
        sendHTTP(fd, resp);
        close(fd);
    }

    // /camera/frame
    void handleCameraFrame(int fd, const std::map<std::string,std::string>& params) {
        std::string fmt = "jpeg";
        if (params.count("format")) fmt = params.at("format");
        std::string res = "";
        if (params.count("resolution")) res = params.at("resolution");

        // Optional: adjust camera resolution or pixel format based on params

        // Get latest frame
        std::vector<uint8_t> frame;
        std::string frame_fmt;
        {
            std::unique_lock<std::mutex> lk(frame_mutex);
            if (!last_frame.empty()) {
                frame = last_frame;
                frame_fmt = last_frame_fmt;
            } else {
                std::string resp = "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n";
                sendHTTP(fd, resp);
                close(fd);
                return;
            }
        }
        std::string ctype;
        if (frame_fmt == "jpeg") ctype = "image/jpeg";
        else ctype = "application/octet-stream";

        std::ostringstream oss;
        oss << "HTTP/1.1 200 OK\r\n"
            << "Content-Type: " << ctype << "\r\n"
            << "Content-Length: " << frame.size() << "\r\n"
            << "Cache-Control: no-cache\r\n"
            << "\r\n";
        sendHTTP(fd, oss.str());
        sendHTTP(fd, frame.data(), frame.size());
        close(fd);
    }

    // /camera/stream
    void handleCameraStream(int fd, const std::map<std::string,std::string>& params) {
        std::string fmt = "mjpeg";
        if (params.count("format")) fmt = params.at("format");
        // Only support MJPEG for live stream in browser
        std::ostringstream oss;
        oss << "HTTP/1.1 200 OK\r\n"
            << "Connection: close\r\n"
            << "Cache-Control: no-cache\r\n"
            << "Content-Type: multipart/x-mixed-replace; boundary=" << BOUNDARY << "\r\n"
            << "\r\n";
        sendHTTP(fd, oss.str());

        {
            std::lock_guard<std::mutex> lk(stream_clients_mutex);
            stream_clients.emplace_back(fd, fmt);
        }
        // This thread will keep alive until client closes connection (handled in captureLoop)
    }

    void captureLoop() {
        // Wait for /camera/start
        while (running) {
            {
                std::unique_lock<std::mutex> lk(capture_mutex);
                capture_cv.wait(lk, [this]{ return capture_on || !running; });
                if (!running) return;
            }
            if (!camera.openDevice() || !camera.initDevice() || !camera.startCapture()) {
                std::this_thread::sleep_for(std::chrono::seconds(1));
                continue;
            }
            while (capture_on && running) {
                // Grab a frame
                std::vector<uint8_t> frame;
                std::string frame_fmt;
                if (camera.readFrame(frame, &frame_fmt)) {
                    // Update snapshot
                    {
                        std::lock_guard<std::mutex> lk(frame_mutex);
                        last_frame = frame;
                        last_frame_fmt = frame_fmt;
                    }
                    // MJPEG streaming
                    std::lock_guard<std::mutex> lk(stream_clients_mutex);
                    for (auto it = stream_clients.begin(); it != stream_clients.end();) {
                        int fd = it->fd;
                        std::ostringstream oss;
                        oss << "--" << BOUNDARY << "\r\n"
                            << "Content-Type: image/jpeg\r\n"
                            << "Content-Length: " << frame.size() << "\r\n"
                            << "\r\n";
                        sendHTTP(fd, oss.str());
                        sendHTTP(fd, frame.data(), frame.size());
                        sendHTTP(fd, "\r\n");
                        // Check if client is alive
                        fd_set wfds;
                        struct timeval tv;
                        FD_ZERO(&wfds);
                        FD_SET(fd, &wfds);
                        tv.tv_sec = 0;
                        tv.tv_usec = 0;
                        int alive = select(fd+1, NULL, &wfds, NULL, &tv);
                        if (alive < 0) {
                            close(fd);
                            it = stream_clients.erase(it);
                        } else {
                            ++it;
                        }
                    }
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(MJPEG_FRAME_TIMEOUT_MS));
            }
            camera.stopCapture();
            camera.closeDevice();
            // Disconnect stream clients if stopped
            std::lock_guard<std::mutex> lk(stream_clients_mutex);
            for (auto& c : stream_clients) {
                close(c.fd);
            }
            stream_clients.clear();
        }
    }
};

int get_env_int(const char* name, int def) {
    const char* v = std::getenv(name);
    if (!v) return def;
    try { return std::stoi(v); } catch (...) { return def; }
}

std::string get_env_str(const char* name, const char* def) {
    const char* v = std::getenv(name);
    return v ? v : def;
}

int main() {
    signal(SIGPIPE, SIG_IGN);

    int http_port = get_env_int("HTTP_PORT", DEFAULT_HTTP_PORT);
    std::string http_host = get_env_str("HTTP_HOST", DEFAULT_SERVER_HOST);
    std::string video_dev = get_env_str("VIDEO_DEVICE", DEFAULT_VIDEO_DEVICE);

    int width = get_env_int("CAMERA_WIDTH", 640);
    int height = get_env_int("CAMERA_HEIGHT", 480);
    std::string pixel_format = get_env_str("CAMERA_FORMAT", "mjpeg");

    USBCamera camera(video_dev, width, height, pixel_format);
    HTTPServer server(http_host, http_port, camera);

    std::cout << "USB Camera HTTP Server started on " << http_host << ":" << http_port << std::endl;
    server.start();

    return 0;
}