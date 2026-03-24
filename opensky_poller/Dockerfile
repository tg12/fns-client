FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt --upgrade

# Remove build tools to reduce image size
RUN apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

COPY service.py ./service.py
COPY persistence.py ./persistence.py

EXPOSE 8081

CMD ["python", "-m", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8081"]
