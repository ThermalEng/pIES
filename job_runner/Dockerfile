# pIES job_runner 容器代理服务镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb')); open('/tmp/jr-deps.txt','w').write('\n'.join(p['project']['dependencies']))" \
    && pip install --no-cache-dir -r /tmp/jr-deps.txt \
    && rm -f /tmp/jr-deps.txt

COPY pies_job_runner ./pies_job_runner
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8010

CMD ["uvicorn", "pies_job_runner.app:app", "--host", "0.0.0.0", "--port", "8010"]
