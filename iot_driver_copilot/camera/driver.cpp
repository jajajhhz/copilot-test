#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <thread>
#include <mutex>
#include <atomic>
#include <unordered_map>
#include <map>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/inotify.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <yaml-cpp/yaml.h>
#include <curl/curl.h>
#include <nlohmann/json.hpp>

// ---- Configuration: Environment Variables ----
#define ENV_DEVICE_NAME "EDGEDEVICE_NAME"
#define ENV_DEVICE_NAMESPACE "EDGEDEVICE_NAMESPACE"
#define ENV_HTTP_HOST "HTTP_SERVER_HOST"
#define ENV_HTTP_PORT "HTTP_SERVER_PORT"
#define ENV_UART_PORT "UART_PORT"
#define ENV_UART_BAUD "UART_BAUDRATE"
#define ENV_KUBERNETES_TOKEN_PATH "/var/run/secrets/kubernetes.io/serviceaccount/token"
#define ENV_KUBERNETES_CA_PATH "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
#define ENV_KUBERNETES_NAMESPACE_PATH "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
#define EDGEDEVICE_CRD_GROUP "shifu.edgenesis.io"
#define EDGEDEVICE_CRD_VERSION "v1alpha1"
#define EDGEDEVICE_CRD_KIND "EdgeDevice"
#define EDGEDEVICE_CRD_PLURAL "edgedevices"
#define CONFIGMAP_INSTRUCTIONS_PATH "/etc/edgedevice/config/instructions"

// ---- Simple HTTP Server (lightweight, no third-party library) ----
#define HTTP_BUFFER_SIZE 8192
#define MAX_CONNECTIONS 8

// ---- UART Communication ----
class UARTSession {
public:
    UARTSession(const std::string& port, int baudrate)
        : port_(port), baudrate_(baudrate), fd_(-1) {}

    bool open() {
        fd_ = ::open(port_.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
        if (fd_ < 0) return false;
        struct termios tty;
        memset(&tty, 0, sizeof tty);
        if (tcgetattr(fd_, &tty) != 0) return false;
        cfsetospeed(&tty, baudrate_);
        cfsetispeed(&tty, baudrate_);
        tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;     // 8-bit chars
        tty.c_iflag &= ~IGNBRK;         // disable break processing
        tty.c_lflag = 0;                // no signaling chars, no echo,
        tty.c_oflag = 0;                // no remapping, no delays
        tty.c_cc[VMIN]  = 1;            // read doesn't block
        tty.c_cc[VTIME] = 5;            // 0.5 seconds read timeout
        tty.c_iflag &= ~(IXON | IXOFF | IXANY); // shut off xon/xoff ctrl
        tty.c_cflag |= (CLOCAL | CREAD);// ignore modem controls,
        tty.c_cflag &= ~(PARENB | PARODD);      // shut off parity
        tty.c_cflag &= ~CSTOPB;
        tty.c_cflag &= ~CRTSCTS;
        if (tcsetattr(fd_, TCSANOW, &tty) != 0) return false;
        return true;
    }

    void close() {
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
    }

    ssize_t write(const uint8_t* data, size_t len) {
        if (fd_ < 0) return -1;
        return ::write(fd_, data, len);
    }

    ssize_t read(uint8_t* buf, size_t len) {
        if (fd_ < 0) return -1;
        return ::read(fd_, buf, len);
    }

    ~UARTSession() { close(); }
private:
    std::string port_;
    int baudrate_;
    int fd_;
};

// ---- Simple HTTP base64 utility ----
static const std::string base64_chars =
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz"
             "0123456789+/";

std::string base64_encode(unsigned char const* bytes_to_encode, size_t in_len) {
    std::string ret;
    int i = 0, j = 0;
    unsigned char char_array_3[3], char_array_4[4];

    while (in_len--) {
        char_array_3[i++] = *(bytes_to_encode++);
        if (i == 3) {
            char_array_4[0] = (char_array_3[0] & 0xfc) >> 2;
            char_array_4[1] = ((char_array_3[0] & 0x03) << 4) + ((char_array_3[1] & 0xf0) >> 4);
            char_array_4[2] = ((char_array_3[1] & 0x0f) << 2) + ((char_array_3[2] & 0xc0) >> 6);
            char_array_4[3] = char_array_3[2] & 0x3f;
            for(i = 0; (i <4) ; i++) ret += base64_chars[char_array_4[i]];
            i = 0;
        }
    }

    if (i) {
        for(j = i; j < 3; j++) char_array_3[j] = '\0';
        char_array_4[0] = ( char_array_3[0] & 0xfc ) >> 2;
        char_array_4[1] = ( ( char_array_3[0] & 0x03 ) << 4 ) + ( ( char_array_3[1] & 0xf0 ) >> 4 );
        char_array_4[2] = ( ( char_array_3[1] & 0x0f ) << 2 ) + ( ( char_array_3[2] & 0xc0 ) >> 6 );
        char_array_4[3] = char_array_3[2] & 0x3f;
        for (j = 0; (j < i + 1); j++) ret += base64_chars[char_array_4[j]];
        while((i++ < 3)) ret += '=';
    }
    return ret;
}

// ---- EdgeDevice CRD Kubernetes REST PATCH ----
class KubernetesClient {
public:
    KubernetesClient() {
        std::ifstream tokenf(ENV_KUBERNETES_TOKEN_PATH);
        std::stringstream ss; ss << tokenf.rdbuf();
        token_ = ss.str();
        tokenf.close();

        ca_path_ = ENV_KUBERNETES_CA_PATH;
        namespace_ = getenv("EDGEDEVICE_NAMESPACE") ? getenv("EDGEDEVICE_NAMESPACE") : get_current_namespace();
        api_server_ = getenv("KUBERNETES_SERVICE_HOST") ? getenv("KUBERNETES_SERVICE_HOST") : "kubernetes.default.svc";
        api_port_ = getenv("KUBERNETES_SERVICE_PORT") ? getenv("KUBERNETES_SERVICE_PORT") : "443";
    }

