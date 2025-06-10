#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <unordered_map>
#include <mutex>
#include <atomic>
#include <condition_variable>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/ioctl.h>
#include <dirent.h>
#include <signal.h>
#include <yaml-cpp/yaml.h>
#include <nlohmann/json.hpp>
#include <curl/curl.h>
#include <httplib.h>

// ----------- Constants and Types ------------

static const std::string K8S_HOST = "https://kubernetes.default.svc";
static const std::string TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token";
static const std::string CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
static const std::string NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace";

using json = nlohmann::json;

// Device Phases
enum DevicePhase {
    Pending,
    Running,
    Failed,
    Unknown
};

inline std::string phaseToString(DevicePhase phase) {
    switch (phase) {
        case Pending: return "Pending";
        case Running: return "Running";
        case Failed: return "Failed";
        case Unknown: return "Unknown";
        default: return "Unknown";
    }
}

// ----------- UART Camera Driver Logic ------------

class UARTCamera {
public:
    UARTCamera(const std::string& uart_path, int baudrate)
        : uart_path_(uart_path), baudrate_(baudrate), fd_(-1), is_streaming_(false) {}

    bool connect() {
        std::lock_guard<std::mutex> lock(uart_mutex_);
        fd_ = open(uart_path_.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
        if (fd_ < 0) return false;
        struct termios tty;
        memset(&tty, 0, sizeof tty);
        if (tcgetattr(fd_, &tty) != 0) return false;
        cfsetospeed(&tty, baudrate_);
        cfsetispeed(&tty, baudrate_);
        tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
        tty.c_iflag &= ~IGNBRK;
        tty.c_lflag = 0;
        tty.c_oflag = 0;
        tty.c_cc[VMIN]  = 1;
        tty.c_cc[VTIME] = 1;
        tty.c_cflag |= (CLOCAL | CREAD);
        tty.c_cflag &= ~(PARENB | PARODD);
        tty.c_cflag &= ~CSTOPB;
        tty.c_cflag &= ~CRTSCTS;
        if (tcsetattr(fd_, TCSANOW, &tty) != 0) return false;
        return true;
    }

    void disconnect() {
        std::lock_guard<std::mutex> lock(uart_mutex_);
        if (fd_ >= 0) {
            close(fd_);
            fd_ = -1;
        }
        is_streaming_ = false;
    }

    bool isConnected() {
        std::lock_guard<std::mutex> lock(uart_mutex_);
        return fd_ >= 0;
    }

    // Send a command and wait for an "OK" or error
    bool sendCommand(const std::string& cmd, std::string& response, int timeout_ms = 1000) {
        std::lock_guard<std::mutex> lock(uart_mutex_);
        if (fd_ < 0) return false;
        std::string full_cmd = cmd + "\n";
        if (::write(fd_, full_cmd.c_str(), full_cmd.size()) < 0) return false;
        // Read response
        char buf[256];
        int len = 0;
        auto start = std::chrono::steady_clock::now();
        std::string resp;
        while (true) {
            int n = ::read(fd_, buf, sizeof(buf));
            if (n > 0) {
                resp.append(buf, n);
                if (resp.find('\n') != std::string::npos)
                    break;
            }
            auto now = std::chrono::steady_clock::now();
            if (std::chrono::duration_cast<std::chrono::milliseconds>(now - start).count() > timeout_ms) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        response = resp;
        return !resp.empty();
    }

    // Capture still image, save to file, return file path
    bool captureImage(std::string& file_path, std::string& base64_img) {
        std::string response;
        if (!sendCommand("CAPTURE_IMAGE", response)) return false;
        // Let's assume the device returns "OK" and then binary image data
        if (response.find("OK") == std::string::npos) return false;
        std::string img_fn = "/tmp/capture_" + std::to_string(time(nullptr)) + ".jpg";
        std::ofstream ofs(img_fn, std::ios::binary);
        if (!ofs.is_open()) return false;
        // Read image data (simulate, or real device protocol)
        // We'll just simulate 10KB of data here
        char imgbuf[10240];
        int total = 0;
        while (total < 10240) {
            int n = ::read(fd_, imgbuf + total, 10240 - total);
            if (n > 0) total += n;
            else break;
        }
        ofs.write(imgbuf, total);
        ofs.close();
        file_path = img_fn;
        // Load to base64
        std::ifstream ifs(img_fn, std::ios::binary);
        std::ostringstream oss;
        oss << ifs.rdbuf();
        base64_img = base64_encode(reinterpret_cast<const unsigned char*>(oss.str().data()), oss.str().size());
        return true;
    }

    // Start video stream (MJPEG over HTTP)
    bool startVideoStream() {
        std::string response;
        if (!sendCommand("START_VIDEO", response)) return false;
        if (response.find("OK") == std::string::npos) return false;
        is_streaming_ = true;
        return true;
    }

    // Stop video stream
    bool stopVideoStream() {
        std::string response;
        if (!sendCommand("STOP_VIDEO", response)) return false;
        if (response.find("OK") == std::string::npos) return false;
        is_streaming_ = false;
        return true;
    }

    // Read one jpeg frame from UART (simulate)
    bool readVideoFrame(std::vector<unsigned char>& jpeg_buf) {
        std::lock_guard<std::mutex> lock(uart_mutex_);
        if (fd_ < 0) return false;
        // simulate: device sends [4 bytes:frame len][frame data]
        unsigned char lenbuf[4];
        int got = 0;
        while (got < 4) {
            int n = ::read(fd_, lenbuf + got, 4 - got);
            if (n > 0) got += n;
            else return false;
        }
        int frame_len = (lenbuf[0]<<24) | (lenbuf[1]<<16) | (lenbuf[2]<<8) | lenbuf[3];
        if (frame_len <= 0 || frame_len > 1024*1024) return false;
        jpeg_buf.resize(frame_len);
        got = 0;
        while (got < frame_len) {
            int n = ::read(fd_, jpeg_buf.data() + got, frame_len - got);
            if (n > 0) got += n;
            else return false;
        }
        return true;
    }

    bool isStreaming() const { return is_streaming_; }

private:
    std::string uart_path_;
    int baudrate_;
    int fd_;
    bool is_streaming_;
    std::mutex uart_mutex_;

    // -------- Base64 helpers -----------
    static const std::string& base64_chars() {
        static std::string chars =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789+/";
        return chars;
    }
    static std::string base64_encode(const unsigned char* buf, size_t bufLen) {
        std::string ret;
        int i = 0, j = 0;
        unsigned char char_array_3[3], char_array_4[4];
        while (bufLen--) {
            char_array_3[i++] = *(buf++);
            if (i == 3) {
                char_array_4[0] = (char_array_3[0] & 0xfc) >> 2;
                char_array_4[1] = ((char_array_3[0] & 0x03) << 4) +
                                  ((char_array_3[1] & 0xf0) >> 4);
                char_array_4[2] = ((char_array_3[1] & 0x0f) << 2) +
                                  ((char_array_3[2] & 0xc0) >> 6);
                char_array_4[3] = char_array_3[2] & 0x3f;
                for (i = 0; i < 4; i++) ret += base64_chars()[char_array_4[i]];
                i = 0;
            }
        }
        if (i) {
            for (j = i; j < 3; j++) char_array_3[j] = '\0';
            char_array_4[0] = (char_array_3[0] & 0xfc) >> 2;
            char_array_4[1] = ((char_array_3[0] & 0x03) << 4) +
                              ((char_array_3[1] & 0xf0) >> 4);
            char_array_4[2] = ((char_array_3[1] & 0x0f) << 2) +
                              ((char_array_3[2] & 0xc0) >> 6);
            char_array_4[3] = char_array_3[2] & 0x3f;
            for (j = 0; j < i + 1; j++) ret += base64_chars()[char_array_4[j]];
            while ((i++ < 3)) ret += '=';
        }
        return ret;
    }
};

// ----------- YAML Config Loader ---------------

struct APIInstructionSettings {
    std::unordered_map<std::string, std::string> protocolPropertyList;
};
using APIInstructionDict = std::unordered_map<std::string, APIInstructionSettings>;

bool loadAPIInstructions(const std::string& config_path, APIInstructionDict& dict) {
    try {
        YAML::Node config = YAML::LoadFile(config_path);
        for (auto it = config.begin(); it != config.end(); ++it) {
            std::string api = it->first.as<std::string>();
            APIInstructionSettings settings;
            if (it->second["protocolPropertyList"]) {
                for (auto sit = it->second["protocolPropertyList"].begin();
                     sit != it->second["protocolPropertyList"].end(); ++sit) {
                    settings.protocolPropertyList[sit->first.as<std::string>()] = sit->second.as<std::string>();
                }
            }
            dict[api] = settings;
        }
        return true;
    } catch (...) {
        return false;
    }
}

// ----------- K8s API Utilities ---------------

class K8sClient {
public:
    K8sClient() {
        // Read token
        std::ifstream tfs(TOKEN_PATH);
        if (!tfs) throw std::runtime_error("Cannot read K8s token");
        token_ = std::string((std::istreambuf_iterator<char>(tfs)),
                             std::istreambuf_iterator<char>());
        // CA
        ca_path_ = CA_PATH;
        // Namespace
        std::ifstream nfs(NAMESPACE_PATH);
        if (nfs) {
            namespace_ = std::string((std::istreambuf_iterator<char>(nfs)),
                                     std::istreambuf_iterator<char>());
        }
    }

    bool patchEdgeDeviceStatus(const std::string& name, const std::string& ns, DevicePhase phase) {
        CURL* curl = curl_easy_init();
        if (!curl) return false;
        std::string url = K8S_HOST + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/"
                        + ns + "/edgedevices/" + name + "/status";
        std::string patch_body = R"({"status":{"edgeDevicePhase":")" + phaseToString(phase) + R"("}})";
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Content-Type: application/merge-patch+json");
        std::string auth = "Authorization: Bearer " + token_;
        headers = curl_slist_append(headers, auth.c_str());
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PATCH");
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, patch_body.c_str());
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_path_.c_str());
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 2L);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, K8sClient::curl_write_cb);
        long http_code = 0;
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
        return (res == CURLE_OK && (http_code == 200 || http_code == 201 || http_code == 202));
    }

    bool getEdgeDeviceSpecAddress(const std::string& name, const std::string& ns, std::string& address) {
        CURL* curl = curl_easy_init();
        if (!curl) return false;
        std::string url = K8S_HOST + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/"
                        + ns + "/edgedevices/" + name;
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Accept: application/json");
        std::string auth = "Authorization: Bearer " + token_;
        headers = curl_slist_append(headers, auth.c_str());
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_path_.c_str());
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
        std::string response;
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, K8sClient::curl_write_cb_str);
        long http_code = 0;
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
        if (res == CURLE_OK && http_code == 200) {
            try {
                auto j = json::parse(response);
                address = j["spec"]["address"];
                return true;
            } catch (...) {}
        }
        return false;
    }

    static size_t curl_write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
        return size * nmemb;
    }
    static size_t curl_write_cb_str(char* ptr, size_t size, size_t nmemb, void* userdata) {
        std::string* str = static_cast<std::string*>(userdata);
        str->append(ptr, size*nmemb);
        return size*nmemb;
    }
