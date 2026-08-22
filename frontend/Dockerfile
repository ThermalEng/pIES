# pIES 前端 (React + Vite 构建, nginx 托管)
# 构建上下文 = 仓库根(docker-compose 指定 context: ., dockerfile: frontend/Dockerfile)
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# manual/ 在仓库根(构建上下文), 帮助中心 manifest 生成器消费(FE-DOC-01)
COPY manual/ /manual/
# 产品版本只从后端 pyproject 元数据读取,避免前后端重复维护版本号。
COPY backend/pyproject.toml /app/backend-pyproject.toml
ENV PRODUCT_PYPROJECT=/app/backend-pyproject.toml
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
