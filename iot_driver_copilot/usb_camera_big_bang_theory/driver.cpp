#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>
#include <atomic>
#include <mutex>
#include <vector>
#include <condition_variable>
#include <map>
#include <sstream>

#include <sys/types.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <unistd.h>
#include <fcntl.h>

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <errno.h>

#define MAX_CLIENTS 16
#define BOUNDARY "frameboundary"
#define DEFAULT_VIDEO_DEVICE "/dev/video0"
#define DEFAULT_HTTP_PORT 8080
#define DEFAULT_RESOLUTION_WIDTH 640
#define DEFAULT_RESOLUTION_HEIGHT 480

struct Buffer {
    void* start;
    size_t length;
};

enum CaptureStatus {
    CAPTURE_STOPPED,
    CAPTURE_RUNNING
};

class USBCamera {
public:
    USBCamera();
    ~USBCamera();

    bool openDevice(const std::string& dev, int width, int height, const std::string& format);
    void closeDevice();
    bool startCapture();
    bool stopCapture();
    bool grabFrame(std::vector<unsigned char>& out, std::string& outFormat, int reqWidth, int reqHeight, const std::string& reqFormat);
    bool isCapturing();

    // for streaming
    bool getLatestMJPEGFrame(std::vector<unsigned char>& out);

    int defaultWidth = DEFAULT_RESOLUTION_WIDTH;
    int defaultHeight = DEFAULT_RESOLUTION_HEIGHT;
    std::string defaultFormat = "mjpeg";

private:
    bool initMMap();
    void uninitMMap();
    bool readFrame(std::vector<unsigned char>& out);
    int fd;
    Buffer* buffers;
    unsigned int n_buffers;
    std::mutex mtx;
    std::atomic<CaptureStatus> status;
    std::vector<unsigned char> lastMJPEGFrame;
    std::condition_variable frameReady;
};

USBCamera::USBCamera() : fd(-1), buffers(nullptr), n_buffers(0), status(CAPTURE_STOPPED) {}
USBCamera::~USBCamera() { closeDevice(); }

bool USBCamera::openDevice(const std::string& dev, int width, int height, const std::string& format) {
    std::lock_guard<std::mutex> lock(mtx);
    if (fd != -1) return true;
    fd = ::open(dev.c_str(), O_RDWR | O_NONBLOCK, 0);
    if (fd == -1) return false;

    struct v4l2_capability cap;
    if (ioctl(fd, VIDIOC_QUERYCAP, &cap) == -1) return false;

    struct v4l2_format fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;

    if (format == "jpeg" || format == "mjpeg")
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    else
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;

    fmt.fmt.pix.width = width;
    fmt.fmt.pix.height = height;

    if (ioctl(fd, VIDIOC_S_FMT, &fmt) == -1) return false;
    defaultWidth = width;
    defaultHeight = height;
    defaultFormat = format;
    return initMMap();
}

void USBCamera::closeDevice() {
    std::lock_guard<std::mutex> lock(mtx);
    if (fd != -1) {
        stopCapture();
        uninitMMap();
        ::close(fd);
        fd = -1;
    }
}

bool USBCamera::initMMap() {
    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count = 4;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;

    if (ioctl(fd, VIDIOC_REQBUFS, &req) == -1) return false;
    buffers = (Buffer*)calloc(req.count, sizeof(*buffers));
    if (!buffers) return false;

    for (n_buffers = 0; n_buffers < req.count; ++n_buffers) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = n_buffers;

        if (ioctl(fd, VIDIOC_QUERYBUF, &buf) == -1) return false;
        buffers[n_buffers].length = buf.length;
        buffers[n_buffers].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buf.m.offset);
        if (buffers[n_buffers].start == MAP_FAILED) return false;
    }
    return true;
}

void USBCamera::uninitMMap() {
    for (unsigned int i = 0; i < n_buffers; ++i)
        if (buffers[i].start)
            munmap(buffers[i].start, buffers[i].length);
    free(buffers);
    buffers = nullptr;
    n_buffers = 0;
}