private:
    std::string token_;
    std::string ca_path_;
    std::string namespace_;
};

// ----------- Global State ------------

struct AppState {
    UARTCamera* camera;
    std::string device_name;
    std::string device_namespace;
    std::string device_address;
    APIInstructionDict api_instructions;
    std::atomic<DevicePhase> phase;
    std::mutex latest_img_mutex;
    std::string latest_img_path;
    std::string latest_img_base64;
    std::atomic<bool> video_streaming;
    std::mutex video_mutex;
    std::condition_variable video_cv;
    std::vector<unsigned char> last_video_frame;
};

AppState g_state;

// ----------- HTTP API Handlers ------------

void handle_capture(const httplib::Request& req, httplib::Response& res) {
    if (!g_state.camera->isConnected()) {
        res.status = 500;
        res.set_content("{\"error\":\"Camera not connected\"}", "application/json");
        return;
    }
    std::string file_path, base64_img;
    if (!g_state.camera->captureImage(file_path, base64_img)) {
        res.status = 500;
        res.set_content("{\"error\":\"Capture failed\"}", "application/json");
        return;
    }
    {
        std::lock_guard<std::mutex> lock(g_state.latest_img_mutex);
        g_state.latest_img_path = file_path;
        g_state.latest_img_base64 = base64_img;
    }
    json resp;
    resp["status"] = "success";
    resp["file_path"] = file_path;
    resp["base64"] = base64_img;
    res.set_content(resp.dump(), "application/json");
}

