FROM rust:1.97.1-bookworm AS runtime-build
WORKDIR /build
COPY crates/zavliq-runtime/Cargo.toml crates/zavliq-runtime/Cargo.lock ./
COPY crates/zavliq-runtime/src src
RUN cargo build --release --locked

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 1000 zavliq && useradd --uid 1000 --gid 1000 --no-create-home zavliq
WORKDIR /app
COPY packages/client-python /app/packages/client-python
COPY services/echo /app/services/echo
RUN pip install --no-cache-dir /app/packages/client-python /app/services/echo
COPY --from=runtime-build /build/target/release/zavliq /usr/local/bin/zavliq
COPY infra/scripts/echo-health.py /usr/local/bin/echo-health
ENV ZAVLIQ_BINARY=/usr/local/bin/zavliq TOKIO_WORKER_THREADS=2
USER 1000:1000
ENTRYPOINT ["zavliq-echo"]
