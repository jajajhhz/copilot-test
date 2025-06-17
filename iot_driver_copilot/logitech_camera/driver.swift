// swift-tools-version:5.5
import Foundation
import Vapor
import AVFoundation

// MARK: - Environment Variables
let SERVER_HOST = ProcessInfo.processInfo.environment["SERVER_HOST"] ?? "0.0.0.0"
let SERVER_PORT = Int(ProcessInfo.processInfo.environment["SERVER_PORT"] ?? "8080") ?? 8080
let DEFAULT_CAMERA_ID = Int(ProcessInfo.processInfo.environment["CAMERA_ID"] ?? "0") ?? 0
let DEFAULT_WIDTH = Int(ProcessInfo.processInfo.environment["CAMERA_WIDTH"] ?? "640") ?? 640
let DEFAULT_HEIGHT = Int(ProcessInfo.processInfo.environment["CAMERA_HEIGHT"] ?? "480") ?? 480
let DEFAULT_FPS = Int(ProcessInfo.processInfo.environment["CAMERA_FPS"] ?? "15") ?? 15
let STREAM_BOUNDARY = "mjpegstream"

// MARK: - CameraManager
final class CameraManager {
    private var session: AVCaptureSession?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var currentCamera: AVCaptureDevice?
    private var cameraId: Int = DEFAULT_CAMERA_ID
    private let queue = DispatchQueue(label: "CameraManager.Queue")
    private var isSessionRunning = false
    private var frameBuffer: CGImage?
    private var frameLock = NSLock()
    private var availableCameras: [AVCaptureDevice] {
        AVCaptureDevice.devices(for: .video)
    }

    static let shared = CameraManager()

    private init() {}

    func listCameras() -> [[String: Any]] {
        let cams = availableCameras
        return cams.enumerated().map {
            [
                "camera_id": $0.offset,
                "unique_id": $0.element.uniqueID,
                "localized_name": $0.element.localizedName,
                "connected": $0.element.isConnected,
                "position": "\($0.element.position.rawValue)",
                "active": ($0.offset == cameraId)
            ]
        }
    }

    func selectCamera(id: Int) throws {
        guard id >= 0 && id < availableCameras.count else {
            throw Abort(.badRequest, reason: "Invalid camera_id")
        }
        stopCamera()
        cameraId = id
        try startCamera()
    }

    func startCamera() throws {
        queue.sync {
            if isSessionRunning { return }
            session = AVCaptureSession()
            session?.sessionPreset = .vga640x480
            let devices = availableCameras
            guard cameraId < devices.count else { return }
            currentCamera = devices[cameraId]
            guard let input = try? AVCaptureDeviceInput(device: currentCamera!) else { return }
            if session!.canAddInput(input) {
                session!.addInput(input)
            }
            videoOutput = AVCaptureVideoDataOutput()
            videoOutput?.alwaysDiscardsLateVideoFrames = true
            videoOutput?.setSampleBufferDelegate(self, queue: queue)
            if session!.canAddOutput(videoOutput!) {
                session!.addOutput(videoOutput!)
            }
            session!.startRunning()
            isSessionRunning = true
        }
    }

    func stopCamera() {
        queue.sync {
            session?.stopRunning()
            session = nil
            videoOutput = nil
            currentCamera = nil
            isSessionRunning = false
            frameBuffer = nil
        }
    }

    func captureFrame() throws -> (Data, [String: Any]) {
        guard isSessionRunning else {
            throw Abort(.badRequest, reason: "Camera not started")
        }
        var image: CGImage?
        frameLock.lock(); image = frameBuffer; frameLock.unlock()
        guard let cgimg = image else {
            throw Abort(.internalServerError, reason: "No frame available")
        }
        let bitmap = NSBitmapImageRep(cgImage: cgimg)
        guard let jpegData = bitmap.representation(using: .jpeg, properties: [:]) else {
            throw Abort(.internalServerError, reason: "Failed to encode image")
        }
        let meta: [String: Any] = [
            "width": cgimg.width,
            "height": cgimg.height,
            "camera_id": cameraId,
            "timestamp": Int(Date().timeIntervalSince1970)
        ]
        return (jpegData, meta)
    }

