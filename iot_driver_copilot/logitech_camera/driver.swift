//
//  LogitechCameraHTTPDriver.swift
//
//  Swift HTTP server for USB Camera control (Logitech, Mac/Win/Linux)
//  Core endpoints: list, select, start, stop, stream, capture, record
//  Uses only Foundation, Dispatch, and AVFoundation (Apple standard SDKs)
//
//  Env vars: HTTP_HOST, HTTP_PORT, CAMERA_RES_WIDTH, CAMERA_RES_HEIGHT, CAMERA_FPS
//

import Foundation
import AVFoundation

#if os(Linux)
#error("This driver currently requires macOS (AVFoundation).")
#endif

// MARK: - Config
struct Config {
    static let host = ProcessInfo.processInfo.environment["HTTP_HOST"] ?? "0.0.0.0"
    static let port = UInt16(ProcessInfo.processInfo.environment["HTTP_PORT"] ?? "8080") ?? 8080
    static let width = Int(ProcessInfo.processInfo.environment["CAMERA_RES_WIDTH"] ?? "640") ?? 640
    static let height = Int(ProcessInfo.processInfo.environment["CAMERA_RES_HEIGHT"] ?? "480") ?? 480
    static let fps = Int(ProcessInfo.processInfo.environment["CAMERA_FPS"] ?? "15") ?? 15
}

// MARK: - Camera Management

