import Foundation
import Vapor
import AVFoundation

// MARK: - Configuration via Environment Variables

let SERVER_HOST = Environment.get("SERVER_HOST") ?? "0.0.0.0"
let SERVER_PORT = Int(Environment.get("SERVER_PORT") ?? "8080") ?? 8080
let DEFAULT_RES_WIDTH = Int(Environment.get("CAMERA_RES_WIDTH") ?? "640") ?? 640
let DEFAULT_RES_HEIGHT = Int(Environment.get("CAMERA_RES_HEIGHT") ?? "480") ?? 480
let DEFAULT_FPS = Int(Environment.get("CAMERA_FPS") ?? "20") ?? 20

// MARK: - Camera Session Management

final class CameraManager {
    static let shared = CameraManager()
    
    private var captureSession: AVCaptureSession?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var photoOutput: AVCapturePhotoOutput?
    private var videoConnection: AVCaptureConnection?
    private var cameraInput: AVCaptureDeviceInput?
    private var currentCamera: AVCaptureDevice?
    private var isStreaming = false
    private var lock = NSLock()
    
    private var availableCameras: [AVCaptureDevice] {
        let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: [.externalUnknown, .builtInWideAngleCamera], mediaType: .video, position: .unspecified)
        return discovery.devices
    }
    
    private var outputFormat: OSType {
        return kCVPixelFormatType_32BGRA
    }
    
    // For MJPEG streaming
    private var streamClients: [ObjectIdentifier: Response.Body.StreamWriter] = [:]
    
    private init() { }
    
    func listCameras() -> [[String: Any]] {
        var result: [[String: Any]] = []
        for cam in availableCameras {
            result.append([
                "camera_id": cam.uniqueID,
                "localized_name": cam.localizedName,
                "is_active": currentCamera?.uniqueID == cam.uniqueID
            ])
        }
        return result
    }
    
    func startCamera(cameraID: String? = nil, width: Int = DEFAULT_RES_WIDTH, height: Int = DEFAULT_RES_HEIGHT, fps: Int = DEFAULT_FPS) throws {
        lock.lock()
        defer { lock.unlock() }
        if captureSession?.isRunning == true {
            return // Already started
        }
        let camera: AVCaptureDevice
        if let camID = cameraID {
            guard let cam = availableCameras.first(where: { $0.uniqueID == camID }) else {
                throw Abort(.notFound, reason: "Camera with id \(camID) not found.")
            }
            camera = cam
        } else {
            guard let cam = availableCameras.first else {
                throw Abort(.notFound, reason: "No camera available.")
            }
            camera = cam
        }
        let session = AVCaptureSession()
        session.beginConfiguration()
        session.sessionPreset = .vga640x480
        if session.canSetSessionPreset(.high) && width > 640 && height > 480 {
            session.sessionPreset = .high
        }
        let input = try AVCaptureDeviceInput(device: camera)
        if session.canAddInput(input) {
            session.addInput(input)
        } else {
            throw Abort(.internalServerError, reason: "Could not add camera input.")
        }
        // Video output for streaming
        let videoOutput = AVCaptureVideoDataOutput()
        videoOutput.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: outputFormat
        ]
        videoOutput.alwaysDiscardsLateVideoFrames = true
        if session.canAddOutput(videoOutput) {
            session.addOutput(videoOutput)
        } else {
            throw Abort(.internalServerError, reason: "Could not add video output.")
        }
        // Photo output for capture
        let photoOutput = AVCapturePhotoOutput()
        if session.canAddOutput(photoOutput) {
            session.addOutput(photoOutput)
        }
        // Set frame rate
        try camera.lockForConfiguration()
        camera.activeVideoMinFrameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
        camera.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
        camera.unlockForConfiguration()
        session.commitConfiguration()
        self.captureSession = session
        self.cameraInput = input
        self.currentCamera = camera
        self.videoOutput = videoOutput
        self.photoOutput = photoOutput
        self.isStreaming = false
        session.startRunning()
    }
    
    func stopCamera() {
        lock.lock()
        defer { lock.unlock() }
        captureSession?.stopRunning()
        isStreaming = false
        captureSession = nil
        videoOutput = nil
        photoOutput = nil
        cameraInput = nil
        currentCamera = nil
        streamClients.removeAll()
    }
    
    func switchCamera(cameraID: String) throws {
        lock.lock()
        defer { lock.unlock() }
        stopCamera()
        try startCamera(cameraID: cameraID)
    }
    
    func captureFrame(format: String = "jpeg", width: Int = DEFAULT_RES_WIDTH, height: Int = DEFAULT_RES_HEIGHT, eventLoop: EventLoop) -> EventLoopFuture<(Data, [String: Any])> {
        guard let session = captureSession, session.isRunning else {
            return eventLoop.makeFailedFuture(Abort(.serviceUnavailable, reason: "Camera is not running."))
        }
        guard let photoOutput = self.photoOutput else {
            return eventLoop.makeFailedFuture(Abort(.internalServerError, reason: "Photo output unavailable."))
        }
        let promise = eventLoop.makePromise(of: (Data, [String: Any]).self)
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        photoOutput.capturePhoto(with: settings, delegate: PhotoCaptureDelegate { imageData, error in
            if let error = error {
                promise.fail(error)
                return
            }
            guard let data = imageData else {
                promise.fail(Abort(.internalServerError, reason: "Failed to capture image"))
                return
            }
            let meta: [String: Any] = [
                "format": "jpeg",
                "timestamp": Date().iso8601,
                "camera_id": self.currentCamera?.uniqueID ?? "unknown",
                "resolution": ["width": width, "height": height]
            ]
            promise.succeed((data, meta))
        })
        return promise.futureResult
    }
    
    func streamMJPEG(onFrame: @escaping (Data) -> Void) throws {
        guard let session = captureSession, session.isRunning else {
            throw Abort(.serviceUnavailable, reason: "Camera is not running.")
        }
        guard let videoOutput = self.videoOutput else {
            throw Abort(.internalServerError, reason: "Video output unavailable.")
        }
        if isStreaming { return }
        isStreaming = true
        videoOutput.setSampleBufferDelegate(MJPEGSampleBufferDelegate(onFrame: onFrame), queue: DispatchQueue.global(qos: .userInitiated))
    }
    
    func stopStreaming() {
        isStreaming = false
        videoOutput?.setSampleBufferDelegate(nil, queue: nil)
    }
    
    func recordVideo(duration: Int, format: String = "mp4", width: Int = DEFAULT_RES_WIDTH, height: Int = DEFAULT_RES_HEIGHT, fps: Int = DEFAULT_FPS, eventLoop: EventLoop) -> EventLoopFuture<(URL, Int)> {
        let promise = eventLoop.makePromise(of: (URL, Int).self)
        // Use a background temp file for video writing
        let tmpDir = URL(fileURLWithPath: NSTemporaryDirectory())
        let filename = "recording_\(UUID().uuidString).\(format)"
        let fileURL = tmpDir.appendingPathComponent(filename)
        
        guard let session = captureSession, session.isRunning else {
            promise.fail(Abort(.serviceUnavailable, reason: "Camera is not running."))
            return promise.futureResult
        }
        let writer: AVAssetWriter
        do {
            writer = try AVAssetWriter(outputURL: fileURL, fileType: format == "avi" ? .avi : .mp4)
        } catch {
            promise.fail(Abort(.internalServerError, reason: "Failed to create video file writer: \(error.localizedDescription)"))
            return promise.futureResult
        }
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        input.expectsMediaDataInRealTime = true
        if writer.canAdd(input) {
            writer.add(input)
        }
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: nil)
        writer.startWriting()
        writer.startSession(atSourceTime: .zero)
        
        let endTime = Date().addingTimeInterval(TimeInterval(duration))
        var frameCount = 0
        let frameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
        
        let queue = DispatchQueue(label: "video.recording.queue")
        videoOutput?.setSampleBufferDelegate(VideoRecordingSampleBufferDelegate(input: input, adaptor: adaptor, writer: writer, until: endTime, frameDuration: frameDuration, onFinish: { writtenFrames, error in
            if let error = error {
                promise.fail(Abort(.internalServerError, reason: "Recording error: \(error.localizedDescription)"))
                return
            }
            promise.succeed((fileURL, writtenFrames))
        }), queue: queue)
        
        return promise.futureResult
    }
}

