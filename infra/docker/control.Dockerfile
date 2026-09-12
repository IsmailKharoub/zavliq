FROM node:24.21.0-bookworm-slim AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY services/control services/control
RUN pnpm install --filter @zavliq/control --frozen-lockfile
RUN pnpm --filter @zavliq/control build

FROM node:24.21.0-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build /app /app
COPY infra/scripts/start-control.sh /usr/local/bin/start-control
RUN mkdir -p /data && chown node:node /data
# The entrypoint only reads Docker-mounted secrets and drops privileges before serving.
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
EXPOSE 3000
ENTRYPOINT ["bash", "/usr/local/bin/start-control"]
