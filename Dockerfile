FROM python:3.11-slim

# Install system dependencies: ffmpeg is essential for precise video cutting
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Exposed ports for Render / Koyeb / Hugging Face
EXPOSE 8080 10000 7860

CMD ["python", "app.py"]