bool USBCamera::startCapture() {
    std::lock_guard<std::mutex> lock(mtx);
    if (status == CAPTURE_RUNNING) return true;
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
    status = CAPTURE_RUNNING;
    return true;
}

bool USBCamera::stopCapture() {
    std::lock_guard<std::mutex> lock(mtx);
    if (status == CAPTURE_STOPPED) return true;
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(fd, VIDIOC_STREAMOFF, &type) == -1) return false;
    status = CAPTURE_STOPPED;
    return true;
}

bool USBCamera::isCapturing() {
    return status == CAPTURE_RUNNING;
}

bool USBCamera::readFrame(std::vector<unsigned char>& out) {
    struct v4l2_buffer buf;
    memset(&buf, 0, sizeof(buf));
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;

    if (ioctl(fd, VIDIOC_DQBUF, &buf) == -1) {
        if (errno == EAGAIN) return false;
        return false;
    }

    unsigned char* data = static_cast<unsigned char*>(buffers[buf.index].start);
    out.assign(data, data + buf.bytesused);

    // For streaming (MJPEG only)
    if (defaultFormat == "mjpeg" || defaultFormat == "jpeg") {
        std::unique_lock<std::mutex> l(mtx);
        lastMJPEGFrame = out;
        frameReady.notify_all();
    }

    if (ioctl(fd, VIDIOC_QBUF, &buf) == -1) return false;
    return true;
}

bool USBCamera::grabFrame(std::vector<unsigned char>& out, std::string& outFormat, int reqWidth, int reqHeight, const std::string& reqFormat) {
    if (!isCapturing()) return false;
    fd_set fds;
    struct timeval tv;
    int r;

    FD_ZERO(&fds);
    FD_SET(fd, &fds);
    tv.tv_sec = 2;
    tv.tv_usec = 0;

    r = select(fd+1, &fds, NULL, NULL, &tv);

    if (r == -1) return false;
    if (r == 0) return false;

    std::vector<unsigned char> frame;
    if (!readFrame(frame)) return false;

    // For now, return in current format
    if (reqFormat == "jpeg" || reqFormat == "mjpeg" || defaultFormat == "mjpeg" || defaultFormat == "jpeg") {
        out = frame;
        outFormat = "jpeg";
        return true;
    } else if (defaultFormat == "yuyv" && reqFormat == "jpeg") {
        // TODO: YUYV to JPEG conversion (needs a JPEG encoder, omitted here)
        return false;
    } else {
        out = frame;
        outFormat = defaultFormat;
        return true;
    }
}

bool USBCamera::getLatestMJPEGFrame(std::vector<unsigned char>& out) {
    std::unique_lock<std::mutex> lock(mtx);
    if (lastMJPEGFrame.size() > 0) {
        out = lastMJPEGFrame;
        return true;
    }
    return false;
}

// HTTP server code

class HTTPServer {
public:
    HTTPServer(USBCamera* cam, int port);
    void start();

private:
    void handleClient(int clientSock);
    void streamMJPEG(int clientSock, std::map<std::string, std::string>& query);
    void sendSnapshot(int clientSock, std::map<std::string, std::string>& query);
    void sendStart(int clientSock);
    void sendStop(int clientSock);
    void sendNotFound(int clientSock);

    USBCamera* camera;
    int port;
    std::atomic<bool> running;
    std::thread serverThread;
};

std::vector<std::string> split(const std::string& s, char delimiter) {
    std::vector<std::string> tokens;
    std::string token;
    std::istringstream tokenStream(s);
    while (getline(tokenStream, token, delimiter)) {
        tokens.push_back(token);
    }
    return tokens;
}

std::map<std::string, std::string> parseQuery(const std::string& query) {
    std::map<std::string, std::string> params;
    auto pairs = split(query, '&');
    for (auto& p : pairs) {
        auto kv = split(p, '=');
        if (kv.size() == 2) params[kv[0]] = kv[1];
    }
    return params;
}