    func recordVideo(duration: Int, to url: URL, width: Int, height: Int, fps: Int, completion: @escaping (Bool, String?) -> Void) {
        queue.async {
            guard self.isSessionRunning else {
                completion(false, "Camera not started")
                return
            }
            let assetWriter: AVAssetWriter
            do {
                assetWriter = try AVAssetWriter(outputURL: url, fileType: .mp4)
            } catch {
                completion(false, "Failed to create writer: \(error)")
                return
            }
            let outputSettings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: width,
                AVVideoHeightKey: height
            ]
            let writerInput = AVAssetWriterInput(mediaType: .video, outputSettings: outputSettings)
            writerInput.expectsMediaDataInRealTime = true
            if assetWriter.canAdd(writerInput) {
                assetWriter.add(writerInput)
            }
            let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: writerInput,
                                                               sourcePixelBufferAttributes: [
                                                                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                                                kCVPixelBufferWidthKey as String: width,
                                                                kCVPixelBufferHeightKey as String: height
                                                               ])
            assetWriter.startWriting()
            assetWriter.startSession(atSourceTime: .zero)
            let startTime = Date()
            let endTime = startTime.addingTimeInterval(Double(duration))
            var appended = false
            let group = DispatchGroup()
            group.enter()
            writerInput.requestMediaDataWhenReady(on: self.queue) {
                while writerInput.isReadyForMoreMediaData {
                    let now = Date()
                    if now > endTime { break }
                    var cgimg: CGImage?
                    self.frameLock.lock(); cgimg = self.frameBuffer; self.frameLock.unlock()
                    if let cgimg = cgimg {
                        let pixelBuffer = cgImageToPixelBuffer(cgimg: cgimg, width: width, height: height)
                        let sampleTime = CMTime(value: CMTimeValue(now.timeIntervalSince(startTime) * Double(fps)), timescale: CMTimeScale(fps))
                        if let pb = pixelBuffer {
                            appended = adaptor.append(pb, withPresentationTime: sampleTime)
                        }
                    }
                    usleep(1000000 / UInt32(fps))
                }
                writerInput.markAsFinished()
                assetWriter.finishWriting {
                    group.leave()
                }
            }
            group.wait()
            completion(appended, appended ? nil : "Failed to record")
        }
    }
}

func cgImageToPixelBuffer(cgimg: CGImage, width: Int, height: Int) -> CVPixelBuffer? {
    var pixelBuffer: CVPixelBuffer?
    let attrs = [
        kCVPixelBufferCGImageCompatibilityKey: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey: true
    ] as CFDictionary
    let status = CVPixelBufferCreate(kCFAllocatorDefault, width, height,
                                     kCVPixelFormatType_32BGRA, attrs, &pixelBuffer)
    guard status == kCVReturnSuccess, let pxbuffer = pixelBuffer else { return nil }
    CVPixelBufferLockBaseAddress(pxbuffer, [])
    let context = CGContext(data: CVPixelBufferGetBaseAddress(pxbuffer),
                            width: width, height: height,
                            bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pxbuffer),
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue)
    context?.draw(cgimg, in: CGRect(x: 0, y: 0, width: width, height: height))
    CVPixelBufferUnlockBaseAddress(pxbuffer, [])
    return pxbuffer
}

// MARK: - AVCaptureVideoDataOutputSampleBufferDelegate
extension CameraManager: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        if let imgBuf = CMSampleBufferGetImageBuffer(sampleBuffer) {
            let ciImg = CIImage(cvPixelBuffer: imgBuf)
            let ctx = CIContext()
            if let cgimg = ctx.createCGImage(ciImg, from: ciImg.extent) {
                frameLock.lock()
                frameBuffer = cgimg
                frameLock.unlock()
            }
        }
    }
}

