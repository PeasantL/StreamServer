FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install ffmpeg and other dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file first (for better caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py middleware.py utils.py database.py config.py ./
COPY templates/ ./templates/
COPY static/ ./static/ 

# Copy default config
COPY config.json ./

# Create thumbnail directory
RUN mkdir -p thumbnails

# Expose the port
EXPOSE 6969

# Run the application
CMD ["python", "main.py"]
