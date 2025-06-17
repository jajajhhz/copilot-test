import Foundation
import Vapor
import AVFoundation

// MARK: - Environment Variables

let serverHost = Environment.get("SERVER_HOST") ?? "0.0.0.0"
let serverPort = Int(Environment.get("SERVER_PORT") ?? "8080") ?? 8080
let defaultCameraId = Int(Environment.get("CAMERA_ID") ?? "0") ?? 0
let defaultWidth = Int(Environment.get("CAMERA_WIDTH") ?? "640") ?? 640
let defaultHeight = Int(Environment.get("CAMERA_HEIGHT") ?? "480") ?? 480
let defaultFPS = Int(Environment.get("CAMERA_FPS") ?? "30") ?? 30

// MARK: - CameraManager

final class CameraManager {
    private var captureSession: AVCaptureSession?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var photoOutput: AVCapturePhotoOutput?
    private var videoDevice: AVCaptureDevice?
    private var videoConnection: AVCaptureConnection?
    private var currentCameraId: Int = defaultCameraId
    private var availableCameras: [AVCaptureDevice] = []
    private let cameraQueue = DispatchQueue(label: "camera.session.queue")
    private let lock = NSLock()
    private var isRecording: Bool = false
    private var recordingURL: URL?
    private var assetWriter: AVAssetWriter?
    private var assetWriterInput: AVAssetWriterInput?
    private var assetAdaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var recordingStartTime: CMTime?
    private var lastSampleTime: CMTime?
    private var recordingSemaphore = DispatchSemaphore(value: 1)
    
    init() {
        refreshCameras()
    }
    
    func refreshCameras() {
        availableCameras = AVCaptureDevice.devices(for: .video)
    }
    
    func listCameras() -> [[String: Any]] {
        refreshCameras()
        return availableCameras.enumerated().map { (idx, device) in
            [
                "camera_id": idx,
                "localized_name": device.localizedName,
                "unique_id": device.uniqueID,
                "is_active": idx == currentCameraId
            ]
        }
    }
    
    func selectCamera(id: Int) throws {
        lock.lock()
        defer { lock.unlock() }
        refreshCameras()
        guard id >= 0 && id < availableCameras.count else {
            throw Abort(.badRequest, reason: "Camera id \(id) is not available.")
        }
        if currentCameraId != id {
            stopCamera()
            currentCameraId = id
        }
    }
    
