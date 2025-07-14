```cpp
#include <iostream>
#include <cstdlib>
#include <string>
#include <sstream>
#include <vector>
#include <thread>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <fstream>
#include <chrono>
#include <opencv2/opencv.hpp>
#include <microhttpd.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

#define DEFAULT_HTTP_HOST "0.0.0.0"
#define DEFAULT_HTTP_PORT 8080
#define DEFAULT_CAMERA_INDEX 0

std::mutex camera_mutex;
std::condition_variable camera_cv;
std::atomic<bool> camera_running(false);
std::atomic<bool> streaming(false);
cv::VideoCapture camera;
int camera_index = DEFAULT_CAMERA_INDEX;

struct StreamingSession {
    std::mutex mtx;
    std::condition_variable cv;
    std::vector<unsigned char> frame;
    bool new_frame = false;
    bool stop = false;
};
std::vector<StreamingSession*> streaming_sessions;

// Environment variable helpers
std::string get_env(const char* var, const std::string& def) {
    const char* val = std::getenv(var);
    if (val) return std::string(val);
    else return def;
}

int get_env_int(const char* var, int def) {
    const char* val = std::getenv(var);
    if (val) return std::stoi(val);
    else return def;
}

void camera_capture_loop() {
    while (camera_running) {
        cv::Mat frame;
        {
            std::unique_lock<std::mutex> lk(camera_mutex);
            if (!camera.read(frame)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
        }
        std::vector<unsigned char> buf;
        cv::imencode(".jpg", frame, buf);
        // Distribute frame to all streaming sessions
        for (auto& session : streaming_sessions) {
            {
                std::lock_guard<std::mutex> lk(session->mtx);
                session->frame = buf;
                session->new_frame = true;
            }
            session->cv.notify_all();
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(30)); // ~33fps
    }
}

// Utility: Write HTTP response
int send_response(struct MHD_Connection* connection, const char* data, size_t size, const char* content_type, int status_code) {
    struct MHD_Response* response = MHD_create_response_from_buffer(size, (void*)data, MHD_RESPMEM_MUST_COPY);
    if (!response) return MHD_NO;
    MHD_add_response_header(response, "Content-Type", content_type);
    int ret = MHD_queue_response(connection, status_code, response);
    MHD_destroy_response(response);
    return ret;
}

int send_json_response(struct MHD_Connection* connection, const json& j, int status_code = MHD_HTTP_OK) {
    std::string s = j.dump();
    return send_response(connection, s.data(), s.size(), "application/json", status_code);
}

// API Handlers

int handle_camera_start(struct MHD_Connection* connection) {
    std::unique_lock<std::mutex> lk(camera_mutex);
    if (camera_running) {
        return send_json_response(connection, {{"success", true}, {"message", "Camera already started"}});
    }
    camera.open(camera_index);
    if (!camera.isOpened()) {
        camera_running = false;
        return send_json_response(connection, {{"success", false}, {"message", "Failed to open camera"}}, MHD_HTTP_INTERNAL_SERVER_ERROR);
    }
    camera_running = true;
    std::thread(camera_capture_loop).detach();
    return send_json_response(connection, {{"success", true}, {"message", "Camera started"}});
}

int handle_camera_stop(struct MHD_Connection* connection) {
    {
        std::unique_lock<std::mutex> lk(camera_mutex);
        if (!camera_running) {
            return send_json_response(connection, {{"success", true}, {"message", "Camera already stopped"}});
        }
        camera_running = false;
        camera.release();
    }
    return send_json_response(connection, {{"success", true}, {"message", "Camera stopped"}});
}

int handle_camera_capture(struct MHD_Connection* connection, struct MHD_Connection* con, const char* url, const char* method, const char* upload_data, size_t* upload_data_size, void** ptr) {
    if (!camera_running) {
        return send_json_response(connection, {{"success", false}, {"message", "Camera not started"}}, MHD_HTTP_BAD_REQUEST);
    }
    const char* format = MHD_lookup_connection_value(connection, MHD_GET_ARGUMENT_KIND, "format");
    std::string img_fmt = (format) ? std::string(format) : "jpeg";
    int imencode_flag = (img_fmt == "png") ? cv::IMWRITE_PNG_COMPRESSION : cv::IMWRITE_JPEG_QUALITY;
    std::string ext = (img_fmt == "png") ? ".png" : ".jpg";
    std::string mime = (img_fmt == "png") ? "image/png" : "image/jpeg";

    cv::Mat frame;
    {
        std::unique_lock<std::mutex> lk(camera_mutex);
        camera.read(frame);
    }
    if (frame.empty()) {
        return send_json_response(connection, {{"success", false}, {"message", "Failed to capture frame"}}, MHD_HTTP_INTERNAL_SERVER_ERROR);
    }
    std::vector<unsigned char> buf;
    cv::imencode(ext, frame, buf);
    struct MHD_Response* response = MHD_create_response_from_buffer(buf.size(), (void*)buf.data(), MHD_RESPMEM_MUST_COPY);
    if (!response) return MHD_NO;
    MHD_add_response_header(response, "Content-Type", mime.c_str());
    MHD_add_response_header(response, "Content-Disposition", ("inline; filename=\"capture" + ext + "\"").c_str());
    int ret = MHD_queue_response(connection, MHD_HTTP_OK, response);
    MHD_destroy_response(response);
    return ret;
}

// MJPEG streaming
ssize_t streaming_reader_callback(void *cls, uint64_t pos, char *buf, size_t max) {
    // Not used for streaming
    return 0;
}

int handle_camera_stream(struct MHD_Connection* connection) {
    if (!camera_running) {
        return send_json_response(connection, {{"success", false}, {"message", "Camera not started"}}, MHD_HTTP_BAD_REQUEST);
    }

    struct MHD_Response* response;
    response = MHD_create_response_from_callback(
        MHD_SIZE_UNKNOWN,
        4096,
        [](void* cls, uint64_t pos, char* buf, size_t max) -> ssize_t {
            StreamingSession* session = (StreamingSession*)cls;
            std::unique_lock<std::mutex> lk(session->mtx);
            while (!session->new_frame && !session->stop) {
                session->cv.wait(lk);
            }
            if (session->stop) return MHD_CONTENT_READER_END_OF_STREAM;
            // Write MJPEG frame
            std::ostringstream oss;
            oss << "--boundarydonotcross\r\n";
            oss << "Content-Type: image/jpeg\r\n";
            oss << "Content-Length: " << session->frame.size() << "\r\n\r\n";
            std::string header = oss.str();
            size_t to_copy = std::min(header.size(), max);
            memcpy(buf, header.data(), to_copy);
            size_t copied = to_copy;
            if (to_copy < max) {
                size_t img_to_copy = std::min(session->frame.size(), max - copied);
                memcpy(buf + copied, session->frame.data(), img_to_copy);
                copied += img_to_copy;
                if (copied < max) {
                    buf[copied++] = '\r';
                    if (copied < max) buf[copied++] = '\n';
                }
            }
            session->new_frame = false;
            return copied;
        },
        new StreamingSession(),
        [](void* cls) {
            StreamingSession* session = (StreamingSession*)cls;
            session->stop = true;
            session->cv.notify_all();
            delete session;
        }
    );
    if (!response) return MHD_NO;
    MHD_add_response_header(response, "Content-Type", "multipart/x-mixed-replace;boundary=boundarydonotcross");
    int ret = MHD_queue_response(connection, MHD_HTTP_OK, response);
    MHD_destroy_response(response);
    return ret;
}

// Dispatcher
int answer_to_connection(void *cls, struct MHD_Connection *connection,
                         const char *url, const char *method,
                         const char *version, const char *upload_data,
                         size_t *upload_data_size, void **con_cls) {
    std::string path(url);
    std::string mthd(method);

    if (path == "/camera/start" && mthd == "POST") {
        return handle_camera_start(connection);
    } else if (path == "/camera/stop" && mthd == "POST") {
        return handle_camera_stop(connection);
    } else if (path == "/camera/capture" && mthd == "GET") {
        return handle_camera_capture(connection, connection, url, method, upload_data, upload_data_size, con_cls);
    } else if (path == "/camera/stream" && mthd == "GET") {
        return handle_camera_stream(connection);
    } else {
        return send_json_response(connection, {{"success", false}, {"message", "Not Found"}}, MHD_HTTP_NOT_FOUND);
    }
}

int main(int argc, char** argv) {
    std::string http_host = get_env("HTTP_HOST", DEFAULT_HTTP_HOST);
    int http_port = get_env_int("HTTP_PORT", DEFAULT_HTTP_PORT);
    camera_index = get_env_int("CAMERA_INDEX", DEFAULT_CAMERA_INDEX);

    struct MHD_Daemon *daemon;
    daemon = MHD_start_daemon(MHD_USE_SELECT_INTERNALLY, http_port, NULL, NULL,
                              &answer_to_connection, NULL, MHD_OPTION_END);
    if (NULL == daemon) {
        std::cerr << "Failed to start HTTP server" << std::endl;
        return 1;
    }
    std::cout << "DeviceShifu Camera Driver (Logitech Brio 105) running on port " << http_port << std::endl;
    while (1) std::this_thread::sleep_for(std::chrono::seconds(10));
    MHD_stop_daemon(daemon);
    return 0;
}
```
