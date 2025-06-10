#include <iostream>
#include <sstream>
#include <fstream>
#include <string>
#include <cstdlib>
#include <thread>
#include <atomic>
#include <chrono>
#include <unordered_map>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <algorithm>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <curl/curl.h>

// Single-header HTTP server library (C++11, MIT License)
// https://github.com/yhirose/cpp-httplib
#include "httplib.h"

using json = nlohmann::json;

// --- UART Communication ---

class UARTDevice {
public:
    UARTDevice(const std::string& dev, int baud) : device_path(dev), baud_rate(baud), fd(-1) {}

    bool open_port() {
        fd = open(device_path.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
        if (fd < 0) return false;
        struct termios tty;
        if (tcgetattr(fd, &tty) != 0) { close(fd); fd = -1; return false; }
        cfsetospeed(&tty, baud_rate_to_flag(baud_rate));
        cfsetispeed(&tty, baud_rate_to_flag(baud_rate));
        tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;     // 8-bit chars
        tty.c_iflag &= ~IGNBRK;         // disable break processing
        tty.c_lflag = 0;                // no signaling chars, no echo,
        tty.c_oflag = 0;                // no remapping, no delays
        tty.c_cc[VMIN]  = 0;            // read doesn't block
        tty.c_cc[VTIME] = 5;            // 0.5 seconds read timeout
        tty.c_cflag |= (CLOCAL | CREAD);// ignore modem controls, enable reading
        tty.c_cflag &= ~(PARENB | PARODD);      // shut off parity
        tty.c_cflag &= ~CSTOPB;
        tty.c_cflag &= ~CRTSCTS;
        if (tcsetattr(fd, TCSANOW, &tty) != 0) { close(fd); fd = -1; return false; }
        return true;
    }

    void close_port() {
        if (fd >= 0) { close(fd); fd = -1; }
    }

    bool is_open() const { return fd >= 0; }

    int write_data(const std::string& data) {
        if (fd < 0) return -1;
        return ::write(fd, data.data(), data.size());
    }

    int read_data(char* buf, size_t bufsize) {
        if (fd < 0) return -1;
        return ::read(fd, buf, bufsize);
    }

    ~UARTDevice() { close_port(); }

private:
    std::string device_path;
    int baud_rate;
    int fd;

    static speed_t baud_rate_to_flag(int baud)
    {
        switch (baud) {
            case 9600: return B9600;
            case 19200: return B19200;
            case 38400: return B38400;
            case 57600: return B57600;
            case 115200: return B115200;
            default: return B9600;
        }
    }
};

// --- EdgeDevice CRD Kubernetes API Client ---

class K8sClient {
public:
    K8sClient() {
        token = get_token();
        ca_cert = get_ca_cert();
        k8s_host = std::getenv("KUBERNETES_SERVICE_HOST") ? std::getenv("KUBERNETES_SERVICE_HOST") : "kubernetes.default.svc";
        k8s_port = std::getenv("KUBERNETES_SERVICE_PORT") ? std::getenv("KUBERNETES_SERVICE_PORT") : "443";
        curl_global_init(CURL_GLOBAL_ALL);
    }
    ~K8sClient() { curl_global_cleanup(); }

    json get_edgedevice(const std::string& ns, const std::string& name) {
        std::string url = "https://" + k8s_host + ":" + k8s_port + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + ns + "/edgedevices/" + name;
        std::string resp;
        http_req("GET", url, "", resp);
        if (!resp.empty()) return json::parse(resp, nullptr, false);
        return json();
    }

    bool patch_status(const std::string& ns, const std::string& name, const std::string& phase) {
        std::string url = "https://" + k8s_host + ":" + k8s_port + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + ns + "/edgedevices/" + name + "/status";
        json patch = {
            {"status", { {"edgeDevicePhase", phase } } }
        };
        std::string resp;
        return http_req("PATCH", url, patch.dump(), resp, "application/merge-patch+json");
    }

private:
    std::string token, ca_cert, k8s_host, k8s_port;

