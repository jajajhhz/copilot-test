#include <iostream>
#include <fstream>
#include <string>
#include <thread>
#include <chrono>
#include <map>
#include <atomic>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <filesystem>
#include <csignal>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <httplib.h>
#include <curl/curl.h>

using json = nlohmann::json;
namespace fs = std::filesystem;

// --- CONFIGURATION ---

const std::string INSTRUCTIONS_PATH = "/etc/edgedevice/config/instructions";
const std::string K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token";
const std::string K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
const std::string K8S_API = "https://kubernetes.default.svc";

// --- UART UTILS ---
class UART {
    int fd = -1;
    std::string device;
    int baudrate;
    std::mutex uart_mutex;

public:
    UART(const std::string& device, int baudrate) : device(device), baudrate(baudrate) {}
    bool open_port() {
        std::lock_guard<std::mutex> lock(uart_mutex);
        fd = ::open(device.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
        if (fd < 0) return false;
        struct termios tty;
        memset(&tty, 0, sizeof tty);
        if (tcgetattr(fd, &tty) != 0) return false;
        cfsetospeed(&tty, baudrate);
        cfsetispeed(&tty, baudrate);
        tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
        tty.c_iflag &= ~IGNBRK;
        tty.c_lflag = 0;
        tty.c_oflag = 0;
        tty.c_cc[VMIN]  = 1;
        tty.c_cc[VTIME] = 5;
        tty.c_iflag &= ~(IXON | IXOFF | IXANY);
        tty.c_cflag |= (CLOCAL | CREAD);
        tty.c_cflag &= ~(PARENB | PARODD);
        tty.c_cflag &= ~CSTOPB;
        tty.c_cflag &= ~CRTSCTS;
        if (tcsetattr(fd, TCSANOW, &tty) != 0) return false;
        return true;
    }
    ssize_t write_bytes(const void* buf, size_t count) {
        std::lock_guard<std::mutex> lock(uart_mutex);
        if (fd < 0) return -1;
        return ::write(fd, buf, count);
    }
    ssize_t read_bytes(void* buf, size_t count) {
        std::lock_guard<std::mutex> lock(uart_mutex);
        if (fd < 0) return -1;
        return ::read(fd, buf, count);
    }
    void close_port() {
        std::lock_guard<std::mutex> lock(uart_mutex);
        if (fd >= 0) { ::close(fd); fd = -1; }
    }
    ~UART() { close_port(); }
};

// --- YAML INSTRUCTION LOADING ---
struct ApiSettings {
    std::map<std::string, std::string> protocolPropertyList;
};
std::map<std::string, ApiSettings> load_api_settings(const std::string& path) {
    std::map<std::string, ApiSettings> settings;
    if (!fs::exists(path)) return settings;
    YAML::Node config = YAML::LoadFile(path);
    for (auto it = config.begin(); it != config.end(); ++it) {
        ApiSettings api;
        if (it->second["protocolPropertyList"]) {
            for (auto prop = it->second["protocolPropertyList"].begin();
                 prop != it->second["protocolPropertyList"].end(); ++prop) {
                api.protocolPropertyList[prop->first.Scalar()] = prop->second.Scalar();
            }
        }
        settings[it->first.Scalar()] = api;
    }
    return settings;
}

// --- KUBERNETES CLIENT (minimal) ---
class K8sClient {
    std::string token;
    std::string ca_path;
    std::string api_server;

public:
    K8sClient(const std::string& api_server, const std::string& token_path, const std::string& ca_path)
        : api_server(api_server), ca_path(ca_path) {
        std::ifstream t(token_path);
        token = std::string((std::istreambuf_iterator<char>(t)), std::istreambuf_iterator<char>());
    }

    bool patch_status(const std::string& ns, const std::string& name, const std::string& phase) {
        std::string url = api_server + "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + ns + "/edgedevices/" + name + "/status";
        std::string data = "{\"status\":{\"edgeDevicePhase\":\"" + phase + "\"}}";

        struct curl_slist* headers = NULL;
        headers = curl_slist_append(headers, ("Authorization: Bearer " + token).c_str());
        headers = curl_slist_append(headers, "Accept: application/json");
        headers = curl_slist_append(headers, "Content-Type: application/merge-patch+json");

        CURL* curl = curl_easy_init();
        if (!curl) return false;

        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_path.c_str());
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PATCH");
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, data.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, data.size());
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L); // For in-cluster

        long resp_code = 0;
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &resp_code);

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        return (res == CURLE_OK) && (resp_code >= 200 && resp_code < 300);
    }
};

