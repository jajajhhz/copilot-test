#include <iostream>
#include <fstream>
#include <sstream>
#include <thread>
#include <atomic>
#include <vector>
#include <map>
#include <mutex>
#include <condition_variable>
#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <termios.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <nlohmann/json.hpp> // For JSON (https://github.com/nlohmann/json)
#include <yaml-cpp/yaml.h>   // For YAML (https://github.com/jbeder/yaml-cpp)
#include <curl/curl.h>       // For Kubernetes API

using json = nlohmann::json;

// -----------------------------
// Config & Utility
// -----------------------------
std::string getenv_or_default(const char* key, const std::string& dflt = "") {
    const char* val = getenv(key);
    return val ? std::string(val) : dflt;
}

std::string load_file(const std::string& path) {
    std::ifstream in(path, std::ios::in | std::ios::binary);
    if (!in) return "";
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

void log(const std::string& msg) {
    std::cerr << "[" << std::chrono::system_clock::now().time_since_epoch().count() << "] " << msg << std::endl;
}

// -----------------------------
// UART Communication
// -----------------------------
class UART {
    int fd;
    std::mutex mtx;
    std::string device;
    int baud;
public:
    UART(const std::string& dev, int baudrate) : fd(-1), device(dev), baud(baudrate) {}
    bool openPort() {
        std::lock_guard<std::mutex> lock(mtx);
        fd = open(device.c_str(), O_RDWR | O_NOCTTY | O_NDELAY);
        if (fd == -1) return false;

        struct termios options;
        tcgetattr(fd, &options);
        speed_t brate = (baud == 115200 ? B115200 : (baud == 57600 ? B57600 : B9600));
        cfsetispeed(&options, brate);
        cfsetospeed(&options, brate);

        options.c_cflag |= (CLOCAL | CREAD);
        options.c_cflag &= ~PARENB;
        options.c_cflag &= ~CSTOPB;
        options.c_cflag &= ~CSIZE;
        options.c_cflag |= CS8;
        options.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
        options.c_iflag &= ~(IXON | IXOFF | IXANY);
        options.c_oflag &= ~OPOST;
        tcsetattr(fd, TCSANOW, &options);
        return true;
    }
    void closePort() {
        std::lock_guard<std::mutex> lock(mtx);
        if (fd != -1) { close(fd); fd = -1; }
    }
    int writeData(const std::vector<uint8_t>& data) {
        std::lock_guard<std::mutex> lock(mtx);
        if (fd == -1) return -1;
        return ::write(fd, data.data(), data.size());
    }
    int readData(uint8_t* buf, size_t len, int timeout_ms=500) {
        std::lock_guard<std::mutex> lock(mtx);
        if (fd == -1) return -1;
        fd_set set;
        struct timeval timeout;
        FD_ZERO(&set);
        FD_SET(fd, &set);
        timeout.tv_sec = timeout_ms/1000;
        timeout.tv_usec = (timeout_ms%1000)*1000;
        int rv = select(fd+1, &set, NULL, NULL, &timeout);
        if(rv > 0)
            return ::read(fd, buf, len);
        return -1;
    }
    ~UART() { closePort(); }
};

// -----------------------------
// Kubernetes Client (CRD Status Update)
// -----------------------------
class K8sCRDClient {
    std::string token;
    std::string ca_cert_path;
    std::string api_server;
    std::string edgename;
    std::string edgenamespace;
public:
    K8sCRDClient(const std::string& edge_name, const std::string& edge_ns)
        : edgename(edge_name), edgenamespace(edge_ns)
    {
        token = load_file("/var/run/secrets/kubernetes.io/serviceaccount/token");
        ca_cert_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
        api_server = "https://" + load_file("/var/run/secrets/kubernetes.io/serviceaccount/namespace");
        if (const char* host = getenv("KUBERNETES_SERVICE_HOST")) {
            if (const char* port = getenv("KUBERNETES_SERVICE_PORT")) {
                api_server = "https://" + std::string(host) + ":" + std::string(port);
            }
        }
    }
    void setPhase(const std::string& phase) {
        std::string url = api_server + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + edgenamespace +
                          "/edgedevices/" + edgename + "/status";
        json patch = {
            {"status", {{"edgeDevicePhase", phase}}}
        };
        CURL* curl = curl_easy_init();
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token).c_str());
        headers = curl_slist_append(headers, "Content-Type: application/merge-patch+json");
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PATCH");
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, patch.dump().c_str());
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_cert_path.c_str());
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 2L);
        curl_easy_perform(curl);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
    }
    json getEdgeDevice() {
        std::string url = api_server + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + edgenamespace +
                          "/edgedevices/" + edgename;
        CURL* curl = curl_easy_init();
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token).c_str());
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_cert_path.c_str());
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);

        std::string resp;
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,
            +[](void* ptr, size_t size, size_t nmemb, void* userdata) -> size_t {
                ((std::string*)userdata)->append((char*)ptr, size*nmemb);
                return size*nmemb;
            });
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp);
        curl_easy_perform(curl);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        try { return json::parse(resp); }
        catch (...) { return json(); }
    }
};