final class CameraManager: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    static let shared = CameraManager()
    private override init() {}

    private var session: AVCaptureSession?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var deviceInput: AVCaptureDeviceInput?
    private(set) var activeCameraId: String?
    private var frameBuffer: CGImage?
    private let frameLock = NSLock()
    private var streamingClients = NSHashTable<StreamingClient>.weakObjects()

    // For recording lock
    private let recordLock = NSLock()
    private var isRecording = false

    func listCameras() -> [[String: Any]] {
        let devices = AVCaptureDevice.devices(for: .video)
        return devices.map {
            [
                "camera_id": $0.uniqueID,
                "localized_name": $0.localizedName,
                "connected": true,
                "model_id": $0.modelID ?? "",
                "manufacturer": $0.manufacturer ?? ""
            ]
        }
    }

    func selectCamera(by id: String) throws {
        try stopCamera()
        guard let device = AVCaptureDevice.devices(for: .video).first(where: { $0.uniqueID == id }) else {
            throw NSError(domain: "CameraError", code: 404, userInfo: [NSLocalizedDescriptionKey: "Camera not found"])
        }
        try activateCamera(device: device)
    }

    func startDefaultCamera() throws {
        if session != nil { throw NSError(domain: "CameraError", code: 409, userInfo: [NSLocalizedDescriptionKey: "Camera already started"]) }
        guard let device = AVCaptureDevice.devices(for: .video).first else {
            throw NSError(domain: "CameraError", code: 404, userInfo: [NSLocalizedDescriptionKey: "No camera found"])
        }
        try activateCamera(device: device)
    }

    private func activateCamera(device: AVCaptureDevice) throws {
        let session = AVCaptureSession()
        session.sessionPreset = .vga640x480

        let input = try AVCaptureDeviceInput(device: device)
        if session.canAddInput(input) { session.addInput(input) }
        let videoOutput = AVCaptureVideoDataOutput()
        videoOutput.videoSettings = [
            (kCVPixelBufferPixelFormatTypeKey as String): kCVPixelFormatType_32BGRA
        ]
        videoOutput.setSampleBufferDelegate(self, queue: DispatchQueue(label: "VideoFrameQueue"))
        if session.canAddOutput(videoOutput) { session.addOutput(videoOutput) }
        // Set resolution
        try device.lockForConfiguration()
        if device.activeFormat.isVideoStabilizationModeSupported(.off) {
            device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: CMTimeScale(Config.fps))
            device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: CMTimeScale(Config.fps))
        }
        if device.activeFormat.formatDescription.dimensions.width != Config.width ||
            device.activeFormat.formatDescription.dimensions.height != Config.height {
            // Try to select best format for resolution
            let format = device.formats.first(where: {
                let desc = $0.formatDescription
                let dims = CMVideoFormatDescriptionGetDimensions(desc)
                return Int(dims.width) == Config.width && Int(dims.height) == Config.height
            }) ?? device.activeFormat
            device.activeFormat = format
        }
        device.unlockForConfiguration()

        self.session = session
        self.deviceInput = input
        self.videoOutput = videoOutput
        self.activeCameraId = device.uniqueID
        session.startRunning()
    }

    func stopCamera() throws {
        guard let session = self.session else { return }
        session.stopRunning()
        self.session = nil
        self.deviceInput = nil
        self.videoOutput = nil
        self.activeCameraId = nil
        clearFrame()
    }

    func isCameraActive() -> Bool {
        return session?.isRunning == true
    }

    private func clearFrame() {
        frameLock.lock()
        defer { frameLock.unlock() }
        self.frameBuffer = nil
    }

    func captureFrame() throws -> (Data, [String: Any]) {
        guard isCameraActive() else {
            throw NSError(domain: "CameraError", code: 400, userInfo: [NSLocalizedDescriptionKey: "Camera not running"])
        }
        var image: CGImage?
        let timeout = DispatchTime.now() + .seconds(2)
        while image == nil && DispatchTime.now() < timeout {
            frameLock.lock()
            image = self.frameBuffer
            frameLock.unlock()
            if image == nil { usleep(10000) }
        }
        guard let cgImg = image else {
            throw NSError(domain: "CameraError", code: 504, userInfo: [NSLocalizedDescriptionKey: "Timeout waiting for frame"])
        }
        let bitmapRep = NSBitmapImageRep(cgImage: cgImg)
        guard let imgData = bitmapRep.representation(using: .jpeg, properties: [:]) else {
            throw NSError(domain: "CameraError", code: 500, userInfo: [NSLocalizedDescriptionKey: "Failed to encode image"])
        }
        let meta: [String: Any] = [
            "width": cgImg.width,
            "height": cgImg.height,
            "timestamp": Int(Date().timeIntervalSince1970)
        ]
        return (imgData, meta)
    }

    // MARK: - VideoDataOutput Delegate
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let buf = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvPixelBuffer: buf)
        let context = CIContext(options: nil)
        if let cgImg = context.createCGImage(ciImage, from: ciImage.extent) {
            frameLock.lock()
            self.frameBuffer = cgImg
            frameLock.unlock()
            let clients = streamingClients.allObjects
            for client in clients {
                client.pushFrame(cgImg: cgImg)
            }
        }
    }

    // MARK: - Streaming
    func addStreamingClient(_ client: StreamingClient) {
        streamingClients.add(client)
    }
    func removeStreamingClient(_ client: StreamingClient) {
        streamingClients.remove(client)
    }

    // MARK: - Recording
    func recordVideo(duration: Int, to url: URL, completion: @escaping (Bool, String?) -> Void) {
        recordLock.lock()
        defer { recordLock.unlock() }
        guard isCameraActive() else {
            completion(false, "Camera not running")
            return
        }
        if isRecording {
            completion(false, "Recording already in progress")
            return
        }
        isRecording = true
        let width = Config.width
        let height = Config.height
        let fps = Config.fps
        let writer: AVAssetWriter
        do {
            try? FileManager.default.removeItem(at: url)
            writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        } catch {
            isRecording = false
            completion(false, "Failed to create writer: \(error)")
            return
        }
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else {
            isRecording = false
            completion(false, "Writer can't add input")
            return
        }
        writer.add(input)
        writer.startWriting()
        writer.startSession(atSourceTime: .zero)

        let queue = DispatchQueue(label: "RecordingQueue")
        let group = DispatchGroup()
        group.enter()
        var isStopped = false
        var startTime: CMTime?
        self.frameLock.lock()
        var lastFrame: CGImage? = self.frameBuffer
        self.frameLock.unlock()

        input.requestMediaDataWhenReady(on: queue) {
            var frameCount = 0
            let frameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
            while !isStopped && input.isReadyForMoreMediaData && frameCount < duration * fps {
                self.frameLock.lock()
                guard let cgImg = self.frameBuffer ?? lastFrame else {
                    self.frameLock.unlock()
                    usleep(10000)
                    continue
                }
                lastFrame = cgImg
                self.frameLock.unlock()
                guard let sampleBuffer = cgImageToSampleBuffer(cgImg, width: width, height: height, time: CMTimeMultiply(frameDuration, multiplier: Int32(frameCount))) else {
                    usleep(10000)
                    continue
                }
                if startTime == nil {
                    startTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
                }
                input.append(sampleBuffer)
                frameCount += 1
                Thread.sleep(forTimeInterval: 1.0/Double(fps))
            }
            input.markAsFinished()
            writer.finishWriting {
                isStopped = true
                self.isRecording = false
                group.leave()
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + .seconds(duration)) {
            isStopped = true
        }
        group.notify(queue: .main) {
            completion(true, nil)
        }
    }
}

