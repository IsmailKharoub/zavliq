# The input binary comes only from the successful draft workflow for this commit.
FROM python:3.12-slim-bookworm
ARG ZAVLIQ_NATIVE_SHA256
ARG ZAVLIQ_SOURCE_REVISION
LABEL org.opencontainers.image.revision=$ZAVLIQ_SOURCE_REVISION
LABEL com.zavliq.native.sha256=$ZAVLIQ_NATIVE_SHA256
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 1000 zavliq && useradd --uid 1000 --gid 1000 --no-create-home zavliq
WORKDIR /app
COPY packages/client-python /app/packages/client-python
COPY services/echo /app/services/echo
RUN pip install --no-cache-dir /app/packages/client-python /app/services/echo
COPY .zavliq-runtime/zavliq /usr/local/bin/zavliq
RUN printf '%s  /usr/local/bin/zavliq\n' "$ZAVLIQ_NATIVE_SHA256" | sha256sum -c - \
    && chmod 755 /usr/local/bin/zavliq \
    && /usr/local/bin/zavliq methods > /dev/null
COPY infra/scripts/echo-health.py /usr/local/bin/echo-health
ENV ZAVLIQ_BINARY=/usr/local/bin/zavliq TOKIO_WORKER_THREADS=2
USER 1000:1000
ENTRYPOINT ["zavliq-echo"]