    func startCamera(width: Int = defaultWidth, height: Int = defaultHeight, fps: Int = defaultFPS) throws {
        lock.lock()
        defer { lock.unlock() }
        if captureSession?.isRunning == true { return }
        refreshCameras()
        guard availableCameras.count > currentCameraId else {
            throw Abort(.notFound, reason: "Camera not found.")
        }
        let session = AVCaptureSession()
        session.sessionPreset = .high
        
        let device = availableCameras[currentCameraId]
        guard let input = try? AVCaptureDeviceInput(device: device) else {
            throw Abort(.internalServerError, reason: "Cannot create device input.")
        }
        if session.canAddInput(input) {
            session.addInput(input)
        } else {
            throw Abort(.internalServerError, reason: "Cannot add device input to session.")
        }
        let videoDataOutput = AVCaptureVideoDataOutput()
        videoDataOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String:
                                         kCVPixelFormatType_32BGRA]
        videoDataOutput.alwaysDiscardsLateVideoFrames = true
        if session.canAddOutput(videoDataOutput) {
            session.addOutput(videoDataOutput)
        } else {
            throw Abort(.internalServerError, reason: "Cannot add video output to session.")
        }
        if let connection = videoDataOutput.connection(with: .video) {
            if connection.isVideoOrientationSupported {
                connection.videoOrientation = .portrait
            }
        }
        let photoOutput = AVCapturePhotoOutput()
        if session.canAddOutput(photoOutput) {
            session.addOutput(photoOutput)
        } else {
            throw Abort(.internalServerError, reason: "Cannot add photo output to session.")
        }
        // Set resolution and frame rate
        try device.lockForConfiguration()
        if device.activeFormat.videoSupportedFrameRateRanges.contains(where: { $0.maxFrameRate >= Double(fps) }) {
            device.activeVideoMinFrameDuration = CMTimeMake(value: 1, timescale: Int32(fps))
            device.activeVideoMaxFrameDuration = CMTimeMake(value: 1, timescale: Int32(fps))
        }
        if device.activeFormat.formatDescription.dimensions.width != width ||
            device.activeFormat.formatDescription.dimensions.height != height
        {
            // No direct way to set resolution, it depends on preset/format
            // Use sessionPreset .high and rely on output resizing
        }
        device.unlockForConfiguration()
        session.startRunning()
        captureSession = session
        videoOutput = videoDataOutput
        photoOutput = photoOutput
        videoDevice = device
        videoConnection = videoDataOutput.connection(with: .video)
    }
    
    func stopCamera() {
        lock.lock()
        defer { lock.unlock() }
        captureSession?.stopRunning()
        captureSession = nil
        videoOutput = nil
        photoOutput = nil
        videoDevice = nil
        videoConnection = nil
    }
    
    func captureFrame() throws -> (image: Data, meta: [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        guard let photoOutput = self.photoOutput,
              let session = self.captureSession,
              session.isRunning else {
            throw Abort(.badRequest, reason: "Camera is not started.")
        }
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        let captureSemaphore = DispatchSemaphore(value: 0)
        var imageData: Data?
        var meta: [String: Any] = [:]
        class Handler: NSObject, AVCapturePhotoCaptureDelegate {
            let completion: (Data?, [String: Any]) -> Void
            init(completion: @escaping (Data?, [String: Any]) -> Void) {
                self.completion = completion
            }
            func photoOutput(_ output: AVCapturePhotoOutput,
                             didFinishProcessingPhoto photo: AVCapturePhoto,
                             error: Error?) {
                var meta: [String: Any] = [:]
                if let cgImage = photo.cgImageRepresentation() {
                    meta["width"] = cgImage.width
                    meta["height"] = cgImage.height
                }
                meta["timestamp"] = Date().timeIntervalSince1970
                if let error = error {
                    print("Photo capture error: \(error)")
                }
                self.completion(photo.fileDataRepresentation(), meta)
            }
        }
        var handlerRef: Handler?
        handlerRef = Handler { data, m in
            imageData = data
            meta = m
            captureSemaphore.signal()
        }
        photoOutput.capturePhoto(with: settings, delegate: handlerRef!)
        _ = captureSemaphore.wait(timeout: .now() + 3)
        guard let data = imageData else {
            throw Abort(.internalServerError, reason: "Failed to capture frame.")
        }
        return (image: data, meta: meta)
    }
    
    func streamMJPEG(res: (Int, Int)? = nil, fps: Int = defaultFPS, onFrame: @escaping (Data) -> Void, onStop: @escaping () -> Void) throws {
        lock.lock()
        guard let output = videoOutput, let session = captureSession, session.isRunning else {
            lock.unlock()
            throw Abort(.badRequest, reason: "Camera is not started.")
        }
        let queue = cameraQueue
        let handler = MJPEGFrameHandler(onFrame: onFrame)
        output.setSampleBufferDelegate(handler, queue: queue)
        lock.unlock()
        // MJPEG will be sent through handler. When connection closes, call onStop.
        handler.onStop = { [weak self] in
            output.setSampleBufferDelegate(nil, queue: nil)
            onStop()
        }
    }
    
    func startRecording(duration: Int, completion: @escaping (Result<URL, Error>) -> Void) {
        cameraQueue.async {
            self.recordingSemaphore.wait()
            defer { self.recordingSemaphore.signal() }
            self.lock.lock()
            guard let output = self.videoOutput,
                  let session = self.captureSession,
                  session.isRunning else {
                self.lock.unlock()
                completion(.failure(Abort(.badRequest, reason: "Camera is not started.")))
                return
            }
            guard !self.isRecording else {
                self.lock.unlock()
                completion(.failure(Abort(.badRequest, reason: "Already recording.")))
                return
            }
            let tmpURL = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent(UUID().uuidString)
                .appendingPathExtension("mp4")
            do {
                let writer = try AVAssetWriter(outputURL: tmpURL, fileType: .mp4)
                let settings: [String: Any] = [
                    AVVideoCodecKey: AVVideoCodecType.h264,
                    AVVideoWidthKey: self.defaultWidth,
                    AVVideoHeightKey: self.defaultHeight
                ]
                let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
                input.expectsMediaDataInRealTime = true
                let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: nil)
                if writer.canAdd(input) {
                    writer.add(input)
                } else {
                    throw Abort(.internalServerError, reason: "Can't add input to asset writer.")
                }
                self.isRecording = true
                self.recordingURL = tmpURL
                self.assetWriter = writer
                self.assetWriterInput = input
                self.assetAdaptor = adaptor
                self.lastSampleTime = nil
                writer.startWriting()
                writer.startSession(atSourceTime: CMTime.zero)
                let group = DispatchGroup()
                group.enter()
                let handler = RecordingFrameHandler(adaptor: adaptor, input: input, startTime: {
                    self.recordingStartTime = $0
                }, lastSampleTime: { self.lastSampleTime = $0 })
                output.setSampleBufferDelegate(handler, queue: self.cameraQueue)
                DispatchQueue.global().asyncAfter(deadline: .now() + .seconds(duration)) {
                    output.setSampleBufferDelegate(nil, queue: nil)
                    self.lock.lock()
                    self.isRecording = false
                    self.assetWriterInput?.markAsFinished()
                    self.assetWriter?.finishWriting {
                        self.lock.unlock()
                        group.leave()
                    }
                }
                group.wait(timeout: .now() + .seconds(duration + 5))
                completion(.success(tmpURL))
            } catch {
                self.isRecording = false
                self.assetWriter = nil
                self.assetWriterInput = nil
                self.assetAdaptor = nil
                self.lock.unlock()
                completion(.failure(error))
            }
            self.lock.unlock()
        }
    }
}

