#include <iostream>
#include <fstream>
#include <sstream>
#include <thread>
#include <atomic>
#include <unordered_map>
#include <mutex>
#include <chrono>
#include <regex>
#include <vector>
#include <cstdlib>
#include <cstdio>

#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

#include <yaml-cpp/yaml.h>
#include <nlohmann/json.hpp>

#include <k8s_client.h> // Custom: assumed for CRD update, see comment below

#define CONFIG_PATH "/etc/edgedevice/config/instructions"
#define IMAGE_BUFFER_PATH "/tmp/last_camera_image.jpg"
#define VIDEO_BUFFER_PATH "/tmp/last_camera_video.mjpeg"

using json = nlohmann::json;

// ==== UART Communication ====
class UART {
public:
    UART(const std::string &dev, int baudrate)
        : fd_(-1), dev_(dev), baudrate_(baudrate) {}

    bool open_port() {
        fd_ = open(dev_.c_str(), O_RDWR | O_NOCTTY | O_NDELAY);
        if (fd_ == -1) return false;
        fcntl(fd_, F_SETFL, 0);
        struct termios options;
        tcgetattr(fd_, &options);
        cfsetispeed(&options, baudrate_);
        cfsetospeed(&options, baudrate_);
        options.c_cflag |= (CLOCAL | CREAD);
        options.c_cflag &= ~PARENB;
        options.c_cflag &= ~CSTOPB;
        options.c_cflag &= ~CSIZE;
        options.c_cflag |= CS8;
        tcsetattr(fd_, TCSANOW, &options);
        return true;
    }

    void close_port() {
        if (fd_ != -1) {
            close(fd_);
            fd_ = -1;
        }
    }

    bool write_cmd(const std::string &cmd) {
        if (fd_ == -1) return false;
        ssize_t n = write(fd_, cmd.c_str(), cmd.length());
        return n == (ssize_t)cmd.length();
    }

    std::string read_response(size_t maxlen = 4096, int timeout_ms = 1000) {
        if (fd_ == -1) return "";
        fd_set readfds;
        struct timeval tv;
        FD_ZERO(&readfds);
        FD_SET(fd_, &readfds);
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        std::string result;
        char buf[256];

        int rc = select(fd_ + 1, &readfds, NULL, NULL, &tv);
        if (rc > 0 && FD_ISSET(fd_, &readfds)) {
            ssize_t n = read(fd_, buf, sizeof(buf));
            if (n > 0) result.append(buf, n);
        }
        return result;
    }

    bool is_open() const { return fd_ != -1; }

    ~UART() { close_port(); }

private:
    int fd_;
    std::string dev_;
    int baudrate_;
};

// ========== Config Loading ==========
struct ProtocolSettings {
    std::unordered_map<std::string, std::string> settings;
};
std::unordered_map<std::string, ProtocolSettings> load_api_settings(const std::string &config_dir) {
    std::unordered_map<std::string, ProtocolSettings> api_settings;
    struct stat s;
    if (stat(config_dir.c_str(), &s) != 0 || !S_ISDIR(s.st_mode)) return api_settings;
    DIR *dir = opendir(config_dir.c_str());
    if (!dir) return api_settings;

    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (entry->d_type != DT_REG) continue;
        std::string fname(entry->d_name);
        if (fname.size() > 5 && fname.substr(fname.size() - 5) == ".yaml") {
            std::string path = config_dir + "/" + fname;
            try {
                YAML::Node node = YAML::LoadFile(path);
                for (auto it = node.begin(); it != node.end(); ++it) {
                    std::string api = it->first.as<std::string>();
                    ProtocolSettings ps;
                    auto ppl = it->second["protocolPropertyList"];
                    if (ppl) {
                        for (auto p = ppl.begin(); p != ppl.end(); ++p) {
                            ps.settings[p->first.as<std::string>()] = p->second.as<std::string>();
                        }
                    }
                    api_settings[api] = ps;
                }
            } catch (...) {}
        }
    }
    closedir(dir);
    return api_settings;
}