    std::string get_current_namespace() {
        std::ifstream nsf(ENV_KUBERNETES_NAMESPACE_PATH);
        std::string ns;
        getline(nsf, ns);
        nsf.close();
        return ns;
    }

    // PATCH status.edgeDevicePhase in EdgeDevice CR
    bool patch_edge_device_phase(const std::string& dev_name, const std::string& ns, const std::string& phase) {
        std::string url = "https://" + api_server_ + ":" + api_port_ +
            "/apis/" EDGEDEVICE_CRD_GROUP "/" EDGEDEVICE_CRD_VERSION "/namespaces/" + ns + "/" EDGEDEVICE_CRD_PLURAL "/" + dev_name + "/status";
        std::string payload = R"({"status": {"edgeDevicePhase": ")" + phase + R"("}})";
        CURL* curl = curl_easy_init();
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token_).c_str());
        headers = curl_slist_append(headers, "Accept: application/json");
        headers = curl_slist_append(headers, "Content-Type: application/merge-patch+json");
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_path_.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload.c_str());
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PATCH");
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, [](void*, size_t sz, size_t nm, void*){ return sz*nm; });
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, nullptr);
        long http_code = 0;
        auto res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
        return (res == CURLE_OK && http_code >= 200 && http_code < 300);
    }

    // GET EdgeDevice CR to get .spec.address
    std::string get_device_address(const std::string& dev_name, const std::string& ns) {
        std::string url = "https://" + api_server_ + ":" + api_port_ +
            "/apis/" EDGEDEVICE_CRD_GROUP "/" EDGEDEVICE_CRD_VERSION "/namespaces/" + ns + "/" EDGEDEVICE_CRD_PLURAL "/" + dev_name;
        CURL* curl = curl_easy_init();
        std::string response;
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token_).c_str());
        headers = curl_slist_append(headers, "Accept: application/json");
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_path_.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, +[](void* ptr, size_t sz, size_t nm, void* userdata) -> size_t {
            std::string* resp = reinterpret_cast<std::string*>(userdata);
            resp->append((char*)ptr, sz*nm);
            return sz*nm;
        });
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
        auto res = curl_easy_perform(curl);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
        if (res != CURLE_OK) return "";
        try {
            auto j = nlohmann::json::parse(response);
            if (j.contains("spec") && j["spec"].contains("address")) return j["spec"]["address"];
        } catch (...) { }
        return "";
    }
