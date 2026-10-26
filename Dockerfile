# syntax=docker/dockerfile:1
#
# Two images from one file (docs/docker.md):
#
#   docker build --target api -t knowledge-api .
#   docker build --target ui  -t knowledge-ui  .
#
# The API image holds the service, the embedding model and the fictional
# company's files for seeding. The UI image holds the UI and nothing of the
# service: it only talks to the API, so it has no copy of the code, the
# database, the index or the model to read.
#
# Neither image holds a secret. The signing key and the model key are given
# when a container starts, and the database and index live on a volume.

# Exact version and digest, so a rebuild gets the same base.
ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b


FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# Every container runs as this user, never as root.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app


FROM base AS api-build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    HF_HOME=/opt/models
# PyTorch from its CPU-only index first: the default Linux build brings
# several gigabytes of GPU libraries the service never uses. The same
# version as the demo's (ui/requirements.txt).
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
# The embedding model, downloaded once here, so a container never fetches
# anything at startup. The same model as APP_EMBEDDING_MODEL's default.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', device='cpu')"


FROM base AS ui-build
RUN python -m venv /opt/venv
COPY requirements-ui.txt /tmp/requirements-ui.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements-ui.txt


FROM base AS ui
COPY --from=ui-build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    UI_API_URL=http://api:8000
WORKDIR /app
# The UI's own modules only. ui/demo.py and ui/streamlit_app.py are the
# public demo page, which runs the service in its own process; they are not
# part of this UI and stay out of its image.
COPY ui/__init__.py ui/app.py ui/asking.py ui/client.py ui/documents.py \
     ui/managing.py ui/reviewing.py ui/session.py ui/text.py ui/
USER app
EXPOSE 8501
# Streamlit's own health route. Not the API's: if the API is down, a new UI
# wouldn't help, and the UI says so to whoever is using it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"]
CMD ["streamlit", "run", "ui/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]


FROM base AS api
COPY --from=api-build /opt/venv /opt/venv
COPY --from=api-build /opt/models /opt/models
# The model is loaded from the image and nothing is fetched. The database and
# the index are on the volume at /data, kept apart from the code.
ENV PATH=/opt/venv/bin:$PATH \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    APP_EMBEDDING_OFFLINE=true \
    APP_DATABASE_URL=sqlite:////data/app.db \
    APP_INDEX_DIRECTORY=/data/index
WORKDIR /app
# The code belongs to root, so the user the service runs as can't change it.
COPY app/ app/
COPY scripts/ scripts/
COPY data/acmecorp/ data/acmecorp/
RUN mkdir /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8000
# Healthy once /health answers 200, which is once the model and the index are
# loaded: the port isn't open before. The image has no curl, so the check is
# Python's own; urlopen fails on a 503, which /health gives when the
# database doesn't answer. A slow machine gets two minutes to start, checked
# every two seconds so a fast one is healthy as soon as it is ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# Inside a container it has to listen on every interface to be reached at
# all; which of the host's addresses it is published on is decided by
# docker run -p or Compose, and the docs publish it on 127.0.0.1 only.
CMD ["uvicorn", "app.main:serve", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log"]
