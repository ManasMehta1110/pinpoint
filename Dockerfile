# The demo app. Builds from this repository; runs on any host that sets PORT
# (Render) or expects 7860 (Hugging Face Spaces). Set BASETEN_API_KEY as a
# secret in the host's settings; without it the gallery and verifier still
# work and live audits are off.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Spaces run containers as uid 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

COPY pyproject.toml README.md LICENSE ./
COPY pinpoint ./pinpoint
RUN pip install ".[app]"
COPY app ./app

USER user
ENV HOST=0.0.0.0 \
    PORT=7860
EXPOSE 7860
CMD ["python", "-m", "app.server"]
