#include <iostream>
#include <fstream>
#include <sstream>
#include <thread>
#include <atomic>
#include <cstdlib>
#include <map>
#include <vector>
#include <string>
#include <mutex>
#include <condition_variable>
#include <chrono>
#include <filesystem>

#include <fcntl.h>
#include <unistd.h>
#include <termios.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <yaml-cpp/yaml.h>
#include <nlohmann/json.hpp>
#include <httplib.h>
#include <k8s-cpp/k8s.hpp>

// ----------- CONFIGURABLE ENV VARS ----------------

// Required: Device CRD info
const std::string ENV_EDGEDEVICE_NAME      = "EDGEDEVICE_NAME";
const std::string ENV_EDGEDEVICE_NAMESPACE = "EDGEDEVICE_NAMESPACE";

// DeviceShifu configuration
const std::string ENV_UART_PORT    = "DEVICE_UART_PORT";   // e.g. /dev/ttyUSB0
const std::string ENV_UART_BAUD    = "DEVICE_UART_BAUD";   // e.g. 115200
const std::string ENV_SERVER_HOST  = "SERVER_HOST";        // e.g. 0.0.0.0
const std::string ENV_SERVER_PORT  = "SERVER_PORT";        // e.g. 8080

const std::string CONFIGMAP_PATH = "/etc/edgedevice/config/instructions";

// Default values if env vars not set
const std::string DEFAULT_HOST    = "0.0.0.0";
const int         DEFAULT_PORT    = 8080;
const std::string DEFAULT_BAUD    = "115200";
const std::string DEFAULT_UART    = "/dev/ttyUSB0";

// --------- GLOBALS --------
std::mutex uart_mutex;
std::atomic<bool> video_streaming{false};
std::vector<uint8_t> latest_image;
std::vector<uint8_t> latest_video_buffer;
std::condition_variable video_cv;
std::mutex video_mutex;
std::string last_image_mime = "image/jpeg";
std::string last_image_format = "jpg";
std::string last_video_mime = "video/mp4";

// -------- YAML CONFIG LOADING --------

using ProtocolSettings = std::map<std::string, std::string>;
using ApiInstructionConfig = std::map<std::string, ProtocolSettings>;

ApiInstructionConfig load_yaml_config(const std::string& path) {
    ApiInstructionConfig config;
    if (!std::filesystem::exists(path)) return config;
    YAML::Node root = YAML::LoadFile(path);
    for (auto it = root.begin(); it != root.end(); ++it) {
        ProtocolSettings settings;
        if (it->second["protocolPropertyList"]) {
            for (auto s = it->second["protocolPropertyList"].begin(); s != it->second["protocolPropertyList"].end(); ++s) {
                settings[s->first.as<std::string>()] = s->second.as<std::string>();
            }
        }
        config[it->first.as<std::string>()] = std::move(settings);
    }
    return config;
}

// -------- UART HELPER --------

class UARTCamera {
    int fd;
    std::string device;
    int baud;

public:
    UARTCamera(const std::string& dev, int baudrate) : fd(-1), device(dev), baud(baudrate) {}

