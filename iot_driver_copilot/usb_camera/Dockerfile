# Use a minimal Go base image for building
FROM golang:1.21-alpine AS builder

# Install build dependencies (required for go modules and cgo)
RUN apk add --no-cache build-base linux-headers v4l-utils

# Set working directory
WORKDIR /app

# Copy driver code and dependency file
COPY driver.go ./
COPY go.mod ./

# Download and tidy Go dependencies
RUN go mod tidy

# Build the Go binary
RUN go build -o camera-driver driver.go

# Use a minimal runtime image
FROM alpine:latest

# Install runtime dependencies (v4l-utils for /dev/video* camera access)
RUN apk add --no-cache v4l-utils

# Set working directory
WORKDIR /app

# Copy binary from builder
COPY --from=builder /app/camera-driver ./

# Expose the default HTTP port
EXPOSE 8080

# Set environment variables with defaults (can be overridden at runtime)
ENV DEVICE_PATH=/dev/video0 \
    CAMERA_FORMAT=MJPEG \
    CAMERA_WIDTH=640 \
    CAMERA_HEIGHT=480 \
    CAMERA_FPS=15 \
    SERVER_HOST= \
    SERVER_PORT=8080

# Set the entrypoint to run the driver
ENTRYPOINT ["./camera-driver"]

# --- HARDWARE DEVICE ACCESS NOTE ---
# To access the USB camera, run the container with:
#   --device=/dev/video0
# Adjust the DEVICE_PATH env variable and --device flag if your camera uses a different device path.