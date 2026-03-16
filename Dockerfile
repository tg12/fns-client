# Build a native Python image for the FNS client and bundled web UI.
FROM python:3.14-slim

# Keep Python runtime behavior predictable inside the container.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first to maximize Docker layer reuse.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source, static assets, and configuration.
COPY fns_client ./fns_client
COPY public ./public
COPY src/main/resources ./src/main/resources
COPY docker ./docker
COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md

# Install the package so the entrypoint runs exactly like the local environment.
RUN pip install --no-cache-dir . --upgrade

# Expose the browser UI and API port.
EXPOSE 8080

# Start the native Python service with config-file-only startup.
CMD ["python", "-m", "fns_client"]