// MARK: - MJPEG Sample Buffer Delegate

final class MJPEGSampleBufferDelegate: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let onFrame: (Data) -> Void
    
    init(onFrame: @escaping (Data) -> Void) {
        self.onFrame = onFrame
    }
    
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvPixelBuffer: imageBuffer)
        let context = CIContext()
        guard let jpegData = context.jpegRepresentation(of: ciImage, colorSpace: CGColorSpaceCreateDeviceRGB(), options: [:]) else { return }
        onFrame(jpegData)
    }
}

// MARK: - Photo Capture Delegate

final class PhotoCaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    let onCapture: (Data?, Error?) -> Void
    
    init(onCapture: @escaping (Data?, Error?) -> Void) {
        self.onCapture = onCapture
    }
    
    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        onCapture(photo.fileDataRepresentation(), error)
    }
}

// MARK: - Video Recording Sample Buffer Delegate

final class VideoRecordingSampleBufferDelegate: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let input: AVAssetWriterInput
    let adaptor: AVAssetWriterInputPixelBufferAdaptor
    let writer: AVAssetWriter
    let until: Date
    let frameDuration: CMTime
    let onFinish: (Int, Error?) -> Void
    var frameCount = 0
    var isRecording = true
    
    init(input: AVAssetWriterInput, adaptor: AVAssetWriterInputPixelBufferAdaptor, writer: AVAssetWriter, until: Date, frameDuration: CMTime, onFinish: @escaping (Int, Error?) -> Void) {
        self.input = input
        self.adaptor = adaptor
        self.writer = writer
        self.until = until
        self.frameDuration = frameDuration
        self.onFinish = onFinish
        super.init()
        DispatchQueue.global().asyncAfter(deadline: .now() + until.timeIntervalSinceNow) { [weak self] in
            self?.finish()
        }
    }
    
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard isRecording else { return }
        if Date() >= until {
            finish()
            return
        }
        guard input.isReadyForMoreMediaData else { return }
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let time = CMTimeMultiply(frameDuration, multiplier: Int32(frameCount))
        if !adaptor.append(imageBuffer, withPresentationTime: time) {
            finish(error: NSError(domain: "VideoRecording", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to append frame"]))
            return
        }
        frameCount += 1
    }
    
    func finish(error: Error? = nil) {
        guard isRecording else { return }
        isRecording = false
        input.markAsFinished()
        writer.finishWriting {
            self.onFinish(self.frameCount, error)
        }
    }
}

