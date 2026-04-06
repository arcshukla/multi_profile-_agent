FROM python:3.11-slim

# System deps for pymupdf (PDF parsing) + C++ build tools for chroma-hnswlib
RUN echo ">>> Installing system dependencies..." && \
    apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    build-essential \
    g++ \
    cmake \
    && rm -rf /var/lib/apt/lists/* && \
    echo ">>> System dependencies installed."

WORKDIR /app

# Disable ChromaDB telemetry (avoids PostHog version-mismatch errors at runtime)
ENV ANONYMIZED_TELEMETRY=False

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN echo ">>> Installing Python dependencies (this may take a while)..." && \
    pip install --no-cache-dir -r requirements.txt && \
    echo ">>> Python dependencies installed."

# Copy application code
COPY . .

# Build Tailwind CSS from templates and copy static source assets
RUN echo ">>> Building Tailwind CSS..." && \
    python -m pytailwindcss \
      -i app/static_src/tailwind.css \
      -o static/tailwind.css \
      --minify && \
    cp app/static_src/favicon.ico static/favicon.ico && \
    cp app/static_src/favicon.png static/favicon.png && \
    cp app/static_src/favicon.svg static/favicon.svg && \
    echo ">>> Tailwind CSS built."

# Create required directories (will be volume-mounted in production)
RUN mkdir -p profiles system logs static && \
    echo ">>> Build complete. Starting application..."

# HuggingFace Spaces uses port 7860
EXPOSE 7860

# Run with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
