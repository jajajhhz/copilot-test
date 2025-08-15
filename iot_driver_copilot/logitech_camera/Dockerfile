# Use a minimal Python image
FROM python:3.11-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Copy driver code and dependencies manifest first (for caching)
COPY driver.py /app/driver.py
COPY requirements.txt /app/requirements.txt

# Install system dependencies required for OpenCV video support
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libgtk2.0-0 \
        libv4l-0 \
        libsm6 \
        libxext6 \
        libxrender1 && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose HTTP port
EXPOSE 8080

# Set default environment variables (can be overridden at runtime)
ENV HTTP_HOST=0.0.0.0
ENV HTTP_PORT=8080
ENV CAMERA_INDEX=0
ENV FRAME_WIDTH=640
ENV FRAME_HEIGHT=480
ENV IMAGE_FORMAT=jpeg

# Command to run the driver
CMD ["python", "driver.py"]

# Note: To access the camera device, run the container with:
#   docker run --device=/dev/video0:/dev/video0 ...