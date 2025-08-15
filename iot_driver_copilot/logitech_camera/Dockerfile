# Use a slim Python image as base
FROM python:3.11-slim

# Set environment variables for unbuffered output and working directory
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install OS-level dependencies required for OpenCV and video device access
# (libgl1 and v4l-utils for camera, libglib2.0-0 for opencv-python-headless image I/O)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy the driver code and dependency file first (for better Docker caching)
COPY requirements.txt driver.py /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the HTTP port (default 8080, can be overridden)
EXPOSE 8080

# Set default environment variables (can be overridden at runtime)
ENV SHIFU_HTTP_HOST=0.0.0.0
ENV SHIFU_HTTP_PORT=8080
ENV SHIFU_CAMERA_INDEX=0
ENV SHIFU_FRAME_WIDTH=640
ENV SHIFU_FRAME_HEIGHT=480
ENV SHIFU_FRAME_RATE=15
ENV SHIFU_IMAGE_FORMAT=jpeg

# Entrypoint to run the driver
CMD ["python", "driver.py"]

# NOTE: To access camera hardware, run the container with --device=/dev/video0 or the relevant device
# Example: docker run --device=/dev/video0 ...