    bool open_uart() {
        fd = open(device.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
        if (fd < 0) return false;

        struct termios tty;
        memset(&tty, 0, sizeof tty);
        if (tcgetattr(fd, &tty) != 0) return false;

        cfsetospeed(&tty, baud_to_flag(baud));
        cfsetispeed(&tty, baud_to_flag(baud));

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

        if (tcsetattr(fd, TCSANOW, &tty) != 0)
            return false;
        return true;
    }

    void close_uart() {
        if (fd >= 0) close(fd);
        fd = -1;
    }

    ~UARTCamera() { close_uart(); }

    bool send_cmd(const std::vector<uint8_t>& cmd) {
        if (fd < 0) return false;
        ssize_t n = write(fd, cmd.data(), cmd.size());
        return (n == (ssize_t)cmd.size());
    }

    bool send_cmd_str(const std::string& str) {
        return send_cmd(std::vector<uint8_t>(str.begin(), str.end()));
    }

    std::vector<uint8_t> read_bytes(size_t max_bytes, int timeout_ms = 2000) {
        std::vector<uint8_t> buf;
        if (fd < 0) return buf;
        fd_set set;
        struct timeval timeout;
        size_t total_read = 0;
        while (total_read < max_bytes) {
            FD_ZERO(&set);
            FD_SET(fd, &set);
            timeout.tv_sec = timeout_ms / 1000;
            timeout.tv_usec = (timeout_ms % 1000) * 1000;
            int rv = select(fd+1, &set, NULL, NULL, &timeout);
            if (rv > 0) {
                uint8_t tmp[256];
                ssize_t r = read(fd, tmp, std::min(sizeof(tmp), max_bytes-total_read));
                if (r > 0) {
                    buf.insert(buf.end(), tmp, tmp+r);
                    total_read += r;
                } else {
                    break;
                }
            } else {
                break;
            }
        }
        return buf;
    }

    // These methods should be adjusted per your device's protocol.
    bool capture_image(std::vector<uint8_t>& image, std::string& mime, std::string& ext) {
        // Example protocol: send "CAPTURE\n", receive JPEG image raw
        std::lock_guard<std::mutex> lock(uart_mutex);
        if (!send_cmd_str("CAPTURE\n")) return false;
        // Wait for header e.g. "IMG <size>\n"
        std::string header;
        char c = 0;
        for (int i=0; i<100; ++i) {
            if (read(fd, &c, 1) != 1) return false;
            if (c == '\n') break;
            header += c;
        }
        // Expected header: "IMG <size>"
        size_t pos = header.find("IMG ");
        if (pos != 0) return false;
        size_t img_size = std::stoi(header.substr(4));
        if (img_size == 0 || img_size > 10*1024*1024) return false;
        image = read_bytes(img_size, 5000);
        // Heuristic: if starts with JPEG magic, it's jpeg
        if (image.size()>2 && image[0]==0xFF && image[1]==0xD8) {
            mime="image/jpeg"; ext="jpg";
        } else if (image.size()>8 && image[0]==0x89 && image[1]==0x50) {
            mime="image/png"; ext="png";
        } else {
            mime="application/octet-stream"; ext="bin";
        }
        return image.size() == img_size;
    }

    // Video stream: Start sending "VIDEOSTART\n", receive video chunks until "VIDEOSTOP\n"
    bool start_video_stream() {
        std::lock_guard<std::mutex> lock(uart_mutex);
        return send_cmd_str("VIDEOSTART\n");
    }
    bool stop_video_stream() {
        std::lock_guard<std::mutex> lock(uart_mutex);
        return send_cmd_str("VIDEOSTOP\n");
    }

    // This function will run in a thread to grab video data
    void stream_video(std::atomic<bool>& running, std::vector<uint8_t>& buffer) {
        buffer.clear();
        while (running) {
            auto chunk = read_bytes(1024, 200);
            if (!chunk.empty()) {
                std::lock_guard<std::mutex> lk(video_mutex);
                buffer.insert(buffer.end(), chunk.begin(), chunk.end());
                video_cv.notify_all();
            }
        }
    }

private:
    speed_t baud_to_flag(int baud) {
        switch (baud) {
            case 9600: return B9600;
            case 19200: return B19200;
            case 38400: return B38400;
            case 57600: return B57600;
            case 115200: return B115200;
            default: return B115200;
        }
    }
};

// --------- K8S CRD CLIENT --------

class K8sEdgeDeviceStatusUpdater {
    std::string name, ns;
    k8s::Client client;
    nlohmann::json dev_obj;
    std::string resource_uri;

public:
    K8sEdgeDeviceStatusUpdater(const std::string& name_, const std::string& ns_)
        : name(name_), ns(ns_), client(k8s::in_cluster_config()) {
        resource_uri = "/apis/shifu.edgenesis.io/v1alpha1/namespaces/" + ns + "/edgedevices/" + name;
    }

    bool update_phase(const std::string& phase) {
        // Patch resource status
        nlohmann::json patch = {
            {"status", {
                {"edgeDevicePhase", phase}
            }}
        };
        try {
            client.patch(resource_uri, patch, "application/merge-patch+json");
            return true;
        } catch (...) {
            return false;
        }
    }

