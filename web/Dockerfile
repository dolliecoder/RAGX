FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S ragx && adduser -S ragx -G ragx
COPY --from=build --chown=ragx:ragx /app/.next/standalone ./
COPY --from=build --chown=ragx:ragx /app/.next/static ./.next/static
USER ragx
EXPOSE 3000
CMD ["node", "server.js"]