// ==== Global State ====
std::mutex camera_mutex;
std::atomic<bool> video_streaming(false);
std::vector<std::vector<uint8_t>> video_buffer;
std::vector<uint8_t> last_image_buffer;
std::string image_format = "jpeg";
std::string video_format = "mjpeg";
std::string uart_device;
int uart_baudrate = B115200;
UART *camera_uart = nullptr;
std::string device_address;
std::string edge_device_name;
std::string edge_device_namespace;

// ==== Kubernetes CRD Status Update ====
enum class EdgeDevicePhase { Pending, Running, Failed, Unknown };
void update_edge_device_status(EdgeDevicePhase phase) {
    // This is a placeholder. Implement a minimal in-cluster k8s client here.
    // Use k8s_client.h for custom CRD update; not provided in this snippet.
    // Example: k8s_update_status(edge_device_name, edge_device_namespace, phase_string);
}

// ==== Camera Protocol ====
bool send_uart_capture_image(std::vector<uint8_t> &img_buf) {
    std::lock_guard<std::mutex> lock(camera_mutex);
    if (!camera_uart || !camera_uart->is_open()) return false;
    camera_uart->write_cmd("CAPTURE_IMAGE\n");
    std::string resp = camera_uart->read_response(4096, 2000);
    // Expecting "IMAGE_BEGIN\n...binary...\nIMAGE_END\n"
    size_t begin = resp.find("IMAGE_BEGIN\n");
    size_t end = resp.find("IMAGE_END\n");
    if (begin == std::string::npos || end == std::string::npos || end <= begin)
        return false;
    std::string img_data = resp.substr(begin + 12, end - (begin + 12));
    img_buf.assign(img_data.begin(), img_data.end());
    return true;
}

bool send_uart_start_video() {
    std::lock_guard<std::mutex> lock(camera_mutex);
    if (!camera_uart || !camera_uart->is_open()) return false;
    return camera_uart->write_cmd("START_VIDEO\n");
}

bool send_uart_stop_video() {
    std::lock_guard<std::mutex> lock(camera_mutex);
    if (!camera_uart || !camera_uart->is_open()) return false;
    return camera_uart->write_cmd("STOP_VIDEO\n");
}

bool send_uart_get_video_frame(std::vector<uint8_t> &frame_buf) {
    std::lock_guard<std::mutex> lock(camera_mutex);
    if (!camera_uart || !camera_uart->is_open()) return false;
    camera_uart->write_cmd("GET_VIDEO_FRAME\n");
    std::string resp = camera_uart->read_response(65536, 1000);
    // Expecting "FRAME_BEGIN\n...binary...\nFRAME_END\n"
    size_t begin = resp.find("FRAME_BEGIN\n");
    size_t end = resp.find("FRAME_END\n");
    if (begin == std::string::npos || end == std::string::npos || end <= begin)
        return false;
    std::string frame_data = resp.substr(begin + 11, end - (begin + 11));
    frame_buf.assign(frame_data.begin(), frame_data.end());
    return true;
}

// ==== Video Streaming Thread ====
void video_stream_thread() {
    while (video_streaming) {
        std::vector<uint8_t> frame;
        if (send_uart_get_video_frame(frame)) {
            std::lock_guard<std::mutex> lock(camera_mutex);
            video_buffer.push_back(std::move(frame));
            if (video_buffer.size() > 100) video_buffer.erase(video_buffer.begin());
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(60)); // ~15fps
    }
}

// ==== HTTP Server (Minimal) ====
#include <netinet/in.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <cstring>

class HttpServer {
public:
    HttpServer(const std::string &host, int port)
        : host_(host), port_(port), server_fd_(-1) {}