class StreamingClient: NSObject {
    private let output: OutputStream
    private let boundary = "CAMERA_MJPEG_BOUNDARY"
    private var isRunning = true
    private let lock = NSLock()
    init(output: OutputStream) {
        self.output = output
    }

    func start() {
        let header = """
        HTTP/1.1 200 OK\r
        Content-Type: multipart/x-mixed-replace; boundary=\(boundary)\r
        Cache-Control: no-cache\r
        Connection: close\r
        \r
        """
        write(header)
    }

    func pushFrame(cgImg: CGImage) {
        guard isRunning else { return }
        let bitmapRep = NSBitmapImageRep(cgImage: cgImg)
        guard let imgData = bitmapRep.representation(using: .jpeg, properties: [:]) else { return }
        let part = """
        --\(boundary)\r
        Content-Type: image/jpeg\r
        Content-Length: \(imgData.count)\r
        \r
        """
        lock.lock()
        write(part)
        _ = imgData.withUnsafeBytes { output.write($0.bindMemory(to: UInt8.self).baseAddress!, maxLength: imgData.count) }
        write("\r\n")
        lock.unlock()
    }

    func stop() {
        isRunning = false
        write("\r\n--\(boundary)--\r\n")
        output.close()
    }

    private func write(_ str: String) {
        guard let data = str.data(using: .utf8) else { return }
        _ = data.withUnsafeBytes { output.write($0.bindMemory(to: UInt8.self).baseAddress!, maxLength: data.count) }
    }
}

// MARK: - HTTP Server

final class HTTPServer {
    private let listener: FileHandle
    private let queue = DispatchQueue(label: "HTTPServerQueue")
    private var shouldRun = true

