# trunk-tap: SDRTrunk dashboard + whisper.cpp transcription, all in one image.
#
# Build:  docker build -t trunk-tap .
# Run:    docker compose up -d --build   (see docker-compose.yml)

# ---- whisper.cpp build stage --------------------------------------------------
FROM python:3.12-slim AS whisper-build
ARG WHISPER_CPP_TAG=v1.8.7
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${WHISPER_CPP_TAG} \
        https://github.com/ggml-org/whisper.cpp /opt/whisper.cpp
WORKDIR /opt/whisper.cpp
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build -j --config Release --target whisper-cli

# ---- runtime ------------------------------------------------------------------
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=whisper-build /opt/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# /data  = all mutable state (SQLite DB, audio, whisper model, exports) -- mount it
# /sdrtrunk = SDRTrunk's home dir (event_logs, playlist, tuner config) -- optional,
#             mount read-only if SDRTrunk runs on the same host
ENV SDRTD_DATA_DIR=/data \
    SDRTRUNK_HOME=/sdrtrunk \
    SDRTD_EXPORT_DIR=/data/export \
    WHISPER_CPP_BIN=/usr/local/bin/whisper-cli

EXPOSE 5544
VOLUME /data
ENTRYPOINT ["/app/docker-entrypoint.sh"]
