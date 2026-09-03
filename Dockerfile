FROM python:3.11-slim

# rclone powers the "any other cloud" backend (Box, Dropbox, pCloud, WebDAV, S3…).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${arch}.zip" -o /tmp/rclone.zip \
 && unzip -q -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin \
 && chmod +x /usr/local/bin/rclone \
 && rm /tmp/rclone.zip \
 && apt-get purge -y unzip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 fotohu \
 && mkdir -p /data && chown -R fotohu:fotohu /data /app
USER fotohu

ENV FOTOHU_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "fotohu"]