    init(host: String, port: UInt16) throws {
        let sockfd = socket(AF_INET, SOCK_STREAM, 0)
        guard sockfd >= 0 else { throw NSError(domain: "HTTPServer", code: 500, userInfo: [NSLocalizedDescriptionKey: "Socket error"]) }
        var addr = sockaddr_in(
            sin_len: UInt8(MemoryLayout<sockaddr_in>.stride),
            sin_family: sa_family_t(AF_INET),
            sin_port: CFSwapInt16HostToBig(port),
            sin_addr: in_addr(s_addr: inet_addr(host)),
            sin_zero: (0,0,0,0,0,0,0,0)
        )
        let bindRes = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(sockfd, sa, socklen_t(MemoryLayout<sockaddr_in>.stride))
            }
        }
        guard bindRes >= 0 else { throw NSError(domain: "HTTPServer", code: 500, userInfo: [NSLocalizedDescriptionKey: "Bind error"]) }
        listen(sockfd, 10)
        self.listener = FileHandle(fileDescriptor: sockfd, closeOnDealloc: true)
    }

    func start() {
        queue.async {
            while self.shouldRun {
                autoreleasepool {
                    let clientFd = accept(self.listener.fileDescriptor, nil, nil)
                    guard clientFd >= 0 else { return }
                    let client = FileHandle(fileDescriptor: clientFd, closeOnDealloc: true)
                    DispatchQueue.global().async {
                        self.handle(client: client)
                    }
                }
            }
        }
        print("HTTPServer running at http://\(Config.host):\(Config.port)")
        RunLoop.current.run()
    }

    func stop() {
        shouldRun = false
        self.listener.closeFile()
    }

    private func handle(client: FileHandle) {
        guard let req = try? readHTTPRequest(from: client) else {
            client.closeFile()
            return
        }
        do {
            switch (req.method, req.path) {
            case ("GET", "/cameras"):
                let list = CameraManager.shared.listCameras()
                let data = try JSONSerialization.data(withJSONObject: ["cameras": list], options: [])
                respond(client, code: 200, contentType: "application/json", body: data)
            case ("POST", "/camera/start"):
                try CameraManager.shared.startDefaultCamera()
                respondJSON(client, ["status": "ok", "active_camera_id": CameraManager.shared.activeCameraId ?? ""])
            case ("POST", "/camera/stop"):
                try CameraManager.shared.stopCamera()
                respondJSON(client, ["status": "ok"])
            case ("PUT", "/cameras/select"):
                guard let body = req.body else { throw httpErr(400, "No body") }
                guard let json = try? JSONSerialization.jsonObject(with: body, options: []) as? [String: Any],
                      let cameraId = json["camera_id"] as? String else {
                    throw httpErr(400, "Invalid JSON or missing camera_id")
                }
                try CameraManager.shared.selectCamera(by: cameraId)
                respondJSON(client, ["status": "ok", "active_camera_id": cameraId])
            case ("GET", "/camera/stream"):
                guard CameraManager.shared.isCameraActive() else {
                    throw httpErr(400, "Camera not running")
                }
                // Switch to OutputStream
                let output = OutputStream(toFileAtPath: "/dev/fd/\(client.fileDescriptor)", append: false)!
                output.open()
                let streamClient = StreamingClient(output: output)
                CameraManager.shared.addStreamingClient(streamClient)
                streamClient.start()
                // Keep stream open as long as client is alive
                var shouldRun = true
                let deadline = Date().addingTimeInterval(30 * 60) // Max 30min
                while shouldRun, Date() < deadline {
                    sleep(1)
                    if client.offsetInFile == 0 { // closed
                        shouldRun = false
                    }
                }
                streamClient.stop()
                CameraManager.shared.removeStreamingClient(streamClient)
            case ("GET", "/camera/capture"):
                let (img, meta) = try CameraManager.shared.captureFrame()
                let boundary = "FRAME_META_BOUNDARY"
                let header = """
                HTTP/1.1 200 OK\r
                Content-Type: multipart/mixed; boundary=\(boundary)\r
                \r
                --\(boundary)\r
                Content-Type: image/jpeg\r
                Content-Disposition: attachment; filename="capture.jpg"\r
                \r
                """
                client.write(Data(header.utf8))
                client.write(img)
                let metaPart = """

                --\(boundary)\r
                Content-Type: application/json\r
                Content-Disposition: attachment; filename="meta.json"\r
                \r
                \(String(data: try! JSONSerialization.data(withJSONObject: meta, options: []), encoding: .utf8)!)\r
                --\(boundary)--\r
                """
                client.write(Data(metaPart.utf8))
            case ("POST", "/camera/record"):
                guard CameraManager.shared.isCameraActive() else { throw httpErr(400, "Camera not running") }
                guard let body = req.body else { throw httpErr(400, "No body") }
                guard let json = try? JSONSerialization.jsonObject(with: body, options: []) as? [String: Any],
                      let duration = json["duration"] as? Int else {
                    throw httpErr(400, "Missing duration")
                }
                let tempDir = FileManager.default.temporaryDirectory
                let filename = "record_\(Int(Date().timeIntervalSince1970)).mp4"
                let fileURL = tempDir.appendingPathComponent(filename)
                let group = DispatchGroup()
                group.enter()
                var recordSuccess = false
                var recordError: String? = nil
                CameraManager.shared.recordVideo(duration: duration, to: fileURL) { ok, err in
                    recordSuccess = ok
                    recordError = err
                    group.leave()
                }
                group.wait()
                if !recordSuccess {
                    throw httpErr(500, recordError ?? "Recording failed")
                }
                let videoData = try Data(contentsOf: fileURL)
                let headers = """
                HTTP/1.1 200 OK\r
                Content-Type: video/mp4\r
                Content-Disposition: attachment; filename="record.mp4"\r
                Content-Length: \(videoData.count)\r
                \r
                """
                client.write(Data(headers.utf8))
                client.write(videoData)
                try? FileManager.default.removeItem(at: fileURL)
            default:
                respond(client, code: 404, contentType: "application/json", body: Data("{\"error\":\"Not found\"}".utf8))
            }
        } catch let err as HTTPError {
            respond(client, code: err.status, contentType: "application/json", body: Data("{\"error\":\"\(err.message)\"}".utf8))
        } catch {
            respond(client, code: 500, contentType: "application/json", body: Data("{\"error\":\"\(error)\"}".utf8))
        }
        client.closeFile()
    }
}

