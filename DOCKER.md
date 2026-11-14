# Docker 本地部署

## 修复与复验（2026-11-14，覆盖下方首次验证中的依赖告警状态）

- 更新前端锁文件与依赖：Axios 1.21.1、Vue 3.5.45、Vite 6.4.6；jsPDF升级4.2.1，vue-tsc升级3.3.14。完整npm审计及生产依赖审计均为0项已知告警；这不是整个镜像或系统无漏洞的承诺。
- 新版类型检查发现首页无用threadEnd引用，已清理。生产镜像重新npm ci、类型检查和构建通过；浏览器检查PDF真实下载（PDF文件头/内容/页面对象）、结果页操作、首页输入交互和酒店地图联动通过。
- 修正.dockerignore父目录重新包含的边界，显式排除node_modules、venv、data、dist和.git，防止宿主依赖覆盖镜像内安装结果。前端实际构建上下文约521KB，不再带入数百MB本地依赖。
- 用户明确授权后，在独立tripmind-verify容器/新卷中读取本地backend/.env作为运行时配置，只进行一次真实北京一日游。模型和地图依赖启动后backend ready=true；经过Nginx提交任务、Worker执行和SSE回放，30.7秒完成，1天4个景点、13个时间线条目、21条SSE事件，最终is_fallback=false，坐标范围检查通过。
- 本次骨架生成阶段触发完整JSON兼容生成分支；最终不是离线模板，但不能据此宣称骨架路径始终成功，或所有景点事实/票价已被独立核实。未追加真实生成请求。
- 上游MCP依赖的Pydantic前向引用警告仍存在，不通过屏蔽警告伪装修复；离线4会话握手与真实任务均成功。该告警暂不阻断部署，后续升级上游组件需重新验证。
- 复验后的容器和本轮新建检查卷清理，保留最新镜像；真实.env及原业务库/Redis未更改，无Git提交推送。自己的正式部署仍按下方步骤配置和prepare。

当前方案：Nginx 静态前端 + 单进程 FastAPI（内置 Worker/隔离执行器）+ Redis Stack。
SQLite、Redis 和地图工具缓存使用独立命名卷。首次启动使用空数据库，不挂载或覆盖电脑已有的 backend/data。

## 1. 准备配置

需要 Docker Desktop（Linux containers）或 Linux Docker Engine，以及 Docker Compose。
在项目根目录复制 `.env.docker.example` 为 `.env.docker`，然后编辑新文件：

```powershell
Copy-Item .env.docker.example .env.docker
notepad .env.docker
```

Linux 使用 `cp -n .env.docker.example .env.docker`。已有配置不要重复覆盖。

必填：LLM_API_KEY、LLM_BASE_URL、LLM_MODEL_ID、AMAP_API_KEY；前端地图还需要公开的 VITE_AMAP_WEB_JS_KEY。
截图解析需要配置可用视觉模型 LLM_VISION_MODEL。模型服务端密钥不能写入 VITE_*。
当前前端没有新增安全密钥代理；高德账户如要求额外安全配置，需按自己的 JS API 授权方式配置，不要把服务端私钥放入镜像。

默认仅监听 `127.0.0.1:8080`。暂不启用公共缓存 Redis，任务/断点 Redis 不做缓存淘汰。
`.env.docker` 被 Git 忽略，且不在 Docker 构建上下文中。不要公开 `docker compose config` 的完整输出，它可能包含注入后的密钥；仅检查配置用 `config --quiet`。

## 2. 构建与准备地图工具

以下命令均在项目根目录执行：

```bash
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker build
docker compose --env-file .env.docker --profile tools run --rm --no-deps mcp-prepare
```

地图准备必须成功后再启动。它在 Linux 容器中下载固定版本工具并验证握手；使用运行时配置，不把密钥写进镜像层。
工具缓存保存在 mcp-cache 卷，准备和后端使用相同 Python、UID 和缓存目录。不能复制 Windows venv 或用宿主机 prepare 代替容器内准备。
首次构建需要下载基础镜像、Python/npm 依赖；prepare 需要可访问 Python 包源。普通启动地图工具使用离线依赖缓存，实际地图数据仍联网查询。

## 3. 启动与创建管理员

```bash
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs --tail 100 backend
docker compose --env-file .env.docker exec backend python -m app.services.auth_service create-admin --username admin
```