    bool start() {
        struct sockaddr_in addr;
        server_fd_ = socket(AF_INET, SOCK_STREAM, 0);
        if (server_fd_ == -1) return false;
        int opt = 1;
        setsockopt(server_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = inet_addr(host_.c_str());
        addr.sin_port = htons(port_);
        if (bind(server_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0)
            return false;
        if (listen(server_fd_, 10) < 0) return false;
        return true;
    }

    void serve() {
        while (true) {
            int client_fd = accept(server_fd_, NULL, NULL);
            if (client_fd == -1) continue;
            std::thread(&HttpServer::handle_client, this, client_fd).detach();
        }
    }

    void stop() {
        if (server_fd_ != -1) close(server_fd_);
    }

    ~HttpServer() { stop(); }

private:
    std::string host_;
    int port_;
    int server_fd_;

    void handle_client(int client_fd) {
        char buffer[8192];
        ssize_t n = recv(client_fd, buffer, sizeof(buffer) - 1, 0);
        if (n <= 0) { close(client_fd); return; }
        buffer[n] = '\0';
        std::string req(buffer);
        std::string method, path;
        std::istringstream iss(req);
        iss >> method >> path;
        if (method == "GET" && (path == "/camera/image" || path.find("/camera/image?") == 0)) {
            handle_get_image(client_fd);
        } else if (method == "POST" && path == "/camera/capture") {
            handle_post_capture(client_fd);
        } else if (method == "GET" && (path == "/camera/video" || path.find("/camera/video?") == 0)) {
            handle_get_video(client_fd);
        } else if (method == "POST" && (path == "/camera/video/start" || path == "/commands/video/start")) {
            handle_post_video_start(client_fd);
        } else if (method == "POST" && (path == "/camera/video/stop" || path == "/commands/video/stop")) {
            handle_post_video_stop(client_fd);
        } else if (method == "POST" && path == "/commands/capture") {
            handle_post_capture(client_fd);
        } else {
            send_404(client_fd);
        }
        close(client_fd);
    }

    void handle_get_image(int client_fd) {
        std::lock_guard<std::mutex> lock(camera_mutex);
        if (last_image_buffer.empty()) {
            send_json(client_fd, R"({"error":"No image available"})", 404);
            return;
        }
        std::ostringstream oss;
        oss << "HTTP/1.1 200 OK\r\n"
            << "Content-Type: image/jpeg\r\n"
            << "Content-Length: " << last_image_buffer.size() << "\r\n"
            << "Connection: close\r\n\r\n";
        send(client_fd, oss.str().c_str(), oss.str().size(), 0);
        send(client_fd, (char*)last_image_buffer.data(), last_image_buffer.size(), 0);
    }

    void handle_post_capture(int client_fd) {
        std::vector<uint8_t> img;
        bool ok = send_uart_capture_image(img);
        if (ok && !img.empty()) {
            {
                std::lock_guard<std::mutex> lock(camera_mutex);
                last_image_buffer = img;
            }
            send_json(client_fd, R"({"status":"ok","message":"Image captured"})");
        } else {
            send_json(client_fd, R"({"status":"failed","message":"Capture failed"})", 500);
        }
    }

    void handle_get_video(int client_fd) {
        if (!video_streaming) {
            send_json(client_fd, R"({"error":"Video not streaming"})", 404);
            return;
        }
        std::string boundary = "videoboundary";
        std::ostringstream oss;
        oss << "HTTP/1.1 200 OK\r\n"
            << "Content-Type: multipart/x-mixed-replace; boundary=" << boundary << "\r\n"
            << "Connection: close\r\n\r\n";
        send(client_fd, oss.str().c_str(), oss.str().size(), 0);

        // Stream frames as MJPEG
        while (video_streaming) {
            std::vector<uint8_t> frame;
            {
                std::lock_guard<std::mutex> lock(camera_mutex);
                if (!video_buffer.empty())
                    frame = video_buffer.back();
            }
            if (!frame.empty()) {
                std::ostringstream part;
                part << "--" << boundary << "\r\n"
                     << "Content-Type: image/jpeg\r\n"
                     << "Content-Length: " << frame.size() << "\r\n\r\n";
                send(client_fd, part.str().c_str(), part.str().size(), 0);
                send(client_fd, (char*)frame.data(), frame.size(), 0);
                send(client_fd, "\r\n", 2, 0);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(70));
        }
    }

    void handle_post_video_start(int client_fd) {
        if (video_streaming) {
            send_json(client_fd, R"({"status":"ok","message":"Already streaming"})");
            return;
        }
        bool ok = send_uart_start_video();
        if (ok) {
            video_streaming = true;
            std::thread(video_stream_thread).detach();
            send_json(client_fd, R"({"status":"ok","message":"Video streaming started"})");
        } else {
            send_json(client_fd, R"({"status":"failed","message":"Failed to start video"})", 500);
        }
    }

    void handle_post_video_stop(int client_fd) {
        bool ok = send_uart_stop_video();
        video_streaming = false;
        if (ok) {
            send_json(client_fd, R"({"status":"ok","message":"Video streaming stopped"})");
        } else {
            send_json(client_fd, R"({"status":"failed","message":"Failed to stop video"})", 500);
        }
    }

    void send_json(int client_fd, const std::string &body, int status = 200) {
        std::ostringstream oss;
        oss << "HTTP/1.1 " << status << " "
            << (status == 200 ? "OK" : (status == 404 ? "Not Found" : "Internal Server Error")) << "\r\n"
            << "Content-Type: application/json\r\n"
            << "Content-Length: " << body.size() << "\r\n"
            << "Connection: close\r\n\r\n"
            << body;
        send(client_fd, oss.str().c_str(), oss.str().size(), 0);
    }

    void send_404(int client_fd) {
        send_json(client_fd, R"({"error":"Not found"})", 404);
    }
};

// ==== Main Entrypoint ====
int main(int argc, char *argv[]) {
    // Config from env
    edge_device_name = getenv("EDGEDEVICE_NAME") ? getenv("EDGEDEVICE_NAME") : "";
    edge_device_namespace = getenv("EDGEDEVICE_NAMESPACE") ? getenv("EDGEDEVICE_NAMESPACE") : "";
    if (edge_device_name.empty() || edge_device_namespace.empty()) {
        std::cerr << "ERROR: EDGEDEVICE_NAME and EDGEDEVICE_NAMESPACE env required.\n";
        return 1;
    }
    std::string server_host = getenv("DEVICE_SHIFU_HOST") ? getenv("DEVICE_SHIFU_HOST") : "0.0.0.0";
    int server_port = getenv("DEVICE_SHIFU_PORT") ? atoi(getenv("DEVICE_SHIFU_PORT")) : 8080;
    uart_device = getenv("CAMERA_UART_DEVICE") ? getenv("CAMERA_UART_DEVICE") : "/dev/ttyUSB0";
    uart_baudrate = getenv("CAMERA_UART_BAUDRATE") ? atoi(getenv("CAMERA_UART_BAUDRATE")) : B115200;

    // Load API YAML config
    auto api_settings = load_api_settings(CONFIG_PATH);

    // Connect UART
    camera_uart = new UART(uart_device, uart_baudrate);
    if (!camera_uart->open_port()) {
        update_edge_device_status(EdgeDevicePhase::Failed);
        std::cerr << "ERROR: Cannot open UART port\n";
        return 1;
    }
    update_edge_device_status(EdgeDevicePhase::Running);

    // HTTP server
    HttpServer http(server_host, server_port);
    if (!http.start()) {
        update_edge_device_status(EdgeDevicePhase::Failed);
        std::cerr << "ERROR: Cannot start HTTP server\n";
        return 1;
    }

    std::cout << "DeviceShifu Camera driver running on " << server_host << ":" << server_port << std::endl;
    http.serve();

    camera_uart->close_port();
    delete camera_uart;
    update_edge_device_status(EdgeDevicePhase::Unknown);
    return 0;
}