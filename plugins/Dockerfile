# Official built-in stage container image (`pies-builtin-stages:1.2.0`).
# Build from repository root:
#   docker build -f plugins/Dockerfile -t pies-builtin-stages:1.2.0 .
# Contains ONLY pies-sdk, pure iesplan domain packages (no DB/ORM/FastAPI/Redis),
# ies-highs-solver, and the 4 official built-in stage plugins under /app/plugins.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Cache pure domain dependencies independently of source changes (no DB/Redis/FastAPI).
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
    "setuptools>=77" \
    "jsonschema>=4.0" \
    "numpy>=1.26" \
    "ijson>=3.3,<4" \
    "highspy==1.7.2"

COPY sdk /tmp/build/sdk
RUN pip install --no-cache-dir --no-build-isolation --no-deps /tmp/build/sdk

COPY backend/iesplan/__init__.py /tmp/build/iesplan_domain/iesplan/__init__.py
COPY backend/iesplan/core /tmp/build/iesplan_domain/iesplan/core
COPY backend/iesplan/devices /tmp/build/iesplan_domain/iesplan/devices
COPY backend/iesplan/modeling /tmp/build/iesplan_domain/iesplan/modeling
COPY backend/iesplan/assembly /tmp/build/iesplan_domain/iesplan/assembly
COPY backend/iesplan/finance /tmp/build/iesplan_domain/iesplan/finance
COPY backend/iesplan/computation /tmp/build/iesplan_domain/iesplan/computation
RUN rm -f /tmp/build/iesplan_domain/iesplan/core/security.py \
    && python -c 'from pathlib import Path; Path("/tmp/build/iesplan_domain/pyproject.toml").write_text("""[build-system]\nrequires = ["setuptools>=77"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "iesplan"\nversion = "1.2.0"\ndescription = "Pure domain libraries for pIES stage containers"\nrequires-python = ">=3.12"\ndependencies = [\n    "jsonschema>=4.0",\n    "numpy>=1.26",\n    "ijson>=3.3,<4",\n    "highspy==1.7.2",\n]\n\n[tool.setuptools.packages.find]\ninclude = ["iesplan*"]\n\n[tool.setuptools.package-data]\n"iesplan.devices" = ["catalog/**/*.yaml", "catalog/**/*.csv", "schema/*.json", "samples/**/*.yaml"]\n"iesplan.assembly" = ["schema/*.json", "samples/valid/*.yaml", "samples/invalid/*.yaml", "samples/data/*.csv"]\n"iesplan.computation" = ["schema/*.json", "samples/*/*/*/*.json"]\n""", encoding="utf-8")' \
    && pip install --no-cache-dir --no-build-isolation --no-deps /tmp/build/iesplan_domain

COPY backend/solver-executables/ies-highs-solver /usr/local/bin/ies-highs-solver
RUN chmod 755 /usr/local/bin/ies-highs-solver

COPY plugins /tmp/build/plugins
RUN pip install --no-cache-dir --no-build-isolation --no-deps /tmp/build/plugins \
    && mkdir -p /app/plugins \
    && cp -r /tmp/build/plugins/predictor_naive /app/plugins/predictor_naive \
    && cp -r /tmp/build/plugins/generator_highs_milp /app/plugins/generator_highs_milp \
    && cp -r /tmp/build/plugins/solver_highs /app/plugins/solver_highs \
    && cp -r /tmp/build/plugins/evaluator_finance /app/plugins/evaluator_finance \
    && rm -rf /tmp/build \
    && groupadd --gid 10001 pies \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin pies \
    && mkdir -p /work/in /work/out \
    && chmod -R a+rX /app/plugins \
    && chown -R pies:pies /app /work

USER pies

CMD ["python", "-m", "pies_sdk.runner"]
