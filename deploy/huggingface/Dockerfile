# Hugging Face Space for the Pinpoint demo. Builds one exact commit of the
# GitHub repository, so the live product is always a version anyone can
# inspect. To deploy a newer version, change PINPOINT_COMMIT and push this
# file to the Space. Set BASETEN_API_KEY as a secret in the Space settings.
FROM python:3.11-slim

# 501077f: "Demo app: compare, audit live, try to fool the verifier"
ARG PINPOINT_COMMIT=501077ff3afc0e2a517b4f74ecf5f22c5ea9ac90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

# Fetch the commit's source archive and keep only what the app needs.
RUN python -c "import io, sys, tarfile, urllib.request; \
url = 'https://github.com/ManasMehta1110/pinpoint/archive/' + sys.argv[1] + '.tar.gz'; \
data = urllib.request.urlopen(url, timeout=60).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/tmp/src', filter='data')" "$PINPOINT_COMMIT" \
 && src="$(echo /tmp/src/*)" \
 && cp -r "$src/pyproject.toml" "$src/README.md" "$src/LICENSE" "$src/pinpoint" "$src/app" . \
 && rm -rf /tmp/src \
 && pip install ".[app]"

USER user
ENV HOST=0.0.0.0 \
    PORT=7860
EXPOSE 7860
CMD ["python", "-m", "app.server"]
