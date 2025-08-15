# Use lightweight Python base image
FROM python:3.11-slim

# Set environment variables for Python
ENV PYTHONUNBUFFERED=1

# Set build arguments for configuration; can be overridden at runtime
ARG SERVER_HOST=0.0.0.0
ARG SERVER_PORT=8080
ARG CAMERA_ID=0
ARG CAMERA_RESOLUTION=640x480
ARG CAMERA_FRAME_RATE=30

# Set environment variables (can be overridden by user at runtime)
ENV SERVER_HOST=${SERVER_HOST}
ENV SERVER_PORT=${SERVER_PORT}
ENV CAMERA_ID=${CAMERA_ID}
ENV CAMERA_RESOLUTION=${CAMERA_RESOLUTION}
ENV CAMERA_FRAME_RATE=${CAMERA_FRAME_RATE}

# Install system dependencies required for OpenCV and camera access
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Create working directory
WORKDIR /app

# Copy only dependency file and driver code first (for Docker cache)
COPY requirements.txt driver.py /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port used by the Flask app
EXPOSE 8080

# Set entrypoint to run the driver
CMD ["python", "driver.py"]

# Note: For camera access, run the container with:
#  docker run --rm --device=/dev/video0:/dev/video0 ... <image>
# You may need to adjust the device mapping based on your camera device.