void handle_get_image(const httplib::Request& req, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(g_state.latest_img_mutex);
    if (g_state.latest_img_base64.empty()) {
        res.status = 404;
        res.set_content("{\"error\":\"No image captured yet\"}", "application/json");
        return;
    }
    json resp;
    resp["file_path"] = g_state.latest_img_path;
    resp["base64"] = g_state.latest_img_base64;
    res.set_content(resp.dump(), "application/json");
}

void handle_video_start(const httplib::Request& req, httplib::Response& res) {
    if (!g_state.camera->isConnected()) {
        res.status = 500;
        res.set_content("{\"error\":\"Camera not connected\"}", "application/json");
        return;
    }
    if (!g_state.camera->startVideoStream()) {
        res.status = 500;
        res.set_content("{\"error\":\"Failed to start video\"}", "application/json");
        return;
    }
    g_state.video_streaming = true;
    json resp;
    resp["status"] = "streaming";
    resp["url"] = "/camera/video/stream";
    res.set_content(resp.dump(), "application/json");
}

void handle_video_stop(const httplib::Request& req, httplib::Response& res) {
    if (!g_state.camera->isConnected()) {
        res.status = 500;
        res.set_content("{\"error\":\"Camera not connected\"}", "application/json");
        return;
    }
    if (!g_state.camera->stopVideoStream()) {
        res.status = 500;
        res.set_content("{\"error\":\"Failed to stop video\"}", "application/json");
        return;
    }
    g_state.video_streaming = false;
    json resp;
    resp["status"] = "stopped";
    res.set_content(resp.dump(), "application/json");
}