    std::string get_address() {
        try {
            auto response = client.get(resource_uri);
            if (response.contains("spec") && response["spec"].contains("address"))
                return response["spec"]["address"];
        } catch (...) {}
        return "";
    }
};

// --------- HTTP API HANDLERS --------

// API: POST /camera/video/start, POST /commands/video/start
void handle_video_start(httplib::Request& req, httplib::Response& res, UARTCamera& camera) {
    if (video_streaming.exchange(true)) {
        res.status = 409;
        res.set_content(R"({"status":"already running"})", "application/json");
        return;
    }
    if (!camera.start_video_stream()) {
        video_streaming = false;
        res.status = 500;
        res.set_content(R"({"status":"failed to start video"})", "application/json");
        return;
    }
    std::thread([&camera](){
        camera.stream_video(video_streaming, latest_video_buffer);
    }).detach();
    res.set_content(R"({"status":"running"})", "application/json");
}

// API: POST /camera/video/stop, POST /commands/video/stop
void handle_video_stop(httplib::Request& req, httplib::Response& res, UARTCamera& camera) {
    if (!video_streaming.exchange(false)) {
        res.status = 409;
        res.set_content(R"({"status":"not running"})", "application/json");
        return;
    }
    camera.stop_video_stream();
    res.set_content(R"({"status":"stopped"})", "application/json");
}

// API: GET /camera/video
void handle_video_stream(httplib::Request& req, httplib::Response& res) {
    if (!video_streaming) {
        res.status = 404;
        res.set_content(R"({"status":"not streaming"})", "application/json");
        return;
    }
    res.set_header("Content-Type", last_video_mime);
    res.set_chunked_content_provider(
        last_video_mime,
        [](size_t /*offset*/, httplib::DataSink &sink) {
            // Stream out video chunks
            std::unique_lock<std::mutex> lk(video_mutex);
            while (video_streaming) {
                video_cv.wait_for(lk, std::chrono::milliseconds(300));
                if (!latest_video_buffer.empty()) {
                    sink.write((const char*)latest_video_buffer.data(), latest_video_buffer.size());
                    latest_video_buffer.clear();
                }
            }
            sink.done();
            return true;
        }
    );
}

// API: POST /camera/capture, POST /commands/capture
void handle_capture(httplib::Request& req, httplib::Response& res, UARTCamera& camera) {
    std::vector<uint8_t> img;
    std::string mime, ext;
    if (!camera.capture_image(img, mime, ext)) {
        res.status = 500;
        res.set_content(R"({"status":"capture failed"})", "application/json");
        return;
    }
    {
        std::lock_guard<std::mutex> lk(video_mutex);
        latest_image = img;
        last_image_mime = mime;
        last_image_format = ext;
    }
    nlohmann::json jres = {
        {"status", "ok"},
        {"size", img.size()},
        {"format", ext},
        {"mime", mime}
    };
    res.set_content(jres.dump(), "application/json");
}

// API: GET /camera/image
void handle_get_image(httplib::Request& req, httplib::Response& res) {
    std::lock_guard<std::mutex> lk(video_mutex);
    if (latest_image.empty()) {
        res.status = 404;
        res.set_content(R"({"status":"no image"})", "application/json");
        return;
    }
    std::string param = req.get_param_value("format");
    if (param == "json") {
        // Base64 encode and return as JSON
        std::string b64 = nlohmann::json::to_bson(latest_image).dump();
        nlohmann::json jres = {
            {"status","ok"},
            {"data", b64},
            {"mime", last_image_mime}
        };
        res.set_content(jres.dump(), "application/json");
    } else {
        res.set_header("Content-Type", last_image_mime);
        res.set_content((const char*)latest_image.data(), latest_image.size(), last_image_mime.c_str());
    }
}

// ----------- MAIN -------------
int main() {
    // Load env vars
    std::string edge_name      = getenv(ENV_EDGEDEVICE_NAME.c_str())      ? getenv(ENV_EDGEDEVICE_NAME.c_str())      : "";
    std::string edge_namespace = getenv(ENV_EDGEDEVICE_NAMESPACE.c_str()) ? getenv(ENV_EDGEDEVICE_NAMESPACE.c_str()) : "";
    std::string uart_port      = getenv(ENV_UART_PORT.c_str())            ? getenv(ENV_UART_PORT.c_str())            : DEFAULT_UART;
    std::string uart_baud_str  = getenv(ENV_UART_BAUD.c_str())            ? getenv(ENV_UART_BAUD.c_str())            : DEFAULT_BAUD;
    std::string host           = getenv(ENV_SERVER_HOST.c_str())          ? getenv(ENV_SERVER_HOST.c_str())          : DEFAULT_HOST;
    std::string port_str       = getenv(ENV_SERVER_PORT.c_str())          ? getenv(ENV_SERVER_PORT.c_str())          : std::to_string(DEFAULT_PORT);

    if (edge_name.empty() || edge_namespace.empty()) {
        std::cerr << "EDGEDEVICE_NAME and EDGEDEVICE_NAMESPACE are required\n";
        return 2;
    }

    int uart_baud = std::stoi(uart_baud_str);
    int server_port = std::stoi(port_str);

    // Load config
    auto api_config = load_yaml_config(CONFIGMAP_PATH);

    // Set up K8s CRD updater
    K8sEdgeDeviceStatusUpdater k8s_updater(edge_name, edge_namespace);

    // UART camera
    UARTCamera camera(uart_port, uart_baud);

    // Probe device
    std::string phase = "Pending";
    if (camera.open_uart()) {
        phase = "Running";
    } else {
        phase = "Failed";
    }
    k8s_updater.update_phase(phase);

    // Start HTTP server
    httplib::Server svr;

    svr.Post("/camera/video/start", [&](const httplib::Request& req, httplib::Response& res) {
        handle_video_start(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Post("/commands/video/start", [&](const httplib::Request& req, httplib::Response& res) {
        handle_video_start(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Post("/camera/video/stop", [&](const httplib::Request& req, httplib::Response& res) {
        handle_video_stop(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Post("/commands/video/stop", [&](const httplib::Request& req, httplib::Response& res) {
        handle_video_stop(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Post("/camera/capture", [&](const httplib::Request& req, httplib::Response& res) {
        handle_capture(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Post("/commands/capture", [&](const httplib::Request& req, httplib::Response& res) {
        handle_capture(const_cast<httplib::Request&>(req), res, camera);
    });
    svr.Get("/camera/image", [&](const httplib::Request& req, httplib::Response& res) {
        handle_get_image(const_cast<httplib::Request&>(req), res);
    });
    svr.Get("/camera/video", [&](const httplib::Request& req, httplib::Response& res) {
        handle_video_stream(const_cast<httplib::Request&>(req), res);
    });

    // Health endpoint
    svr.Get("/healthz", [](const httplib::Request&, httplib::Response& res) {
        res.set_content(R"({"status":"ok"})", "application/json");
    });

    std::cout << "Starting HTTP server on " << host << ":" << server_port << std::endl;
    svr.listen(host.c_str(), server_port);

    camera.close_uart();
    return 0;
}