管理员密码在终端交互输入，不写进配置、命令参数或镜像。随后打开 http://localhost:8080 登录。
前端深层页面由 Nginx 回退 index.html；/api 同源代理到后端，SSE 关闭代理缓冲。后端和 Redis 不映射宿主机端口。
frontend healthy 只代表静态服务正常；规划是否可用要看 backend 的 /health/ready 健康检查。首次预热可需数分钟。

## 4. 日常更新与停止

```bash
git pull --ff-only
docker compose --env-file .env.docker build
# requirements/mcp版本、Python镜像或缓存包源变化时，先停后端再重新prepare。
docker compose --env-file .env.docker up -d
```

只改前端 JS Key 也要重新构建 frontend；只改后端运行时配置，用 `up -d --force-recreate backend` 应用，restart 不会加载新的环境变量。
默认 Nginx 可重新解析后端地址，后端容器重建后无需改代理配置。

停止：`docker compose --env-file .env.docker down`。这会保留数据卷。
**不要加 `-v`**，否则会删除账号、行程、任务断点和地图依赖缓存。
开发机已有 Redis、数据库不受本项目命名卷影响。默认项目名 tripmind-local，可用 `-p` 为不同环境设置独立名称。

## 5. 数据与服务器迁移

- app-data：`/app/backend/data`，包含 SQLite 业务记录。
- redis-data：`/data`，Redis AOF；配置 noeviction，磁盘与内存仍需要运维监控。
- mcp-cache：`/opt/uv-cache`，固定版本地图工具依赖。

上服务器前先停止新任务、排空或明确取消旧任务，停写后备份 SQLite 和 Redis。不要运行 FLUSHDB。
已有 SQLite 的迁入需停写并使用 SQLite 备份接口形成一致备份；不要运行中只复制 .db 而遗漏 WAL。先保留旧库，不在本轮自动迁移。
SQLite 与 Redis 没有跨库原子快照保证；不能仅凭容器启动成功就宣称任务无损迁移。

服务器设置 TRIPMIND_BIND_IP=0.0.0.0，并将 CORS_ORIGINS 改为用户实际访问的完整来源（含协议、非默认端口）。只对外开放网站端口；不开放 Redis 和8000。
正式公网推荐域名/HTTPS及外层反向代理，HTTPS 后 AUTH_COOKIE_SECURE=true；本地 HTTP 保持 false。当前模板不是自动签证书或高可用方案。
后端仍使用单进程和 SQLite，不直接增加 --workers 或横向复制后端来宣称多实例安全。

## 6. 验证记录

下面保留已完成的验证结果；一次性 Docker 检查脚本和覆盖配置不随仓库发布，正式部署仅使用根目录 compose.yaml。
### 本轮本地验证（2026-11-14）

- 在 Docker Desktop Linux 环境真实构建前后端镜像，前端类型检查和生产构建通过；Compose 配置与 Nginx 语法检查通过。
- 使用独立 tripmind-docker-check 项目验证网页/深层路由、同源API、未登录拦截、登录Cookie、登录后行程读取，确认Redis Search/JSON模块存在。
- 临时账号写入检查卷后重启后端，确认SQLite记录仍在；后端使用UID 10001，数据/工具缓存卷可写。未挂载真实本机数据库或Redis。
- 占位高德Key下仅执行地图工具依赖准备与ListTools握手：在线准备、离线4会话启动通过，均发现16个工具。未执行真实地图查询或模型生成，因此不代表真实Key/额度/规划质量通过。
- 构建白名单未发送真实.env、个人docs、数据库、虚拟环境；后端镜像检查未发现这些路径。Git继续忽略真实配置和私人部署交接文档。
- npm安装报告现有依赖存在18条安全警告（含开发依赖，尚未区分运行时影响）；本轮未执行可能破坏兼容性的audit fix --force，公网发布前需另行评估升级。
- 地图工具自身仍输出Pydantic前向引用警告，但依赖准备和握手成功；不是接口查询成功的证据。

不使用本轮临时账号作为正式管理员。独立检查容器与检查数据卷在验证后清理，构建镜像保留；正式启动请使用自己的.env.docker重新prepare。

参考：[Compose依赖就绪](https://docs.docker.com/compose/how-tos/startup-order/)、[Nginx代理缓冲](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[Redis Stack容器配置](https://redis.io/docs/latest/operate/oss_and_stack/install/archive/install-stack/docker/)。
