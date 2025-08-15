# Use an official Python slim base image for smaller size
FROM python:3.11-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install OS dependencies required for opencv and USB camera access
# libgl1: needed for OpenCV
# v4l-utils: needed for video4linux support (camera access)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy dependency file and driver code first (to leverage Docker cache for dependencies)
COPY requirements.txt /app/
COPY driver.py /app/

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Expose the default port for Flask app
EXPOSE 8080

# Set environment variables for default configuration (can be overridden at runtime)
ENV SERVER_HOST=0.0.0.0
ENV SERVER_PORT=8080
ENV CAMERA_ID=0
ENV CAMERA_RESOLUTION=640x480
ENV CAMERA_FRAME_RATE=30

# For camera access, you need to pass the /dev/video* device(s) at runtime:
# Example: docker run --device=/dev/video0:/dev/video0 ...

# Run the driver
CMD ["python", "driver.py"]