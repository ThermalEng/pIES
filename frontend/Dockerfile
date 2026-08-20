# IES Plan 前端 (React + Vite 构建, nginx 托管)
# 构建上下文 = 仓库根(docker-compose 指定 context: ., dockerfile: frontend/Dockerfile)
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# manual/ 在仓库根(构建上下文), 帮助中心 manifest 生成器消费(FE-DOC-01)
COPY manual/ /manual/
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
