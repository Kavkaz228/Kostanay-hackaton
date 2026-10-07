FROM node:22.16.0-alpine@sha256:41e4389f3d988d2ed55392df4db1420ad048ae53324a8e2b7c6d19508288107e AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236
LABEL org.opencontainers.image.title="Allur twin 2.0 Web" org.opencontainers.image.version="2.0.0" org.opencontainers.image.description="Allur twin 2.0 browser interface"
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
RUN sed -i -E 's|^pid[[:space:]]+[^;]+;|pid /tmp/nginx.pid;|; /^user[[:space:]]/d' /etc/nginx/nginx.conf
USER nginx
EXPOSE 8080
