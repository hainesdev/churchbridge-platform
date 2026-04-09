FROM node:22-alpine AS deps

WORKDIR /app/client

COPY client/package.json client/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

ARG NEXT_PUBLIC_API_URL=https://churchbridge.dhaines.dev
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

WORKDIR /app/client

COPY --from=deps /app/client/node_modules ./node_modules
COPY client/package.json client/package-lock.json ./
COPY client ./

RUN npm run build

FROM node:22-alpine AS runner

ARG NEXT_PUBLIC_API_URL=https://churchbridge.dhaines.dev
ENV NODE_ENV=production \
    PORT=3000 \
    NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

WORKDIR /app/client

COPY --from=builder /app/client/.next ./.next
COPY --from=builder /app/client/public ./public
COPY --from=builder /app/client/package.json ./package.json
COPY --from=builder /app/client/package-lock.json ./package-lock.json
COPY --from=builder /app/client/node_modules ./node_modules
COPY --from=builder /app/client/next.config.ts ./next.config.ts

EXPOSE 3000

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
