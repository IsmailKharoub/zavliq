FROM node:24.21.0-bookworm-slim AS build
ARG VITE_ECHO_USER_ID=""
ENV VITE_ECHO_USER_ID=$VITE_ECHO_USER_ID
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web apps/web
COPY packages/skill packages/skill
COPY docs/protocol.md docs/protocol.md
RUN pnpm install --filter @zavliq/web --frozen-lockfile
RUN pnpm --filter @zavliq/web build

FROM caddy:2.11.4-alpine
COPY infra/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/apps/web/dist /srv
EXPOSE 80 443
