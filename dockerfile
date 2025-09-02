# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install bash for your DEFAULT_SHELL=/bin/bash
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash \
 && rm -rf /var/lib/apt/lists/*

ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} appuser \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN mkdir -p /app/settings && chown -R appuser:appuser /app

USER appuser
ENV PYTHONUNBUFFERED=1 TERM=xterm-256color

# You can keep ENTRYPOINT or use CMD; both are fine for this app
CMD ["python", "app.py"]
