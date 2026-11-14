FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# JS API Key 属于浏览器公开配置，不允许把模型或高德服务端密钥传入构建。
ARG VITE_AMAP_WEB_JS_KEY=""
ENV VITE_API_BASE_URL="" VITE_AMAP_WEB_JS_KEY=$VITE_AMAP_WEB_JS_KEY
RUN npm run build

FROM nginx:1.28-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