// MARK: - Vapor App
func routes(_ app: Application) throws {
    let camera = CameraManager.shared

    app.get("cameras") { req -> Response in
        let cams = camera.listCameras()
        let data = try JSONSerialization.data(withJSONObject: cams, options: [])
        var res = Response()
        res.headers.replaceOrAdd(name: .contentType, value: "application/json")
        res.body = .init(data: data)
        return res
    }

    app.put("cameras", "select") { req -> Response in
        guard let body = req.body.data,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let cid = json["camera_id"] as? Int else {
            throw Abort(.badRequest, reason: "camera_id is required in body")
        }
        try camera.selectCamera(id: cid)
        return Response(status: .ok)
    }

    app.post("camera", "start") { req -> Response in
        do {
            try camera.startCamera()
            return Response(status: .ok)
        } catch {
            throw Abort(.internalServerError, reason: "Failed to start camera: \(error)")
        }
    }

    app.post("camera", "stop") { req -> Response in
        camera.stopCamera()
        return Response(status: .ok)
    }

    app.get("camera", "stream") { req -> Response in
        guard camera.isSessionRunning else {
            throw Abort(.badRequest, reason: "Camera not started")
        }
        let res = Response(status: .ok)
        res.headers.replaceOrAdd(name: .contentType, value: "multipart/x-mixed-replace; boundary=\(STREAM_BOUNDARY)")
        let stream = res.body.stream { writer in
            let interval = 1.0 / Double(DEFAULT_FPS)
            let timer = DispatchSource.makeTimerSource(queue: .global())
            timer.schedule(deadline: .now(), repeating: interval)
            timer.setEventHandler {
                var cgimg: CGImage?
                camera.frameLock.lock(); cgimg = camera.frameBuffer; camera.frameLock.unlock()
                if let cgimg = cgimg {
                    let bitmap = NSBitmapImageRep(cgImage: cgimg)
                    if let jpegData = bitmap.representation(using: .jpeg, properties: [:]) {
                        var chunk = "\r\n--\(STREAM_BOUNDARY)\r\n".data(using: .utf8)!
                        chunk.append("Content-Type: image/jpeg\r\n".data(using: .utf8)!)
                        chunk.append("Content-Length: \(jpegData.count)\r\n\r\n".data(using: .utf8)!)
                        chunk.append(jpegData)
                        _ = try? writer.write(.buffer(.init(data: chunk)))
                    }
                }
            }
            timer.resume()
            writer.onClose.whenComplete { _ in timer.cancel() }
        }
        return res
    }

    app.get("camera", "capture") { req -> Response in
        do {
            let (jpegData, meta) = try camera.captureFrame()
            let boundary = "capframe"
            var body = Data()
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Type: application/json\r\n\r\n".data(using: .utf8)!)
            body.append(try JSONSerialization.data(withJSONObject: meta, options: []))
            body.append("\r\n--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Type: image/jpeg\r\n\r\n".data(using: .utf8)!)
            body.append(jpegData)
            body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
            var res = Response()
            res.headers.replaceOrAdd(name: .contentType, value: "multipart/mixed; boundary=\(boundary)")
            res.body = .init(data: body)
            return res
        } catch {
            throw Abort(.internalServerError, reason: "\(error)")
        }
    }

    app.post("camera", "record") { req -> Response in
        guard let body = req.body.data,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let duration = json["duration"] as? Int else {
            throw Abort(.badRequest, reason: "duration is required in body")
        }
        let width = json["width"] as? Int ?? DEFAULT_WIDTH
        let height = json["height"] as? Int ?? DEFAULT_HEIGHT
        let fps = json["fps"] as? Int ?? DEFAULT_FPS
        let tmpURL = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("record-\(UUID().uuidString).mp4")
        let sem = DispatchSemaphore(value: 0)
        var success = false
        var errMsg: String?
        CameraManager.shared.recordVideo(duration: duration, to: tmpURL, width: width, height: height, fps: fps) { ok, msg in
            success = ok
            errMsg = msg
            sem.signal()
        }
        sem.wait()
        guard success else {
            throw Abort(.internalServerError, reason: errMsg ?? "Recording failed")
        }
        let fileData = try Data(contentsOf: tmpURL)
        var res = Response()
        res.headers.replaceOrAdd(name: .contentType, value: "video/mp4")
        res.headers.replaceOrAdd(name: .contentLength, value: "\(fileData.count)")
        res.headers.replaceOrAdd(name: .contentDisposition, value: "attachment; filename=record.mp4")
        res.body = .init(data: fileData)
        try? FileManager.default.removeItem(at: tmpURL)
        return res
    }
}

// MARK: - App Boot
var env = try! Environment.detect()
let app = Application(env)
defer { app.shutdown() }

try routes(app)
app.http.server.configuration.hostname = SERVER_HOST
app.http.server.configuration.port = SERVER_PORT
try app.run()