// MARK: - MJPEGFrameHandler

class MJPEGFrameHandler: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let onFrame: (Data) -> Void
    var onStop: (() -> Void)?
    private var isRunning = true
    init(onFrame: @escaping (Data) -> Void) {
        self.onFrame = onFrame
    }
    func stop() {
        isRunning = false
        onStop?()
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard isRunning,
              let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvPixelBuffer: imageBuffer)
        let context = CIContext()
        guard let cgImage = context.createCGImage(ciImage, from: ciImage.extent) else { return }
        let nsImage = NSImage(cgImage: cgImage, size: .zero)
        let rep = NSBitmapImageRep(cgImage: cgImage)
        guard let jpegData = rep.representation(using: .jpeg, properties: [:]) else { return }
        let header = """
        --frame\r
        Content-Type: image/jpeg\r
        Content-Length: \(jpegData.count)\r
        \r
        """
        var frameData = Data(header.utf8)
        frameData.append(jpegData)
        frameData.append(Data("\r\n".utf8))
        onFrame(frameData)
        Thread.sleep(forTimeInterval: 1.0 / Double(defaultFPS))
    }
}

// MARK: - RecordingFrameHandler

class RecordingFrameHandler: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let adaptor: AVAssetWriterInputPixelBufferAdaptor
    let input: AVAssetWriterInput
    let startTime: (CMTime) -> Void
    let lastSampleTime: (CMTime) -> Void
    private var firstFrame = true
    init(adaptor: AVAssetWriterInputPixelBufferAdaptor,
         input: AVAssetWriterInput,
         startTime: @escaping (CMTime) -> Void,
         lastSampleTime: @escaping (CMTime) -> Void) {
        self.adaptor = adaptor
        self.input = input
        self.startTime = startTime
        self.lastSampleTime = lastSampleTime
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard input.isReadyForMoreMediaData,
              let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let presentationTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        lastSampleTime(presentationTime)
        if firstFrame {
            startTime(presentationTime)
            firstFrame = false
        }
        adaptor.append(imageBuffer, withPresentationTime: presentationTime)
    }
}

// MARK: - Vapor Application