// MARK: - Date ISO8601 Extension

extension Date {
    var iso8601: String {
        let formatter = ISO8601DateFormatter()
        return formatter.string(from: self)
    }
}

// MARK: - Main HTTP Server

func routes(_ app: Application) throws {
    // List all cameras
    app.get("cameras") { req -> Response in
        let cams = CameraManager.shared.listCameras()
        let res = Response(status: .ok)
        try res.content.encode(cams)
        return res
    }
    
    // Start the default camera or specified camera
    app.post("camera", "start") { req -> HTTPStatus in
        let params = try? req.content.decode([String: String].self)
        let cameraID = params?["camera_id"]
        let width = Int(params?["width"] ?? "") ?? DEFAULT_RES_WIDTH
        let height = Int(params?["height"] ?? "") ?? DEFAULT_RES_HEIGHT
        let fps = Int(params?["fps"] ?? "") ?? DEFAULT_FPS
        try CameraManager.shared.startCamera(cameraID: cameraID, width: width, height: height, fps: fps)
        return .ok
    }
    
    // Stop camera
    app.post("camera", "stop") { req -> HTTPStatus in
        CameraManager.shared.stopCamera()
        return .ok
    }
    
    // Switch camera
    app.put("cameras", "select") { req -> HTTPStatus in
        struct SwitchReq: Content { let camera_id: String }
        let data = try req.content.decode(SwitchReq.self)
        try CameraManager.shared.switchCamera(cameraID: data.camera_id)
        return .ok
    }
    
    // Stream video as MJPEG
    app.get("camera", "stream") { req -> Response in
        let boundary = "Boundary-\(UUID().uuidString)"
        let res = Response(status: .ok)
        res.headers.replaceOrAdd(name: .contentType, value: "multipart/x-mixed-replace; boundary=\(boundary)")
        let stream = res.body.stream { writer in
            do {
                try CameraManager.shared.streamMJPEG { jpegData in
                    var part = ""
                    part += "--\(boundary)\r\n"
                    part += "Content-Type: image/jpeg\r\n"
                    part += "Content-Length: \(jpegData.count)\r\n\r\n"
                    if let partData = part.data(using: .utf8) {
                        try? writer.write(.buffer(ByteBuffer(data: partData))).wait()
                        try? writer.write(.buffer(ByteBuffer(data: jpegData))).wait()
                        try? writer.write(.buffer(ByteBuffer(string: "\r\n"))).wait()
                    }
                }
            } catch {
                _ = writer.write(.end)
            }
        }
        res.body = stream
        return res
    }
    
    // Capture a single frame
    app.get("camera", "capture") { req -> Response in
        let format = req.query["format"] ?? "jpeg"
        let width = Int(req.query["width"] ?? "") ?? DEFAULT_RES_WIDTH
        let height = Int(req.query["height"] ?? "") ?? DEFAULT_RES_HEIGHT
        let eventLoop = req.eventLoop
        let promise = eventLoop.makePromise(of: Response.self)
        _ = CameraManager.shared.captureFrame(format: format, width: width, height: height, eventLoop: eventLoop).map { (data, meta) in
            var headers = HTTPHeaders()
            headers.add(name: .contentType, value: "image/jpeg")
            let response = Response(status: .ok, headers: headers, body: .init(data: data))
            let jsonMeta = try? JSONSerialization.data(withJSONObject: meta, options: [])
            if let jsonMeta = jsonMeta {
                response.headers.replaceOrAdd(name: "X-Meta", value: String(data: jsonMeta, encoding: .utf8) ?? "")
            }
            promise.succeed(response)
        }.flatMapError { error in
            let resp = Response(status: .internalServerError)
            try? resp.content.encode(["error": "\(error)"])
            promise.succeed(resp)
            return eventLoop.makeSucceededFuture(())
        }
        return promise.futureResult
    }
    
    // Record video for duration (seconds)
    app.post("camera", "record") { req -> Response in
        struct RecordReq: Content { let duration: Int? }
        let data = try req.content.decode(RecordReq.self)
        let duration = data.duration ?? 5
        let format = req.query["format"] ?? "mp4"
        let width = Int(req.query["width"] ?? "") ?? DEFAULT_RES_WIDTH
        let height = Int(req.query["height"] ?? "") ?? DEFAULT_RES_HEIGHT
        let fps = Int(req.query["fps"] ?? "") ?? DEFAULT_FPS
        let eventLoop = req.eventLoop
        let promise = eventLoop.makePromise(of: Response.self)
        _ = CameraManager.shared.recordVideo(duration: duration, format: format, width: width, height: height, fps: fps, eventLoop: eventLoop).map { (url, frameCount) in
            defer { try? FileManager.default.removeItem(at: url) }
            guard let fileData = try? Data(contentsOf: url) else {
                let resp = Response(status: .internalServerError)
                try? resp.content.encode(["error": "Failed to read recorded video file"])
                promise.succeed(resp)
                return
            }
            var headers = HTTPHeaders()
            headers.add(name: .contentType, value: format == "avi" ? "video/x-msvideo" : "video/mp4")
            headers.add(name: .contentDisposition, value: "attachment; filename=\"recording.\(format)\"")
            let resp = Response(status: .ok, headers: headers, body: .init(data: fileData))
            promise.succeed(resp)
        }.flatMapError { error in
            let resp = Response(status: .internalServerError)
            try? resp.content.encode(["error": "\(error)"])
            promise.succeed(resp)
            return eventLoop.makeSucceededFuture(())
        }
        return promise.futureResult
    }
}

// MARK: - Vapor Application Boot

var env = try Environment.detect()
let app = Application(env)
defer { app.shutdown() }
app.http.server.configuration.hostname = SERVER_HOST
app.http.server.configuration.port = SERVER_PORT
try routes(app)
try app.run()