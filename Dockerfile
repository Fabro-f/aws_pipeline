FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY bionovaq_mcp_server.py ./
COPY session_manager.py ./

# Copy enhancement modules (required by bioq_explain.py)
COPY disambiguation.py ./
COPY validation.py ./
COPY role_customization.py ./
COPY quick_mode.py ./
COPY progressive_disclosure.py ./
COPY glossary.py ./
COPY metrics_logger.py ./

# Create necessary directories
RUN mkdir -p /app/sessions && chmod 777 /app/sessions && \
    mkdir -p /app/metrics && chmod 777 /app/metrics

# Volumes for persistent storage
VOLUME ["/app/sessions"]
VOLUME ["/app/metrics"]

# Run as root to avoid permission issues with volume mounts
CMD ["python", "bionovaq_mcp_server.py"]