    static std::string get_token() {
        std::ifstream f("/var/run/secrets/kubernetes.io/serviceaccount/token");
        std::stringstream ss;
        ss << f.rdbuf();
        return ss.str();
    }
    static std::string get_ca_cert() {
        std::ifstream f("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt");
        std::stringstream ss;
        ss << f.rdbuf();
        return ss.str();
    }
    bool http_req(const std::string& method, const std::string& url, const std::string& body, std::string& out, const std::string& content_type = "application/json") {
        CURL* curl = curl_easy_init();
        if (!curl) return false;
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token).c_str());
        headers = curl_slist_append(headers, ("Content-Type: " + content_type).c_str());
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_CAINFO, "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt");
        if (method == "PATCH") curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PATCH");
        if (method == "POST") curl_easy_setopt(curl, CURLOPT_POST, 1L);
        if (method == "PATCH" || method == "POST") {
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
        }
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_fn);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &out);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
        CURLcode res = curl_easy_perform(curl);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
        return res == CURLE_OK;
    }
    static size_t write_fn(void* ptr, size_t size, size_t nmemb, void* userdata) {
        ((std::string*)userdata)->append((char*)ptr, size * nmemb);
        return size * nmemb;
    }
};

// --- ConfigMap Loader ---

struct APISettings {
    std::unordered_map<std::string, std::string> protocolPropertyList;
};

class ConfigLoader {
public:
    bool load(const std::string& path) {
        try {
            YAML::Node root = YAML::LoadFile(path);
            for (auto it = root.begin(); it != root.end(); ++it) {
                APISettings setting;
                if (it->second["protocolPropertyList"]) {
                    for (auto pit = it->second["protocolPropertyList"].begin(); pit != it->second["protocolPropertyList"].end(); ++pit) {
                        setting.protocolPropertyList[pit->first.as<std::string>()] = pit->second.as<std::string>();
                    }
                }
                apis[it->first.as<std::string>()] = setting;
            }
            return true;
        } catch (...) { return false; }
    }
    const APISettings* get_api(const std::string& name) const {
        auto it = apis.find(name);
        if (it == apis.end()) return nullptr;
        return &it->second;
    }
private:
    std::unordered_map<std::string, APISettings> apis;
};

// --- Camera State Management ---

enum class CameraVideoState {
    STOPPED,
    RUNNING
};

struct ImageData {
    std::vector<uint8_t> image;
    std::string timestamp;
};

struct VideoFrame {
    std::vector<uint8_t> frame;
    std::string timestamp;
};

class CameraController {
public:
    CameraController(UARTDevice& uart)
        : uartdev(uart), video_state(CameraVideoState::STOPPED), exit_flag(false)
    {}

    bool start_video() {
        std::unique_lock<std::mutex> lock(mtx);
        if (video_state == CameraVideoState::RUNNING) return true;
        // send UART command to start video
        if (!uartdev.is_open()) return false;
        if (uartdev.write_data("START_VIDEO\n") < 0) return false;
        video_state = CameraVideoState::RUNNING;
        // spawn video thread
        exit_flag = false;
        video_thread = std::thread([this](){ this->video_loop(); });
        return true;
    }

    bool stop_video() {
        std::unique_lock<std::mutex> lock(mtx);
        if (video_state == CameraVideoState::STOPPED) return true;
        if (!uartdev.is_open()) return false;
        if (uartdev.write_data("STOP_VIDEO\n") < 0) return false;
        video_state = CameraVideoState::STOPPED;
        exit_flag = true;
        cv.notify_all();
        if (video_thread.joinable()) video_thread.join();
        return true;
    }

    bool capture_image() {
        std::unique_lock<std::mutex> lock(mtx);
        if (!uartdev.is_open()) return false;
        if (uartdev.write_data("CAPTURE_IMAGE\n") < 0) return false;
        std::vector<uint8_t> img;
        std::string ts = now_str();
        // Simulate: read image (in real world, parse UART protocol and image size!)
        char buf[4096];
        int n = uartdev.read_data(buf, sizeof(buf));
        if (n > 0) img.insert(img.end(), buf, buf+n);
        last_image = {img, ts};
        return !img.empty();
    }

