# Docker 部署

此目录是 Docker Compose 部署入口，不应包含真实密钥、账号、上传视频或运行数据库。

完整步骤见项目根目录的 [DEPLOYMENT.md](../DEPLOYMENT.md)。最短启动流程：

```bash
cp .env.example .env
# 编辑 .env：填入随机会话/加密密钥和 Qwen Provider 配置
docker compose up -d --build
```

默认监听 `http://<服务器IP>:8080`，运行数据保存在 `./runtime-data`。公网生产环境请配置 HTTPS 并将 `GONGXU_COOKIE_SECURE=true`。
