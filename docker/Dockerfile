FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/app/hf_cache \
    WXLIVE_OUTPUT_DIR=/app/data

WORKDIR /app

# 国内网络优先使用清华 apt 源（若不可达可删掉下面 sed 行），安装 ffmpeg 与 libsndfile1
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements-nas.txt /app/docker/requirements-nas.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/docker/requirements-nas.txt

COPY wxlive /app/wxlive
COPY lexicon /app/lexicon

RUN mkdir -p /app/data /app/hf_cache

EXPOSE 8910

CMD ["uvicorn", "wxlive.server:app", "--host", "0.0.0.0", "--port", "8910"]
