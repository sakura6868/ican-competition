# 工序智传部署指南

这是一份可独立部署的源码工程。项目默认通过兼容 Qwen API 的视觉模型处理用户已明确授权的压缩关键帧；不随源码包附带 API Key、账号、数据库、真人视频、公开数据集或本地模型权重。

## 1. 推荐方式：Docker Compose

适合 Linux、Windows 11（Docker Desktop）和 macOS。需要安装 Docker Engine / Docker Desktop，并确保 Docker Compose v2 可用。

```bash
cd deploy
cp .env.example .env
```

编辑 `deploy/.env`，至少替换以下配置：

```dotenv
# 两个值均使用随机生成的 64 位十六进制字符串。
GONGXU_SESSION_SECRET=replace-with-a-random-32-byte-secret
GONGXU_MASTER_KEY_HEX=replace-with-64-hex-characters

# 填入你自己的、兼容 Qwen API 的服务配置。
GONGXU_LLM_BASE_URL=https://your-provider.example/v1
GONGXU_LLM_API_KEY=your-api-key
GONGXU_LLM_MODEL=qwen3.8-max
GONGXU_VISION_BACKEND=qwen
```

可用下列命令生成随机值：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

启动服务：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8080/api/v1/health
```

默认访问地址为 `http://<服务器IP>:8080`。首次部署后创建管理员账号：

```bash
docker compose exec api python -m gongxu.cli create-admin --username admin
```

该命令会在终端安全地要求输入密码；不要把密码写入命令历史或源码文件。

## 2. 持久化与端口

默认运行数据保存在 `deploy/runtime-data/`，其中包括 SQLite、加密资产和审计记录。该目录不会被写入 Git，升级代码前请备份它。

可在 `deploy/.env` 调整：

```dotenv
# 运行数据所在的宿主机目录，绝对路径或相对 deploy/ 目录的路径均可。
GONGXU_HOST_DATA_DIR=./runtime-data

# 对外端口；若服务器使用 80 端口，将其改为 80。
GONGXU_HTTP_PORT=8080
```

数据不应放在临时目录、同步盘或公开对象存储中。

## 3. 公网部署要求

公网生产环境必须使用 HTTPS 反向代理，并将 `GONGXU_COOKIE_SECURE=true`。示例中的 `8080` 直连仅适用于本机或受信任的内网演示；直连 HTTP 不应承担真实账号和视频数据。

反向代理将 HTTPS 流量转发至 `127.0.0.1:<GONGXU_HTTP_PORT>` 后，访问域名即可使用。请在安全组或防火墙中仅开放实际需要的端口，并限制服务器运维入口。

## 4. Windows 11 开发启动

未使用 Docker 时，需要 Python 3.12、Node.js 22、FFmpeg 和网络可用的 Qwen Provider：

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\Setup.ps1
# 编辑根目录 .env，配置 Qwen 后：
powershell -ExecutionPolicy Bypass -File .\windows\Start.ps1
```

停止：

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\Stop.ps1
```

## 5. 常见检查

```bash
docker compose logs -f api worker web
docker compose ps
docker compose exec api python -m gongxu.providers.probe --mode live
```

- 登录后提示模型不可用：检查 Base URL、API Key、模型 ID 和 `GONGXU_VISION_BACKEND=qwen`。
- 视频分析返回“无法判断”：这是证据不足或授权不足的安全结果；检查固定机位、光照、遮挡和外发授权。
- 迁移失败：先备份 `runtime-data/`，确认 API 和 worker 使用同一份 `deploy/.env`。

## 6. 隐私边界

系统仅在上传者明确授权后，将必要的压缩关键帧发送给外部视觉 Provider。完整视频、账号信息和总分不应外发。上传资产和衍生文件在本地以 AES-256-GCM 加密保存；请自行遵守所在单位的数据管理与告知要求。

## 7. 生成交付源码包

项目维护者可在根目录运行以下命令，生成不含密钥、视频、数据库、数据集、本机依赖和模型权重的源码交付文件夹：

```bash
./scripts/package-source.sh
```