// MARK: - HTTP Helpers

struct HTTPRequest {
    var method: String
    var path: String
    var headers: [String: String]
    var body: Data?
}

func readHTTPRequest(from client: FileHandle) throws -> HTTPRequest {
    var reqStr = ""
    while true {
        let data = client.readData(ofLength: 1024)
        if data.isEmpty { break }
        reqStr += String(data: data, encoding: .utf8) ?? ""
        if reqStr.contains("\r\n\r\n") { break }
    }
    let lines = reqStr.components(separatedBy: "\r\n")
    guard !lines.isEmpty else { throw httpErr(400, "Empty request") }
    let firstLine = lines[0].split(separator: " ")
    guard firstLine.count >= 2 else { throw httpErr(400, "Malformed request") }
    let method = String(firstLine[0])
    let path = String(firstLine[1])
    var headers: [String: String] = [:]
    var i = 1
    while i < lines.count, lines[i] != "" {
        let parts = lines[i].split(separator: ":", maxSplits: 1)
        if parts.count == 2 {
            headers[String(parts[0]).trimmingCharacters(in: .whitespaces)] = String(parts[1]).trimmingCharacters(in: .whitespaces)
        }
        i += 1
    }
    var body: Data?
    if let cl = headers["Content-Length"], let clen = Int(cl) {
        let remain = clen - (reqStr.split(separator: "\r\n\r\n", maxSplits: 1).last?.count ?? 0)
        if remain > 0 {
            let d = client.readData(ofLength: remain)
            body = d
        }
    }
    return HTTPRequest(method: method, path: path, headers: headers, body: body)
}

struct HTTPError: Error {
    let status: Int
    let message: String
}
func httpErr(_ status: Int, _ msg: String) -> HTTPError {
    return HTTPError(status: status, message: msg)
}

func respond(_ client: FileHandle, code: Int, contentType: String, body: Data) {
    let header = """
    HTTP/1.1 \(code) \(httpStatusMsg(code))\r
    Content-Type: \(contentType)\r
    Content-Length: \(body.count)\r
    Connection: close\r
    \r
    """
    client.write(Data(header.utf8))
    client.write(body)
}

func respondJSON(_ client: FileHandle, _ json: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: json, options: [])
    respond(client, code: 200, contentType: "application/json", body: data)
}

func httpStatusMsg(_ code: Int) -> String {
    switch code {
    case 200: return "OK"
    case 400: return "Bad Request"
    case 404: return "Not Found"
    case 409: return "Conflict"
    case 500: return "Internal Server Error"
    default: return "Error"
    }
}

// MARK: - CGImage to CMSampleBuffer (for recording)
import AppKit
func cgImageToSampleBuffer(_ cgImage: CGImage, width: Int, height: Int, time: CMTime) -> CMSampleBuffer? {
    let pixelBufferAttributes: [CFString: Any] = [
        kCVPixelBufferCGImageCompatibilityKey: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey: true
    ]
    var pxBuffer: CVPixelBuffer?
    let status = CVPixelBufferCreate(
        kCFAllocatorDefault,
        width, height,
        kCVPixelFormatType_32BGRA,
        pixelBufferAttributes as CFDictionary,
        &pxBuffer
    )
    guard status == kCVReturnSuccess, let buffer = pxBuffer else { return nil }
    CVPixelBufferLockBaseAddress(buffer, [])
    let context = CGContext(
        data: CVPixelBufferGetBaseAddress(buffer),
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
    )
    context?.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))
    CVPixelBufferUnlockBaseAddress(buffer, [])

    var newSampleBuffer: CMSampleBuffer?
    var timingInfo = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: 30), presentationTimeStamp: time, decodeTimeStamp: .invalid)
    var videoInfo: CMVideoFormatDescription?
    CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: buffer, formatDescriptionOut: &videoInfo)
    if let videoInfo = videoInfo {
        CMSampleBufferCreateReadyWithImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: buffer,
            formatDescription: videoInfo,
            sampleTiming: &timingInfo,
            sampleBufferOut: &newSampleBuffer
        )
    }
    return newSampleBuffer
}

// MARK: - Main

// Start HTTP server
do {
    let server = try HTTPServer(host: Config.host, port: Config.port)
    server.start()
} catch {
    print("Failed to start server: \(error)")
    exit(1)
}