func routes(_ app: Application, _ cameraManager: CameraManager) throws {
    app.get("cameras") { req -> Response in
        let list = cameraManager.listCameras()
        let res = ["cameras": list]
        return try Response(status: .ok, body: .init(data: JSONEncoder().encode(res)))
    }
    app.put("cameras", "select") { req -> Response in
        struct Select: Content { var camera_id: Int }
        let select = try req.content.decode(Select.self)
        try cameraManager.selectCamera(id: select.camera_id)
        return Response(status: .ok, body: .init(string: "{\"msg\":\"Camera switched to \(select.camera_id)\"}"))
    }
    app.post("camera", "start") { req -> Response in
        let width = Int(req.query["width"] ?? "") ?? defaultWidth
        let height = Int(req.query["height"] ?? "") ?? defaultHeight
        let fps = Int(req.query["fps"] ?? "") ?? defaultFPS
        try cameraManager.startCamera(width: width, height: height, fps: fps)
        return Response(status: .ok, body: .init(string: "{\"msg\":\"Camera started\"}"))
    }
    app.post("camera", "stop") { req -> Response in
        cameraManager.stopCamera()
        return Response(status: .ok, body: .init(string: "{\"msg\":\"Camera stopped\"}"))
    }
    app.get("camera", "stream") { req -> Response in
        let res = req.query["res"]?.split(separator: "x")
        let width = res?.count == 2 ? Int(res?[0] ?? "") ?? defaultWidth : defaultWidth
        let height = res?.count == 2 ? Int(res?[1] ?? "") ?? defaultHeight : defaultHeight
        let fps = Int(req.query["fps"] ?? "") ?? defaultFPS
        let stream = Response(status: .ok)
        stream.headers.replaceOrAdd(name: .contentType, value: "multipart/x-mixed-replace; boundary=frame")
        let body = Response.Body { writer in
            do {
                try cameraManager.streamMJPEG(res: (width, height), fps: fps, onFrame: { frame in
                    try? writer.write(.buffer(ByteBuffer(data: frame)))
                }, onStop: {
                    try? writer.close()
                })
            } catch {
                try? writer.close()
            }
        }
        stream.body = body
        return stream
    }
    app.get("camera", "capture") { req -> Response in
        do {
            let (imageData, meta) = try cameraManager.captureFrame()
            let boundary = "boundary-\(UUID().uuidString)"
            let resp = Response(status: .ok)
            resp.headers.replaceOrAdd(name: .contentType, value: "multipart/form-data; boundary=\(boundary)")
            var body = Data()
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"image\"; filename=\"frame.jpg\"\r\n".data(using: .utf8)!)
            body.append("Content-Type: image/jpeg\r\n\r\n".data(using: .utf8)!)
            body.append(imageData)
            body.append("\r\n".data(using: .utf8)!)
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            let metaData = try JSONSerialization.data(withJSONObject: meta, options: [])
            body.append("Content-Disposition: form-data; name=\"metadata\"; filename=\"meta.json\"\r\n".data(using: .utf8)!)
            body.append("Content-Type: application/json\r\n\r\n".data(using: .utf8)!)
            body.append(metaData)
            body.append("\r\n".data(using: .utf8)!)
            body.append("--\(boundary)--\r\n".data(using: .utf8)!)
            resp.body = .init(data: body)
            return resp
        } catch {
            return Response(status: .internalServerError, body: .init(string: "{\"error\": \"\(error)\"}"))
        }
    }
    app.post("camera", "record") { req -> Response in
        struct Req: Content { var duration: Int }
        let recordReq = try req.content.decode(Req.self)
        var fileURL: URL?
        let sema = DispatchSemaphore(value: 0)
        var err: Error?
        cameraManager.startRecording(duration: recordReq.duration) { result in
            switch result {
            case .success(let url): fileURL = url
            case .failure(let e): err = e
            }
            sema.signal()
        }
        _ = sema.wait(timeout: .now() + .seconds(recordReq.duration + 10))
        if let err = err {
            return Response(status: .internalServerError, body: .init(string: "{\"error\": \"\(err)\"}"))
        }
        guard let url = fileURL, let fileData = try? Data(contentsOf: url) else {
            return Response(status: .internalServerError, body: .init(string: "{\"error\": \"Recording failed\"}"))
        }
        let resp = Response(status: .ok)
        resp.headers.replaceOrAdd(name: .contentType, value: "video/mp4")
        resp.headers.replaceOrAdd(name: .contentDisposition, value: "attachment; filename=\"recording.mp4\"")
        resp.body = .init(data: fileData)
        try? FileManager.default.removeItem(at: url)
        return resp
    }
}

// MARK: - Main

let app = Application(.production)
defer { app.shutdown() }

let cameraManager = CameraManager()

try routes(app, cameraManager)

app.http.server.configuration.hostname = serverHost
app.http.server.configuration.port = serverPort

try app.run()