// -----------------------------
// ConfigMap API Instructions
// -----------------------------
using ProtocolSettings = std::map<std::string, std::map<std::string, std::string>>;
ProtocolSettings load_api_settings(const std::string& folder) {
    ProtocolSettings settings;
    std::ifstream dir(folder + "/api-config.yaml");
    if (!dir) return settings;
    YAML::Node root = YAML::LoadFile(folder + "/api-config.yaml");
    for (auto it = root.begin(); it != root.end(); ++it) {
        std::string api = it->first.as<std::string>();
        std::map<std::string, std::string> prop;
        if (it->second["protocolPropertyList"]) {
            for (auto pit = it->second["protocolPropertyList"].begin();
                pit != it->second["protocolPropertyList"].end(); ++pit) {
                prop[pit->first.as<std::string>()] = pit->second.as<std::string>();
            }
        }
        settings[api] = prop;
    }
    return settings;
}

// -----------------------------
// Camera Data Model (In-Memory)
// -----------------------------
struct ImageData {
    std::vector<uint8_t> data;
    std::string format; // e.g., "jpeg"
    std::string timestamp;
};
struct VideoFrame {
    std::vector<uint8_t> data;
    std::string timestamp;
};
class CameraSession {
    std::mutex mtx;
    std::atomic<bool> streaming;
    std::vector<VideoFrame> videoBuffer;
    ImageData lastImage;
    UART& uart;
    std::condition_variable cv;
    std::thread videoThread;
    bool exitThread;
public:
    CameraSession(UART& uart_) : uart(uart_), streaming(false), exitThread(false) {}
    ~CameraSession() { stopVideo(); }
    bool captureImage() {
        std::lock_guard<std::mutex> lock(mtx);
        std::vector<uint8_t> cmd = {0xA5, 0x01, 0x00, 0x5A}; // Example: 'capture' command
        if (uart.writeData(cmd) < 0) return false;
        uint8_t buf[1024*100];
        int n = uart.readData(buf, sizeof(buf), 2000);
        if (n <= 0) return false;
        lastImage.data.assign(buf, buf+n);
        lastImage.format = "jpeg";
        lastImage.timestamp = std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
        return true;
    }
    ImageData getLatestImage() {
        std::lock_guard<std::mutex> lock(mtx);
        return lastImage;
    }
    void startVideo() {
        std::lock_guard<std::mutex> lock(mtx);
        if (streaming) return;
        streaming = true;
        exitThread = false;
        videoBuffer.clear();
        videoThread = std::thread([this] { this->videoLoop(); });
    }
    void stopVideo() {
        {
            std::lock_guard<std::mutex> lock(mtx);
            if (!streaming) return;
            streaming = false;
            exitThread = true;
        }
        cv.notify_all();
        if (videoThread.joinable()) videoThread.join();
    }
    bool isStreaming() {
        return streaming;
    }
    void videoLoop() {
        while (!exitThread) {
            std::vector<uint8_t> cmd = {0xA5, 0x02, 0x00, 0x5A}; // Example: 'get_frame' command
            if (uart.writeData(cmd) < 0) break;
            uint8_t buf[1024*60];
            int n = uart.readData(buf, sizeof(buf), 300);
            if (n > 0) {
                VideoFrame vf;
                vf.data.assign(buf, buf+n);
                vf.timestamp = std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
                {
                    std::lock_guard<std::mutex> lock(mtx);
                    videoBuffer.push_back(std::move(vf));
                    if (videoBuffer.size() > 60) videoBuffer.erase(videoBuffer.begin());
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }
    std::vector<VideoFrame> getVideoFrames() {
        std::lock_guard<std::mutex> lock(mtx);
        return videoBuffer;
    }
};

// -----------------------------
// HTTP Server (Minimal)
// -----------------------------
struct HttpRequest {
    std::string method;
    std::string path;
    std::map<std::string, std::string> headers;
    std::string body;
    std::map<std::string, std::string> query;
};

struct HttpResponse {
    int code;
    std::string content_type;
    std::vector<uint8_t> body;
    std::map<std::string, std::string> headers;
};

class HttpServer {
    int listen_fd;
    std::string host;
    int port;
    std::atomic<bool> running;
public:
    HttpServer(const std::string& host_, int port_) : host(host_), port(port_), running(false) {}
    bool start(std::function<void(int)> handler) {
        listen_fd = socket(AF_INET, SOCK_STREAM, 0);
        if (listen_fd < 0) return false;
        int opt = 1;
        setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        struct sockaddr_in addr;
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port);
        addr.sin_addr.s_addr = INADDR_ANY;
        if (bind(listen_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) return false;
        if (listen(listen_fd, 10) < 0) return false;
        running = true;
        std::thread([this, handler] {
            while (running) {
                int client_fd = accept(listen_fd, NULL, NULL);
                if (client_fd < 0) continue;
                std::thread(handler, client_fd).detach();
            }
        }).detach();
        return true;
    }
    void stop() {
        running = false;
        close(listen_fd);
    }
};

// -----------------------------
// Driver Main Logic
// -----------------------------
int main() {
    // ENV Init
    std::string EDGEDEVICE_NAME = getenv_or_default("EDGEDEVICE_NAME");
    std::string EDGEDEVICE_NAMESPACE = getenv_or_default("EDGEDEVICE_NAMESPACE");
    std::string UART_DEVICE = getenv_or_default("DEVICE_UART", "/dev/ttyUSB0");
    int UART_BAUD = std::stoi(getenv_or_default("DEVICE_UART_BAUD", "115200"));
    std::string HTTP_HOST = getenv_or_default("SERVER_HOST", "0.0.0.0");
    int HTTP_PORT = std::stoi(getenv_or_default("SERVER_PORT", "8080"));

    if (EDGEDEVICE_NAME.empty() || EDGEDEVICE_NAMESPACE.empty()) {
        log("EDGEDEVICE_NAME or EDGEDEVICE_NAMESPACE not set");
        return 1;
    }

    // Load API ConfigMap
    ProtocolSettings api_settings = load_api_settings("/etc/edgedevice/config/instructions");

    // K8s CRD Client
    K8sCRDClient k8s(EDGEDEVICE_NAME, EDGEDEVICE_NAMESPACE);

    // UART and CameraSession
    UART uart(UART_DEVICE, UART_BAUD);
    CameraSession camera(uart);

    // DeviceShifu Phase Management
    auto update_phase = [&](const std::string& phase) {
        std::thread([&]() { k8s.setPhase(phase); }).detach();
    };

    if (!uart.openPort()) {
        update_phase("Failed");
        log("Failed to open UART port");
        return 1;
    }
    update_phase("Running");

    // HTTP Server
    HttpServer server(HTTP_HOST, HTTP_PORT);

    auto handle_client = [&](int client_fd) {
        char buffer[4096];
        int n = read(client_fd, buffer, sizeof(buffer)-1);
        if (n <= 0) { close(client_fd); return; }
        buffer[n] = 0;
        std::string reqstr(buffer);
        std::istringstream ss(reqstr);
        std::string method, url, version;
        ss >> method >> url >> version;
        std::string path = url;
        std::string querystr;
        auto qpos = url.find('?');
        if (qpos != std::string::npos) {
            path = url.substr(0, qpos);
            querystr = url.substr(qpos+1);
        }
        std::map<std::string, std::string> query;
        if (!querystr.empty()) {
            std::istringstream qs(querystr);
            std::string pair;
            while (std::getline(qs, pair, '&')) {
                auto eq = pair.find('=');
                if (eq != std::string::npos)
                    query[pair.substr(0, eq)] = pair.substr(eq+1);
            }
        }
        // Skip headers (minimal)
        std::string line;
        while (std::getline(ss, line) && line != "\r") { /* skip */ }

        // API Routing
        HttpResponse resp;
        resp.code = 404;
        resp.content_type = "application/json";
        resp.body = std::vector<uint8_t>{};
        resp.headers = {};

        // POST /camera/video/start or /commands/video/start
        if ((method == "POST" && (path == "/camera/video/start" || path == "/commands/video/start"))) {
            camera.startVideo();
            resp.code = 200;
            resp.body = std::vector<uint8_t>(json{{"status","started"}}.dump().begin(), json{{"status","started"}}.dump().end());
        }
        // POST /camera/video/stop or /commands/video/stop
        else if ((method == "POST" && (path == "/camera/video/stop" || path == "/commands/video/stop"))) {
            camera.stopVideo();
            resp.code = 200;
            resp.body = std::vector<uint8_t>(json{{"status","stopped"}}.dump().begin(), json{{"status","stopped"}}.dump().end());
        }
        // POST /camera/capture or /commands/capture
        else if ((method == "POST" && (path == "/camera/capture" || path == "/commands/capture"))) {
            bool ok = camera.captureImage();
            auto img = camera.getLatestImage();
            if (!ok || img.data.empty()) {
                resp.code = 500;
                resp.body = std::vector<uint8_t>(json{{"status","failed"}}.dump().begin(), json{{"status","failed"}}.dump().end());
            } else {
                std::string b64 = "not_implemented"; // base64 encode if needed
                resp.code = 200;
                resp.body = std::vector<uint8_t>(json{
                    {"status","ok"},
                    {"timestamp",img.timestamp},
                    {"format",img.format},
                    {"size",img.data.size()},
                    {"data",b64}
                }.dump().begin(), json{
                    {"status","ok"},
                    {"timestamp",img.timestamp},
                    {"format",img.format},
                    {"size",img.data.size()},
                    {"data",b64}
                }.dump().end());
            }
        }
        // GET /camera/image
        else if ((method == "GET" && path == "/camera/image")) {
            auto img = camera.getLatestImage();
            if (!img.data.empty()) {
                resp.code = 200;
                resp.content_type = "image/jpeg";
                resp.body = img.data;
            } else {
                resp.code = 404;
                std::string msg = "{\"status\":\"no image\"}";
                resp.body = std::vector<uint8_t>(msg.begin(), msg.end());
            }
        }
        // GET /camera/video
        else if ((method == "GET" && path == "/camera/video")) {
            if (!camera.isStreaming()) {
                std::string msg = "{\"status\":\"video not running\"}";
                resp.code = 404;
                resp.body = std::vector<uint8_t>(msg.begin(), msg.end());
            } else {
                // Stream as multipart/x-mixed-replace for browser/cli (MJPEG style)
                std::string boundary = "frame";
                std::string header =
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: multipart/x-mixed-replace;boundary=" + boundary + "\r\n\r\n";
                write(client_fd, header.c_str(), header.size());
                while (camera.isStreaming()) {
                    auto frames = camera.getVideoFrames();
                    if (!frames.empty()) {
                        auto& f = frames.back();
                        std::ostringstream oss;
                        oss << "--" << boundary << "\r\n"
                            << "Content-Type: image/jpeg\r\n"
                            << "Content-Length: " << f.data.size() << "\r\n\r\n";
                        write(client_fd, oss.str().c_str(), oss.str().size());
                        write(client_fd, reinterpret_cast<const char*>(f.data.data()), f.data.size());
                        write(client_fd, "\r\n", 2);
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
                close(client_fd);
                return;
            }
        }
        // Default 404
        else {
            std::string msg = "{\"status\":\"not found\"}";
            resp.body = std::vector<uint8_t>(msg.begin(), msg.end());
            resp.code = 404;
        }
        // Send HTTP response
        std::ostringstream oss;
        oss << "HTTP/1.1 " << resp.code << " "
            << (resp.code == 200 ? "OK" : "Error") << "\r\n";
        oss << "Content-Type: " << resp.content_type << "\r\n";
        oss << "Content-Length: " << resp.body.size() << "\r\n";
        for (const auto& h : resp.headers)
            oss << h.first << ": " << h.second << "\r\n";
        oss << "\r\n";
        write(client_fd, oss.str().c_str(), oss.str().size());
        if (!resp.body.empty())
            write(client_fd, reinterpret_cast<const char*>(resp.body.data()), resp.body.size());
        close(client_fd);
    };

    if (!server.start(handle_client)) {
        log("Failed to start HTTP server");
        update_phase("Failed");
        return 1;
    }
    log("HTTP server started");
    // Keep alive
    while (true) {
        std::this_thread::sleep_for(std::chrono::seconds(30));
        // Check UART
        if (uart.openPort())
            update_phase("Running");
        else
            update_phase("Failed");
    }
    return 0;
}