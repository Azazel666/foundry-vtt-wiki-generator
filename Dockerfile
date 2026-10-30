FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp TZ=Europe/Stockholm

ADD --checksum=sha256:cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 \
    https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-amd64 \
    /usr/local/bin/supercronic
RUN chmod 755 /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check .
RUN mkdir -p /state /foundry && chown 1000:1000 /state

FROM base AS test
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check '.[test]'
COPY tests ./tests
USER 1000:1000
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
COPY docker/entrypoint.sh /usr/local/bin/wikigen-entrypoint
RUN chmod 755 /usr/local/bin/wikigen-entrypoint
USER 1000:1000
VOLUME ["/state"]
HEALTHCHECK --interval=1m --timeout=5s --retries=3 CMD grep -qa supercronic /proc/1/cmdline
CMD ["wikigen-entrypoint"]