private:
    std::string token_;
    std::string ca_path_;
    std::string namespace_;
    std::string api_server_;
    std::string api_port_;
};

// ---- ConfigMap YAML instruction loader ----
struct InstructionSettings {
    std::map<std::string, std::string> protocolPropertyList;
};

std::map<std::string, InstructionSettings> load_instruction_settings(const std::string& path) {
    std::map<std::string, InstructionSettings> settings;
    try {
        YAML::Node config = YAML::LoadFile(path);
        for (auto it = config.begin(); it != config.end(); ++it) {
            std::string api = it->first.as<std::string>();
            auto node = it->second;
            if (node["protocolPropertyList"]) {
                InstructionSettings ins;
                for (auto jt = node["protocolPropertyList"].begin(); jt != node["protocolPropertyList"].end(); ++jt) {
                    ins.protocolPropertyList[jt->first.as<std::string>()] = jt->second.as<std::string>();
                }
                settings[api] = ins;
            }
        }
    } catch (...) { }
    return settings;
}

// ---- Camera State Management ----
enum class CameraStatus {
    PENDING,
    RUNNING,
    FAILED,
    UNKNOWN
};

struct CameraFrame {
    std::vector<uint8_t> data;
    std::string mime_type;
    uint64_t timestamp;
};

// ---- Camera Device Simulator (UART) ----
class CameraDevice {
public:
    CameraDevice(const std::string& uart_port, int baudrate)
        : uart_port_(uart_port), baudrate_(baudrate), streaming_(false), last_frame_() {}

    bool connect() {
        uart_.reset(new UARTSession(uart_port_, baudrate_));
        return uart_->open();
    }

    void disconnect() {
        uart_.reset();
    }

    // Command: Capture an image
    bool capture_image(CameraFrame& frame) {
        const char* cmd = "CAPTURE\r\n";
        if (uart_) uart_->write(reinterpret_cast<const uint8_t*>(cmd), strlen(cmd));
        // Simulate: Read image of random size (JPEG), 64KB max
        std::vector<uint8_t> img(10240 + (std::rand() % 40960), 0xFF);
        // Insert JPEG header
        img[0] = 0xFF; img[1] = 0xD8; img[img.size()-2] = 0xFF; img[img.size()-1] = 0xD9;
        frame.data = img;
        frame.mime_type = "image/jpeg";
        frame.timestamp = std::chrono::system_clock::now().time_since_epoch().count();
        std::lock_guard<std::mutex> lock(mutex_);
        last_frame_ = frame;
        return true;
    }

    // Command: Start video streaming
    bool start_video() {
        const char* cmd = "START_VIDEO\r\n";
        if (uart_) uart_->write(reinterpret_cast<const uint8_t*>(cmd), strlen(cmd));
        streaming_ = true;
        return true;
    }

    // Command: Stop video streaming
    bool stop_video() {
        const char* cmd = "STOP_VIDEO\r\n";
        if (uart_) uart_->write(reinterpret_cast<const uint8_t*>(cmd), strlen(cmd));
        streaming_ = false;
        return true;
    }

