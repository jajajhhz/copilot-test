FROM python:3.11-slim

# System dependencies required for OpenCV video capture (V4L2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libv4l-0 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy application code and dependency file before installing deps
COPY requirements.txt /app/
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Default HTTP port (can be changed via env)
EXPOSE 8000

# Run the driver
CMD ["python", "driver.py"]
