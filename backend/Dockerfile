# pIES 后端镜像 (API + Worker 共用)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖 (psycopg 二进制编译需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY iesplan ./iesplan
RUN pip install --upgrade pip && pip install .[dev]

COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "iesplan.main:app", "--host", "0.0.0.0", "--port", "8000"]