    // "Streaming" video: Simulate MJPEG stream
    void simulate_video_stream(std::function<void(const CameraFrame&)> frame_cb, std::atomic<bool>& stop_flag) {
        while (!stop_flag.load()) {
            CameraFrame frame;
            capture_image(frame);
            frame_cb(frame);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    CameraFrame get_last_frame() {
        std::lock_guard<std::mutex> lock(mutex_);
        return last_frame_;
    }

    bool is_streaming() const { return streaming_; }

private:
    std::string uart_port_;
    int baudrate_;
    std::unique_ptr<UARTSession> uart_;
    std::atomic<bool> streaming_;
    CameraFrame last_frame_;
    std::mutex mutex_;
};


// ---- HTTP Server ----
class HttpServer {
public:
    HttpServer(const std::string& host, uint16_t port, CameraDevice* camera)
        : host_(host), port_(port), camera_(camera), should_stop_(false) {}

    void start() {
        server_thread_ = std::thread([this]() { this->run(); });
    }

    void stop() {
        should_stop_ = true;
        if (server_thread_.joinable()) server_thread_.join();
    }

private:
    std::string host_;
    uint16_t port_;
    CameraDevice* camera_;
    std::atomic<bool> should_stop_;
    std::thread server_thread_;

    void run() {
        int server_fd = socket(AF_INET, SOCK_STREAM, 0);
        int opt = 1;
        setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR | SO_REUSEPORT, &opt, sizeof(opt));
        sockaddr_in address;
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = INADDR_ANY; // listen all
        address.sin_port = htons(port_);
        bind(server_fd, (struct sockaddr*)&address, sizeof(address));
        listen(server_fd, MAX_CONNECTIONS);
        while (!should_stop_) {
            sockaddr_in client_addr;
            socklen_t client_len = sizeof(client_addr);
            int client_fd = accept(server_fd, (struct sockaddr*)&client_addr, &client_len);
            if (client_fd < 0) continue;
            std::thread(&HttpServer::handle_client, this, client_fd).detach();
        }
        close(server_fd);
    }

    void handle_client(int client_fd) {
        char buffer[HTTP_BUFFER_SIZE];
        ssize_t len = read(client_fd, buffer, sizeof(buffer)-1);
        if (len <= 0) { close(client_fd); return; }
        buffer[len] = '\0';
        std::string req(buffer);
        std::string method, path, version;
        std::istringstream iss(req);
        iss >> method >> path >> version;
        // Only support GET/POST
        if (method == "GET" && path == "/camera/image") {
            handle_get_camera_image(client_fd);
        } else if (method == "POST" && (path == "/camera/capture" || path == "/commands/capture")) {
            handle_post_camera_capture(client_fd);
        } else if (method == "POST" && (path == "/camera/video/start" || path == "/commands/video/start")) {
            handle_post_video_start(client_fd);
        } else if (method == "POST" && (path == "/camera/video/stop" || path == "/commands/video/stop")) {
            handle_post_video_stop(client_fd);
        } else if (method == "GET" && path == "/camera/video") {
            handle_get_video_stream(client_fd);
        } else {
            respond_404(client_fd);
        }
        close(client_fd);
    }

    void handle_get_camera_image(int fd) {
        CameraFrame frame = camera_->get_last_frame();
        if (frame.data.empty()) {
            respond_json(fd, 404, R"({"error":"No image found"})");
            return;
        }
        std::string header = "HTTP/1.1 200 OK\r\nContent-Type: " + frame.mime_type +
            "\r\nContent-Length: " + std::to_string(frame.data.size()) + "\r\n\r\n";
        send(fd, header.c_str(), header.length(), 0);
        send(fd, reinterpret_cast<char*>(frame.data.data()), frame.data.size(), 0);
    }

    void handle_post_camera_capture(int fd) {
        CameraFrame frame;
        if (!camera_->capture_image(frame)) {
            respond_json(fd, 500, R"({"status":"fail","msg":"Capture failed"})");
            return;
        }
        nlohmann::json resp = {
            {"status", "success"},
            {"mime_type", frame.mime_type},
            {"timestamp", frame.timestamp},
            {"data", base64_encode(frame.data.data(), frame.data.size())}
        };
        respond_json(fd, 200, resp.dump());
    }

    void handle_post_video_start(int fd) {
        bool ok = camera_->start_video();
        nlohmann::json resp = {
            {"status", ok ? "started" : "failed"},
            {"stream_url", "/camera/video"}
        };
        respond_json(fd, 200, resp.dump());
    }

    void handle_post_video_stop(int fd) {
        bool ok = camera_->stop_video();
        nlohmann::json resp = {
            {"status", ok ? "stopped" : "failed"}
        };
        respond_json(fd, 200, resp.dump());
    }

    void handle_get_video_stream(int fd) {
        if (!camera_->is_streaming()) {
            respond_json(fd, 400, R"({"error":"Video not streaming"})");
            return;
        }
        std::string boundary = "mjpegstream";
        std::string header = "HTTP/1.1 200 OK\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=" + boundary + "\r\n"
            "Cache-Control: no-cache\r\n"
            "\r\n";
        send(fd, header.c_str(), header.size(), 0);
        std::atomic<bool> stop_flag(false);
        std::thread stream_thread([&](){
            camera_->simulate_video_stream([&](const CameraFrame& frame){
                std::ostringstream part;
                part << "--" << boundary << "\r\n"
                     << "Content-Type: " << frame.mime_type << "\r\n"
                     << "Content-Length: " << frame.data.size() << "\r\n\r\n";
                send(fd, part.str().c_str(), part.str().size(), 0);
                send(fd, reinterpret_cast<const char*>(frame.data.data()), frame.data.size(), 0);
                send(fd, "\r\n", 2, 0);
            }, stop_flag);
        });
        // Let it stream for up to 30 seconds or until client closes
        auto t0 = std::chrono::steady_clock::now();
        char tmpbuf[32];
        while (true) {
            ssize_t n = recv(fd, tmpbuf, sizeof(tmpbuf), MSG_DONTWAIT);
            if (n == 0 || n < 0) break;
            if (std::chrono::steady_clock::now() - t0 > std::chrono::seconds(30)) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        stop_flag = true;
        stream_thread.join();
    }

    void respond_json(int fd, int code, const std::string& body) {
        std::ostringstream oss;
        oss << "HTTP/1.1 " << code << " "
            << (code==200 ? "OK":"Error") << "\r\n"
            << "Content-Type: application/json\r\n"
            << "Content-Length: " << body.size() << "\r\n\r\n"
            << body;
        send(fd, oss.str().c_str(), oss.str().size(), 0);
    }

    void respond_404(int fd) {
        respond_json(fd, 404, R"({"error":"Not found"})");
    }
};


// ---- Main DeviceShifu Entrypoint ----
int main() {
    // Load env config
    std::string dev_name = getenv(ENV_DEVICE_NAME) ? getenv(ENV_DEVICE_NAME) : "";
    std::string dev_ns = getenv(ENV_DEVICE_NAMESPACE) ? getenv(ENV_DEVICE_NAMESPACE) : "";
    std::string uart_port = getenv(ENV_UART_PORT) ? getenv(ENV_UART_PORT) : "/dev/ttyS0";
    std::string http_host = getenv(ENV_HTTP_HOST) ? getenv(ENV_HTTP_HOST) : "0.0.0.0";
    uint16_t http_port = getenv(ENV_HTTP_PORT) ? (uint16_t)atoi(getenv(ENV_HTTP_PORT)) : 8080;
    int uart_baud = getenv(ENV_UART_BAUD) ? atoi(getenv(ENV_UART_BAUD)) : B115200;

    // Load ConfigMap instructions
    auto instructions = load_instruction_settings(CONFIGMAP_INSTRUCTIONS_PATH);

    // Setup K8s client
    KubernetesClient kube;
    std::string device_address = kube.get_device_address(dev_name, dev_ns);

    // CameraDevice
    CameraDevice camera(uart_port, uart_baud);

    // EdgeDevice phase management
    CameraStatus cam_status = CameraStatus::PENDING;
    if (camera.connect()) {
        cam_status = CameraStatus::RUNNING;
    } else {
        cam_status = CameraStatus::FAILED;
    }
    // PATCH status
    std::string phase_str;
    switch (cam_status) {
        case CameraStatus::PENDING: phase_str = "Pending"; break;
        case CameraStatus::RUNNING: phase_str = "Running"; break;
        case CameraStatus::FAILED: phase_str = "Failed"; break;
        default: phase_str = "Unknown"; break;
    }
    kube.patch_edge_device_phase(dev_name, dev_ns, phase_str);

    // HTTP server
    HttpServer server(http_host, http_port, &camera);
    server.start();

    // Monitor device status (simulate edge device status updates)
    std::thread([&](){
        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(15));
            CameraStatus new_status = camera.connect() ? CameraStatus::RUNNING : CameraStatus::FAILED;
            if (new_status != cam_status) {
                cam_status = new_status;
                std::string phase;
                switch (cam_status) {
                    case CameraStatus::PENDING: phase = "Pending"; break;
                    case CameraStatus::RUNNING: phase = "Running"; break;
                    case CameraStatus::FAILED: phase = "Failed"; break;
                    default: phase = "Unknown"; break;
                }
                kube.patch_edge_device_phase(dev_name, dev_ns, phase);
            }
        }
    }).detach();

    // Graceful shutdown
    std::signal(SIGTERM, [](int){ exit(0); });
    std::signal(SIGINT, [](int){ exit(0); });

    while (true) std::this_thread::sleep_for(std::chrono::seconds(600));
}