// --- CAMERA SIMULATION LOGIC ---
class CameraDriver {
    UART uart;
    std::string image_dir;
    std::atomic<bool> streaming;
    std::mutex frame_mutex;
    std::vector<uint8_t> last_frame;
    std::condition_variable stream_cv;

public:
    CameraDriver(const std::string& uart_dev, int baud, const std::string& img_dir)
        : uart(uart_dev, baud), image_dir(img_dir), streaming(false) {}

    bool connect() { return uart.open_port(); }
    void disconnect() { uart.close_port(); }

    // Simulate sending UART command to capture image and reading back raw image
    bool capture_image(std::vector<uint8_t>& img_data) {
        static const char* CMD = "CAPTURE\n";
        if (uart.write_bytes(CMD, strlen(CMD)) < 0) return false;
        // Assume camera sends image size (4 bytes, big endian), then raw data.
        uint32_t img_size = 0;
        if (uart.read_bytes(&img_size, 4) != 4) return false;
        img_size = ntohl(img_size);
        img_data.resize(img_size);
        size_t read_total = 0;
        while (read_total < img_size) {
            ssize_t chunk = uart.read_bytes(img_data.data() + read_total, img_size - read_total);
            if (chunk <= 0) return false;
            read_total += chunk;
        }
        // Save to file for demonstration
        std::string file_name = image_dir + "/capture_" + std::to_string(time(nullptr)) + ".jpg";
        std::ofstream img_file(file_name, std::ios::binary);
        img_file.write(reinterpret_cast<char*>(img_data.data()), img_data.size());
        img_file.close();
        return true;
    }