    ImageData get_last_image() {
        std::unique_lock<std::mutex> lock(mtx);
        return last_image;
    }

    void video_loop() {
        while (!exit_flag) {
            // Simulate: read video frames from UART
            char buf[4096];
            int n = uartdev.read_data(buf, sizeof(buf));
            if (n > 0) {
                std::vector<uint8_t> frame(buf, buf+n);
                std::string ts = now_str();
                std::unique_lock<std::mutex> lock(mtx);
                latest_video_frame = {frame, ts};
                cv.notify_all();
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(30));
            }
        }
    }

    VideoFrame get_latest_video_frame() {
        std::unique_lock<std::mutex> lock(mtx);
        return latest_video_frame;
    }

    bool is_video_running() {
        std::unique_lock<std::mutex> lock(mtx);
        return video_state == CameraVideoState::RUNNING;
    }

    void shutdown() {
        stop_video();
    }

private:
    UARTDevice& uartdev;
    std::mutex mtx;
    std::condition_variable cv;
    CameraVideoState video_state;
    std::thread video_thread;
    std::atomic<bool> exit_flag;
    ImageData last_image;
    VideoFrame latest_video_frame;

    static std::string now_str() {
        std::time_t t = std::time(nullptr);
        char buf[32];
        strftime(buf, sizeof(buf), "%FT%TZ", gmtime(&t));
        return buf;
    }
};

// --- Main Application ---