HTTPServer::HTTPServer(USBCamera* cam, int port) : camera(cam), port(port), running(false) {}

void HTTPServer::start() {
    running = true;
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (sockfd < 0) { perror("socket"); exit(1); }

    int enable = 1;
    setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(int));

    struct sockaddr_in serv_addr;
    memset((char*)&serv_addr, 0, sizeof(serv_addr));
    serv_addr.sin_family = AF_INET;
    serv_addr.sin_addr.s_addr = INADDR_ANY;
    serv_addr.sin_port = htons(port);

    if (bind(sockfd, (struct sockaddr*)&serv_addr, sizeof(serv_addr)) < 0) { perror("bind"); exit(1); }
    if (listen(sockfd, MAX_CLIENTS) < 0) { perror("listen"); exit(1); }

    while (running) {
        struct sockaddr_in cli_addr;
        socklen_t clilen = sizeof(cli_addr);
        int newsockfd = accept(sockfd, (struct sockaddr*)&cli_addr, &clilen);
        if (newsockfd < 0) continue;
        std::thread(&HTTPServer::handleClient, this, newsockfd).detach();
    }
    close(sockfd);
}

void HTTPServer::handleClient(int clientSock) {
    char buffer[4096];
    int n = read(clientSock, buffer, sizeof(buffer)-1);
    if (n <= 0) { close(clientSock); return; }
    buffer[n] = 0;
    std::string req(buffer);

    // Get request line
    auto pos = req.find("\r\n");
    if (pos == std::string::npos) { close(clientSock); return; }
    auto req_line = req.substr(0, pos);
    auto parts = split(req_line, ' ');
    if (parts.size() < 2) { close(clientSock); return; }
    std::string method = parts[0];
    std::string path = parts[1];

    // Parse path and query
    std::string real_path = path;
    std::string query_str;
    auto qpos = path.find('?');
    if (qpos != std::string::npos) {
        real_path = path.substr(0, qpos);
        query_str = path.substr(qpos+1);
    }
    auto query = parseQuery(query_str);

    if (method == "GET" && real_path == "/camera/frame") {
        sendSnapshot(clientSock, query);
    } else if (method == "POST" && real_path == "/camera/start") {
        sendStart(clientSock);
    } else if (method == "POST" && real_path == "/camera/stop") {
        sendStop(clientSock);
    } else if (method == "GET" && real_path == "/camera/stream") {
        streamMJPEG(clientSock, query);
    } else {
        sendNotFound(clientSock);
        close(clientSock);
    }
}

void HTTPServer::sendSnapshot(int clientSock, std::map<std::string, std::string>& query) {
    int width = camera->defaultWidth;
    int height = camera->defaultHeight;
    std::string format = camera->defaultFormat;

    if (query.count("resolution")) {
        if (query["resolution"] == "HD") { width = 1280; height = 720; }
        else if (query["resolution"] == "VGA") { width = 640; height = 480; }
    }
    if (query.count("format")) {
        format = query["format"];
    }

    std::vector<unsigned char> frame;
    std::string outFormat;
    if (!camera->grabFrame(frame, outFormat, width, height, format)) {
        std::string resp = "HTTP/1.1 503 Service Unavailable\r\n\r\n";
        send(clientSock, resp.c_str(), resp.size(), 0);
        close(clientSock);
        return;
    }

    std::string contentType = (outFormat == "jpeg" || outFormat == "mjpeg") ? "image/jpeg" : "application/octet-stream";
    std::ostringstream oss;
    oss << "HTTP/1.1 200 OK\r\n";
    oss << "Content-Type: " << contentType << "\r\n";
    oss << "Content-Length: " << frame.size() << "\r\n";
    oss << "Cache-Control: no-cache\r\n";
    oss << "\r\n";
    send(clientSock, oss.str().c_str(), oss.str().size(), 0);
    send(clientSock, (const char*)frame.data(), frame.size(), 0);
    close(clientSock);
}

