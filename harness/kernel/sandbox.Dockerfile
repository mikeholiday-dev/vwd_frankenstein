# Image for DockerSandbox. Owner: A. Built on first use and tagged with this file's hash,
# so editing it rebuilds automatically. Python matches the host (3.12) and the test runner
# matches uv.lock, so a bundle behaves the same here as in the fake sandbox.
FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.23 pytest==9.1.1 \
 && useradd --uid 10001 --create-home sandbox \
 && mkdir /deps /work && chmod 1777 /deps /work
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER sandbox
WORKDIR /work
