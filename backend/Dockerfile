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

# Cache Python dependencies independently of application source changes.
# Still install the real package wheel with bundled catalogs and schemas.
COPY pyproject.toml LICENSE ./
RUN python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb')); open('/tmp/pies-deps.txt','w').write('\\n'.join(p['project']['dependencies']+p['project']['optional-dependencies']['dev']))" \
    && pip install --upgrade pip \
    && pip install -r /tmp/pies-deps.txt
COPY iesplan ./iesplan
# 统一部署配置默认值（可被挂载文件通过 IESPLAN_DEPLOY_CONFIG_FILE 覆盖）
COPY deploy ./deploy
RUN pip install --no-deps .

# 0.9-E: HiGHS 适配 executable 固定安装到绝对路径（shebang 指向镜像 python）
COPY solver-executables/ies-highs-solver /usr/local/bin/ies-highs-solver
RUN chmod 755 /usr/local/bin/ies-highs-solver

COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "iesplan.main:app", "--host", "0.0.0.0", "--port", "8000"]