void HTTPServer::streamMJPEG(int clientSock, std::map<std::string, std::string>& query) {
    int width = camera->defaultWidth;
    int height = camera->defaultHeight;
    std::string format = "mjpeg";
    if (query.count("resolution")) {
        if (query["resolution"] == "HD") { width = 1280; height = 720; }
        else if (query["resolution"] == "VGA") { width = 640; height = 480; }
    }
    if (query.count("format")) {
        format = query["format"];
    }

    std::ostringstream oss;
    oss << "HTTP/1.1 200 OK\r\n";
    oss << "Cache-Control: no-cache\r\n";
    oss << "Connection: close\r\n";
    oss << "Content-Type: multipart/x-mixed-replace; boundary=" << BOUNDARY << "\r\n";
    oss << "\r\n";
    send(clientSock, oss.str().c_str(), oss.str().size(), 0);

    // stream loop
    while (camera->isCapturing()) {
        std::vector<unsigned char> frame;
        if (!camera->getLatestMJPEGFrame(frame)) { usleep(30000); continue; }
        std::ostringstream head;
        head << "--" << BOUNDARY << "\r\n";
        head << "Content-Type: image/jpeg\r\n";
        head << "Content-Length: " << frame.size() << "\r\n\r\n";
        send(clientSock, head.str().c_str(), head.str().size(), 0);
        send(clientSock, (const char*)frame.data(), frame.size(), 0);
        send(clientSock, "\r\n", 2, 0);
        usleep(50*1000); // 20fps
    }
    close(clientSock);
}

void HTTPServer::sendStart(int clientSock) {
    if (camera->isCapturing()) {
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\": \"already running\"}";
        send(clientSock, resp.c_str(), resp.size(), 0);
        close(clientSock);
        return;
    }
    if (camera->startCapture()) {
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\": \"started\"}";
        send(clientSock, resp.c_str(), resp.size(), 0);
    } else {
        std::string resp = "HTTP/1.1 500 Internal Server Error\r\n\r\n";
        send(clientSock, resp.c_str(), resp.size(), 0);
    }
    close(clientSock);
}

void HTTPServer::sendStop(int clientSock) {
    if (!camera->isCapturing()) {
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\": \"already stopped\"}";
        send(clientSock, resp.c_str(), resp.size(), 0);
        close(clientSock);
        return;
    }
    if (camera->stopCapture()) {
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\": \"stopped\"}";
        send(clientSock, resp.c_str(), resp.size(), 0);
    } else {
        std::string resp = "HTTP/1.1 500 Internal Server Error\r\n\r\n";
        send(clientSock, resp.c_str(), resp.size(), 0);
    }
    close(clientSock);
}

void HTTPServer::sendNotFound(int clientSock) {
    std::string resp = "HTTP/1.1 404 Not Found\r\n\r\n";
    send(clientSock, resp.c_str(), resp.size(), 0);
}

// MAIN

int main() {
    const char* dev = getenv("DEVICE_PATH");
    if (!dev) dev = DEFAULT_VIDEO_DEVICE;

    int port = DEFAULT_HTTP_PORT;
    const char* port_env = getenv("HTTP_PORT");
    if (port_env) port = atoi(port_env);

    int width = DEFAULT_RESOLUTION_WIDTH;
    int height = DEFAULT_RESOLUTION_HEIGHT;
    const char* w_env = getenv("CAMERA_RESOLUTION_WIDTH");
    const char* h_env = getenv("CAMERA_RESOLUTION_HEIGHT");
    if (w_env) width = atoi(w_env);
    if (h_env) height = atoi(h_env);

    std::string format = "mjpeg";
    const char* fmt_env = getenv("CAMERA_FORMAT");
    if (fmt_env) format = fmt_env;

    USBCamera camera;
    if (!camera.openDevice(dev, width, height, format)) {
        std::cerr << "Failed to open camera device " << dev << std::endl;
        return 1;
    }

    HTTPServer server(&camera, port);

    // Start capture by default
    camera.startCapture();

    std::cout << "USB Camera HTTP driver listening on port " << port << std::endl;
    server.start();

    return 0;
}