    // Simulate video: reading frames periodically and storing for HTTP streaming
    void start_video() {
        streaming = true;
        std::thread([this]() {
            while (streaming) {
                std::vector<uint8_t> img;
                if (capture_image(img)) {
                    {
                        std::lock_guard<std::mutex> lock(frame_mutex);
                        last_frame = img;
                    }
                    stream_cv.notify_all();
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        }).detach();
    }

    void stop_video() { streaming = false; }

    bool get_latest_frame(std::vector<uint8_t>& img) {
        std::lock_guard<std::mutex> lock(frame_mutex);
        if (last_frame.empty()) return false;
        img = last_frame;
        return true;
    }

    bool is_streaming() const { return streaming; }
};

// --- UTILITY: BASE64 ENCODE ---
static const char b64_table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
std::string base64_encode(const std::vector<uint8_t>& data) {
    std::string out;
    int val = 0, valb = -6;
    for (uint8_t c : data) {
        val = (val << 8) + c;
        valb += 8;
        while (valb >= 0) {
            out.push_back(b64_table[(val >> valb) & 0x3F]);
            valb -= 6;
        }
    }
    if (valb > -6) out.push_back(b64_table[((val << 8) >> (valb + 8)) & 0x3F]);
    while (out.size() % 4) out.push_back('=');
    return out;
}

// --- HTTP SERVER LOGIC ---

int main() {
    // --- ENVIRONMENT CONFIG ---
    const std::string edgedevice_name = getenv("EDGEDEVICE_NAME") ? getenv("EDGEDEVICE_NAME") : "";
    const std::string edgedevice_namespace = getenv("EDGEDEVICE_NAMESPACE") ? getenv("EDGEDEVICE_NAMESPACE") : "";
    const std::string server_host = getenv("SHIFU_HTTP_HOST") ? getenv("SHIFU_HTTP_HOST") : "0.0.0.0";
    const int server_port = getenv("SHIFU_HTTP_PORT") ? std::stoi(getenv("SHIFU_HTTP_PORT")) : 8080;
    const std::string uart_device = getenv("SHIFU_UART_DEVICE") ? getenv("SHIFU_UART_DEVICE") : "/dev/ttyS0";
    const int uart_baud = getenv("SHIFU_UART_BAUD") ? std::stoi(getenv("SHIFU_UART_BAUD")) : B115200;
    const std::string image_dir = getenv("SHIFU_IMAGE_DIR") ? getenv("SHIFU_IMAGE_DIR") : "/tmp/camera_images";

    fs::create_directories(image_dir);

    K8sClient k8s(K8S_API, K8S_TOKEN_PATH, K8S_CA_PATH);

    auto api_settings = load_api_settings(INSTRUCTIONS_PATH);

    CameraDriver camera(uart_device, uart_baud, image_dir);

    std::atomic<bool> running{true};
    std::atomic<bool> connected{false};

    // --- DEVICE STATUS UPDATE THREAD ---
    std::thread([&]() {
        std::string last_phase = "Unknown";
        while (running) {
            std::string phase = "Unknown";
            if (!connected) phase = "Pending";
            else if (camera.is_streaming()) phase = "Running";
            else phase = "Pending";
            if (last_phase != phase) {
                k8s.patch_status(edgedevice_namespace, edgedevice_name, phase);
                last_phase = phase;
            }
            std::this_thread::sleep_for(std::chrono::seconds(5));
        }
    }).detach();

    // --- CONNECT TO CAMERA VIA UART ---
    if (camera.connect()) {
        connected = true;
        k8s.patch_status(edgedevice_namespace, edgedevice_name, "Pending");
    } else {
        connected = false;
        k8s.patch_status(edgedevice_namespace, edgedevice_name, "Failed");
        std::cerr << "Failed to connect to UART camera device." << std::endl;
        return 1;
    }

    // --- HTTP SERVER ---
    httplib::Server svr;

    // POST /camera/capture
    svr.Post("/camera/capture", [&](const httplib::Request& req, httplib::Response& res) {
        std::vector<uint8_t> img;
        if (!camera.capture_image(img)) {
            res.status = 500;
            res.set_content(R"({"status":"error","message":"Failed to capture image"})", "application/json");
            return;
        }
        std::string b64 = base64_encode(img);
        json resp = {
            {"status", "success"},
            {"image_base64", b64},
            {"length", img.size()}
        };
        res.set_content(resp.dump(), "application/json");
    });

    // GET /camera/image
    svr.Get("/camera/image", [&](const httplib::Request& req, httplib::Response& res) {
        std::vector<uint8_t> img;
        if (!camera.get_latest_frame(img)) {
            res.status = 404;
            res.set_content(R"({"status":"error","message":"No image available"})", "application/json");
            return;
        }
        res.set_content(reinterpret_cast<const char*>(img.data()), img.size(), "image/jpeg");
    });

    // POST /camera/video/start
    svr.Post("/camera/video/start", [&](const httplib::Request& req, httplib::Response& res) {
        if (!camera.is_streaming()) camera.start_video();
        json resp = {
            {"status", "success"},
            {"stream_url", "/camera/video/stream"}
        };
        res.set_content(resp.dump(), "application/json");
    });

    // POST /camera/video/stop
    svr.Post("/camera/video/stop", [&](const httplib::Request& req, httplib::Response& res) {
        camera.stop_video();
        json resp = {
            {"status", "success"},
            {"message", "Video streaming stopped"}
        };
        res.set_content(resp.dump(), "application/json");
    });

    // GET /camera/video/stream (MJPEG) - browser and CLI friendly
    svr.Get("/camera/video/stream", [&](const httplib::Request& req, httplib::Response& res) {
        res.set_header("Cache-Control", "no-cache");
        res.set_header("Connection", "close");
        res.set_header("Content-Type", "multipart/x-mixed-replace; boundary=frame");
        httplib::DataSink& sink = res.sink();
        while (camera.is_streaming()) {
            std::vector<uint8_t> frame;
            if (camera.get_latest_frame(frame)) {
                std::ostringstream ss;
                ss << "--frame\r\n"
                   << "Content-Type: image/jpeg\r\n"
                   << "Content-Length: " << frame.size() << "\r\n\r\n";
                sink.write(ss.str().c_str(), ss.str().size());
                sink.write(reinterpret_cast<const char*>(frame.data()), frame.size());
                sink.write("\r\n", 2);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        sink.done();
    });

    // --- SIGNAL HANDLING ---
    std::signal(SIGINT, [](int) { std::exit(0); });

    std::cout << "Camera DeviceShifu HTTP Server running at " << server_host << ":" << server_port << std::endl;
    svr.listen(server_host.c_str(), server_port);

    running = false;
    camera.disconnect();
    return 0;
}