void handle_video_stream(const httplib::Request& req, httplib::Response& res) {
    if (!g_state.camera->isConnected() || !g_state.video_streaming) {
        res.status = 404;
        res.set_content("No video stream", "text/plain");
        return;
    }
    res.set_header("Cache-Control", "no-cache");
    res.set_header("Connection", "close");
    res.set_header("Content-Type", "multipart/x-mixed-replace; boundary=frame");
    res.status = 200;
    httplib::DataSink& sink = res.sink();
    while (g_state.video_streaming && g_state.camera->isStreaming()) {
        std::vector<unsigned char> jpeg;
        if (!g_state.camera->readVideoFrame(jpeg)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            continue;
        }
        std::ostringstream oss;
        oss << "--frame\r\n"
            << "Content-Type: image/jpeg\r\n"
            << "Content-Length: " << jpeg.size() << "\r\n\r\n";
        sink.write(oss.str().c_str(), oss.str().size());
        sink.write((const char*)jpeg.data(), jpeg.size());
        sink.write("\r\n", 2);
        sink.flush();
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        if (!sink.is_writable()) break;
    }
}

// ----------- EdgeDevice Phase Maintenance ------------

void phase_maintainer(std::string name, std::string ns, UARTCamera* camera) {
    K8sClient k8s;
    DevicePhase last_phase = Unknown;
    while (true) {
        DevicePhase cur_phase = Unknown;
        if (!camera->isConnected()) {
            cur_phase = Pending;
        } else if (camera->isStreaming()) {
            cur_phase = Running;
        } else {
            cur_phase = Running;
        }
        if (cur_phase != last_phase) {
            k8s.patchEdgeDeviceStatus(name, ns, cur_phase);
            last_phase = cur_phase;
        }
        g_state.phase = cur_phase;
        std::this_thread::sleep_for(std::chrono::seconds(3));
    }
}

// ----------- Main ------------

int main(int argc, char* argv[]) {
    // Load env
    const char* dev_name = std::getenv("EDGEDEVICE_NAME");
    const char* dev_ns = std::getenv("EDGEDEVICE_NAMESPACE");
    const char* server_host = std::getenv("SERVER_HOST");
    const char* server_port = std::getenv("SERVER_PORT");
    const char* uart_path = std::getenv("UART_PATH");
    const char* baudrate_env = std::getenv("UART_BAUDRATE");
    int baudrate = baudrate_env ? std::stoi(baudrate_env) : B115200;
    if (!dev_name || !dev_ns || !server_host || !server_port || !uart_path) {
        std::cerr << "Missing required environment variables.\n";
        return 1;
    }
    g_state.device_name = dev_name;
    g_state.device_namespace = dev_ns;

    // Get device address from CRD
    K8sClient k8s;
    std::string device_addr;
    if (!k8s.getEdgeDeviceSpecAddress(dev_name, dev_ns, device_addr)) {
        std::cerr << "Cannot fetch device address from EdgeDevice CRD\n";
        return 1;
    }
    g_state.device_address = device_addr;

    // Load API instructions
    APIInstructionDict dict;
    if (!loadAPIInstructions("/etc/edgedevice/config/instructions", dict)) {
        std::cerr << "Cannot load API instructions.\n";
    }
    g_state.api_instructions = dict;

    // Init UART camera
    UARTCamera camera(uart_path, baudrate);
    if (!camera.connect()) {
        std::cerr << "UART camera connection failed.\n";
        g_state.phase = Failed;
    } else {
        g_state.phase = Running;
    }
    g_state.camera = &camera;
    g_state.video_streaming = false;

    // Start phase maintainer thread
    std::thread phase_thread(phase_maintainer, dev_name, dev_ns, &camera);

    // HTTP Server
    httplib::Server svr;
    svr.Post("/camera/capture", handle_capture);
    svr.Get("/camera/image", handle_get_image);
    svr.Post("/camera/video/start", handle_video_start);
    svr.Post("/camera/video/stop", handle_video_stop);
    svr.Get("/camera/video/stream", handle_video_stream);

    std::string host(server_host);
    int port = std::stoi(server_port);
    std::cout << "HTTP server on " << host << ":" << port << std::endl;
    svr.listen(host.c_str(), port);

    phase_thread.join();
    camera.disconnect();
    return 0;
}