int main() {
    // --- Environment ---
    const char* ns_env = std::getenv("EDGEDEVICE_NAMESPACE");
    const char* name_env = std::getenv("EDGEDEVICE_NAME");
    if (!ns_env || !name_env) {
        std::cerr << "EDGEDEVICE_NAMESPACE and EDGEDEVICE_NAME must be set." << std::endl;
        return 1;
    }
    std::string edgedevice_ns(ns_env), edgedevice_name(name_env);
    std::string config_path = "/etc/edgedevice/config/instructions/config.yaml";

    // UART config from env
    std::string uart_dev  = std::getenv("CAMERA_UART_PORT") ? std::getenv("CAMERA_UART_PORT") : "/dev/ttyUSB0";
    int uart_baud = std::getenv("CAMERA_UART_BAUD") ? std::stoi(std::getenv("CAMERA_UART_BAUD")) : 115200;
    // HTTP Server config
    std::string http_host = std::getenv("HTTP_SERVER_HOST") ? std::getenv("HTTP_SERVER_HOST") : "0.0.0.0";
    int http_port = std::getenv("HTTP_SERVER_PORT") ? std::stoi(std::getenv("HTTP_SERVER_PORT")) : 8080;

    // --- Load ConfigMap ---
    ConfigLoader config;
    config.load(config_path);

    // --- K8s CRD Client ---
    K8sClient k8s;
    std::string edgedevice_phase = "Unknown";

    // --- EdgeDevice address ---
    std::string device_addr;
    {
        json dev = k8s.get_edgedevice(edgedevice_ns, edgedevice_name);
        if (dev.contains("spec") && dev["spec"].contains("address"))
            device_addr = dev["spec"]["address"];
    }

    // --- UART Camera ---
    UARTDevice camera(uart_dev, uart_baud);

    // --- Camera Controller ---
    CameraController camctl(camera);

    // --- Phase Management Thread ---
    std::atomic<bool> running{true};
    std::thread phase_thread([&](){
        while (running) {
            std::string new_phase;
            if (!camera.open_port()) {
                new_phase = "Pending";
            } else if (!camera.is_open()) {
                new_phase = "Failed";
            } else {
                new_phase = camctl.is_video_running() ? "Running" : "Pending";
            }
            if (edgedevice_phase != new_phase) {
                k8s.patch_status(edgedevice_ns, edgedevice_name, new_phase);
                edgedevice_phase = new_phase;
            }
            std::this_thread::sleep_for(std::chrono::seconds(5));
        }
    });

    // --- HTTP Server ---
    httplib::Server svr;

    // POST /camera/video/start
    svr.Post("/camera/video/start", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.start_video();
        json ret = { {"status", ok ? "success":"fail"}, {"phase", ok ? "Running":"Failed"} };
        res.set_content(ret.dump(), "application/json");
    });

    // POST /camera/video/stop
    svr.Post("/camera/video/stop", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.stop_video();
        json ret = { {"status", ok ? "success":"fail"}, {"phase", ok ? "Stopped":"Failed"} };
        res.set_content(ret.dump(), "application/json");
    });

    // POST /commands/video/start
    svr.Post("/commands/video/start", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.start_video();
        json ret = { {"status", ok ? "success":"fail"}, {"phase", ok ? "Running":"Failed"} };
        res.set_content(ret.dump(), "application/json");
    });

    // POST /commands/video/stop
    svr.Post("/commands/video/stop", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.stop_video();
        json ret = { {"status", ok ? "success":"fail"}, {"phase", ok ? "Stopped":"Failed"} };
        res.set_content(ret.dump(), "application/json");
    });

    // POST /commands/capture
    svr.Post("/commands/capture", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.capture_image();
        ImageData img = camctl.get_last_image();
        json ret = { {"status", ok ? "success": "fail"}, {"timestamp", img.timestamp}, {"image_size", img.image.size()} };
        res.set_content(ret.dump(), "application/json");
    });

    // POST /camera/capture
    svr.Post("/camera/capture", [&](const httplib::Request&, httplib::Response& res) {
        bool ok = camctl.capture_image();
        ImageData img = camctl.get_last_image();
        std::string base64_image = ""; // (implement base64 encode if needed)
        json ret = { {"status", ok ? "success": "fail"}, {"timestamp", img.timestamp}, {"image_base64", base64_image} };
        res.set_content(ret.dump(), "application/json");
    });

    // GET /camera/image
    svr.Get("/camera/image", [&](const httplib::Request& req, httplib::Response& res) {
        ImageData img = camctl.get_last_image();
        if (img.image.empty()) {
            res.status = 404;
            res.set_content("No image captured.", "text/plain");
            return;
        }
        res.set_content_provider("image/jpeg", img.image.size(),
            [img](size_t offset, size_t length, httplib::DataSink& sink) {
                sink.write((const char*)img.image.data() + offset, length);
                return true;
            });
    });

    // GET /camera/video
    svr.Get("/camera/video", [&](const httplib::Request&, httplib::Response& res) {
        if (!camctl.is_video_running()) {
            res.status = 404;
            res.set_content("Video not running.", "text/plain");
            return;
        }
        res.set_chunked_content_provider("video/x-motion-jpeg", [&](size_t offset, httplib::DataSink& sink) {
            while (camctl.is_video_running()) {
                VideoFrame vf = camctl.get_latest_video_frame();
                if (!vf.frame.empty()) {
                    std::ostringstream oss;
                    oss << "--frame\r\n";
                    oss << "Content-Type: image/jpeg\r\n\r\n";
                    sink.write(oss.str().c_str(), oss.str().size());
                    sink.write((const char*)vf.frame.data(), vf.frame.size());
                    sink.write("\r\n", 2);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            sink.done();
        });
    });

    // graceful shutdown
    svr.set_exception_handler([&](const auto& req, auto& res, std::exception &e) {
        res.status = 500;
        res.set_content(std::string("Internal error: ") + e.what(), "text/plain");
    });

    std::cout << "Starting HTTP server on " << http_host << ":" << http_port << std::endl;
    svr.listen(http_host.c_str(), http_port);

    // cleanup
    running = false;
    camctl.shutdown();
    if (phase_thread.joinable()) phase_thread.join();

    return 0;
}