# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**一个功能完整的 OpenAI 兼容 API 服务，为 Flow 提供统一的接口**

</div>

## 🏠 本地增强版入口

当前工作树在保留上游主要内容、许可证和 attribution 的基础上，增加了本地安全账号池、Windows 一键启动/自启、影策兼容 API 与对应安全门禁。

- [中文使用说明](docs/USER_GUIDE_ZH.md)：Windows 安装、账号添加、测试页、API Key、影策接入、备份恢复和常见故障。
- [本地增强版与上游差异](docs/FORK_DIFFERENCES_ZH.md)：哪些是本地补丁、明确边界，以及后续同步 upstream 的建议流程。

> 本次交付以 Windows 本地优先；Zeabur/生产部署不在本次范围。不要把 API Key、账号凭据、完整上游媒体地址或原始提示词写进 URL、文档或日志。

### Windows 新电脑最快用法

1. 安装 **Python 3.11 或更高版本（64 位）** 和最新版 Google Chrome；安装 Python 时勾选 `Add Python to PATH`。
2. 从本仓库下载 ZIP 并完整解压，或执行：

   ```powershell
   git clone https://github.com/zkhyww/flow2api-secure-account-pool.git
   cd flow2api-secure-account-pool
   ```

3. 双击根目录的 `start-flow2api.cmd`。第一次会自动创建 `venv`、安装 `requirements.txt` 并启动服务；依赖安装所需时间取决于网络。以后再次双击只会复用环境和已有服务。
4. 浏览器打开 `http://127.0.0.1:8000/manage`，首次使用上游默认账号 `admin` / `admin` 登录并立即改密码，然后在这台电脑上逐个添加 Google 账号。

数据库会在首次启动时自动创建，不需要手工安装或从别的电脑复制。Git 仓库故意不包含 `data/`、数据库、Google Cookie、账号 Profile、API Key 和本机配置；这些数据涉及凭据且部分受 Windows 用户保护，**每台电脑都必须分别登录一次**。详见[中文使用说明](docs/USER_GUIDE_ZH.md)。

## ❤️赞助商

<div align="center">

[![FastAIToken](static/sponsors/fastaitoken-banner.png)](https://www.fastaitoken.com/register)

</div>

**FastAIToken** 是面向开发者的 AI API 聚合平台，支持 OpenAI、Claude、Gemini 等主流大模型，兼容 OpenAI API 协议，可无缝接入 **Claude Code、Codex、Gemini CLI、Cherry Studio、Cline、Continue** 等各类 AI 开发工具。平台采用 **充值 1:1（1 元 = 1 美元 API 额度）**，帮助开发者以更低成本、更高效率地使用全球领先的大模型服务。

平台提供多个可选分组与公开状态页，开发者可根据成本、响应速度和稳定性自由选择不同渠道，并享受 **7×24 小时真人技术支持**（非机器人）。

**主要做 AI 开发接入？可以试试 [FastAIToken](https://www.fastaitoken.com/register)，兼容 Codex / Claude Code / Gemini CLI 等主流工具。**

---

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://www.fastaitoken.com/register">
    <img src="static/sponsors/fastaitoken-logo.png" alt="FastAIToken" width="150">
  </a>
</td>
<td valign="top">
  感谢 <strong>FastAIToken</strong> 赞助了本项目！FastAIToken 是面向开发者的 AI API 聚合平台，兼容 OpenAI API 协议，支持 Claude Code、Codex、Gemini CLI、Cherry Studio、Cline、Continue 等主流 AI 开发工具。<br><br>
  当前提供 <strong>0.02x OpenAI 福利分组（限时）</strong>、<strong>0.25x OpenAI 普通分组</strong>、<strong>0.35x OpenAI 备用分组</strong>、<strong>0.45x OpenAI Pro 分组</strong>、<strong>0.7x Claude 普通分组</strong>、<strong>1.2x Claude Max 渠道</strong>；支持 <strong>1 元 = 1 美元 API 额度</strong> 的充值比例，并提供公开状态页、企业开票、<strong>99% SLA 企业级稳定号池</strong> 与 <strong>7×24 小时真人技术支持</strong>。<br><br>
  欢迎通过<a href="https://www.fastaitoken.com/register">此链接</a>注册体验。
</td>
</tr>
</table>

## ✨ 核心特性

- 🎨 **文生图** / **图生图**
- 🎬 **文生视频** / **图生视频**
- 🎞️ **首尾帧视频**
- 🔄 **AT/ST自动刷新** - AT 过期自动刷新，ST 过期时自动通过浏览器更新（personal 模式）
- 📊 **余额显示** - 实时查询和显示 VideoFX Credits
- 🚀 **负载均衡** - 多 Token 轮询和并发控制
- 🌐 **代理支持** - 支持 HTTP/SOCKS5 代理
- 📱 **Web 管理界面** - 直观的 Token 和配置管理
- 🎨 **图片生成连续对话**
- 🧩 **Gemini 官方请求体兼容** - 支持 `generateContent` / `streamGenerateContent`、`systemInstruction`、`contents.parts.text/inlineData/fileData`
- ✅ **Gemini 官方格式已实测出图** - 已使用真实 Token 验证 `/models/{model}:generateContent` 可正常返回官方 `candidates[].content.parts[].inlineData`

## 🚀 快速开始

### 前置要求

- Docker 和 Docker Compose（推荐）
- 或 Python 3.8+

- 由于Flow增加了额外的验证码，你可以自行选择使用浏览器打码或第三发打码：
注册[YesCaptcha](https://yescaptcha.com/i/13Xd8K)并获取api key，将其填入系统配置页面```YesCaptcha API密钥```区域
- YesCaptcha 支持在管理页切换 `type`：`RecaptchaV3TaskProxyless`、`RecaptchaV3TaskProxylessM1`、`RecaptchaV3TaskProxylessM1S7`、`RecaptchaV3TaskProxylessM1S9`；当前默认推荐 `M1S9`，S7/S9 会强制提交 `minScore` 0.7/0.9。
- 默认 `docker-compose.yml` 建议搭配第三方打码（yescaptcha/capmonster/ezcaptcha/capsolver）。
如需 Docker 内有头打码（browser/personal），请使用下方 `docker-compose.headed.yml`。

- 自动更新st浏览器拓展：[Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)

### 方式一：Docker 部署（推荐）

#### 标准模式（不使用代理）

```bash
# 克隆项目
git clone https://github.com/zkhyww/flow2api-secure-account-pool.git
cd flow2api-secure-account-pool

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

> 说明：Compose 已默认挂载 `./tmp:/app/tmp`。如果把缓存超时设为 `0`，语义是“不自动过期删除”；若希望容器重建后仍保留缓存文件，也需要保留这个 `tmp` 挂载。

#### WARP 模式（使用代理）

```bash
# 使用 WARP 代理启动
docker-compose -f docker-compose.warp.yml up -d

# 查看日志
docker-compose -f docker-compose.warp.yml logs -f
```

#### Docker 有头打码模式（browser / personal）

> 适用于你有虚拟化桌面需求、希望在容器里启用有头浏览器打码的场景。  
> 该模式默认启动 `Xvfb + Fluxbox` 实现容器内部可视化，并设置 `ALLOW_DOCKER_HEADED_CAPTCHA=true`。  
> 仅开放应用端口，不提供任何远程桌面连接端口。
> `personal` 内置浏览器现在默认按有头模式启动；如需临时切回无头，可额外设置环境变量 `PERSONAL_BROWSER_HEADLESS=true`。

```bash
# 启动有头模式（首次建议带 --build）
docker compose -f docker-compose.headed.yml up -d --build

# 查看日志
docker compose -f docker-compose.headed.yml logs -f
```

- API 端口：`8000`
- 进入管理后台后，将验证码方式设为 `browser` 或 `personal`

### 方式二：Windows 本地部署（本增强版推荐）

```bash
# 克隆项目
git clone https://github.com/zkhyww/flow2api-secure-account-pool.git
cd flow2api-secure-account-pool

# Windows：双击 start-flow2api.cmd，或在命令行运行
start-flow2api.cmd
```

首次启动会自动创建虚拟环境、安装运行依赖并创建本机数据库。也可以手动执行：

```bash

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

# 安装运行依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

> 运行 Flow2API 服务只需要安装 `requirements.txt`。开发、CI 或交付前执行全量 pytest 时，再额外安装测试依赖：`pip install -r requirements-test.txt`。测试依赖不属于生产运行依赖。

### 首次访问

服务启动后,访问管理后台: **http://localhost:8000**,首次登录后请立即修改密码!

- **用户名**: `admin`
- **密码**: `admin`

## 📈 监控接口

- `GET /health`：公开健康检查，返回服务是否存活、活跃 Token 数、即将过期 Token 数、已过期 Token 数、429 禁用数等摘要
- `GET /metrics`：Prometheus 指标接口
- `GET /api/tokens`：管理接口，返回 `at_expires`、`at_expired`、`at_expiring_within_1h`、`ban_reason`、`consecutive_error_count` 等 Token 状态

Prometheus 可直接抓 `/metrics`。如果部署到 Kubernetes，建议只在集群内抓取，并在 Ingress/Gateway 层单独限制 `/metrics` 的外部访问。

### 模型测试页面

访问 **http://localhost:8000/test** 可打开内置的模型测试页面，支持：

- 在“图片 / 视频”之间切换，并分别选择公开 capability、比例与目录开放的时长；文生、首帧、首尾帧、References 作为独立视频能力展示
- 由服务端 capability metadata 将参数组合解析为现有兼容模型 ID；页面同时显示生成方式、图片用途/张数与 `原生清晰度（外部客户端请选择 720P）`
- 输入提示词一键测试，流式显示生成进度；需要图片的视频能力在达到目录规定的精确张数前会禁用提交
- 成功视频旁提供“续写此视频”动作；源标识只保存在页面内存，不进入普通模型菜单或可见文本

## 📋 支持的模型

### 图片能力（公开目录）

公开目录只列能力，不再枚举 30 个比例/清晰度组合。兼容前缀是本服务请求 ID 的组成部分，不是 Flow 私有上游键。

| Capability ID | 页面名称 | 比例 | 清晰度 |
|---|---|---|---|
| `nano-banana-2` | Nano Banana 2 | 16:9、9:16、1:1、4:3、3:4 | 1K、2K、4K |
| `nano-banana-pro` | Gemini 3 Pro Image / Nano Banana Pro | 16:9、9:16、1:1、4:3、3:4 | 1K、2K、4K |

Codex 提供的真实 Flow 白名单矩阵：

| 能力 / 兼容前缀 | 1K | 2K | 4K |
|---|---|---|---|
| Nano Banana 2 / `gemini-3.1-flash-image` | 5 个比例均 completed、has_media=true | 5 个比例均 completed、has_media=true | 5 个比例均 failed、has_media=false；按 `membership_required` 展示 |
| Nano Banana Pro / `gemini-3.0-pro-image` | 5 个比例均 completed、has_media=true | 5 个比例均 completed、has_media=true | 5 个比例均 failed、has_media=false；按 `membership_required` 展示 |

默认清晰度为 1K。正式页面把 1K/2K 标为可用，把 4K 标为“需要高级会员”且不会默认选择；未来 metadata 中的 `hidden` 组合不会进入可选参数。组合提交时仍解析为现有兼容 model ID。

### 视频能力（公开目录）

`GET /v1/models`、`GET /api/test/models` 与内置测试页共用同一份服务端目录。视频按“生成方式”拆成 14 个明确 capability；横竖画幅仍作为参数，不另复制一套客户端模型表。Omni 文生支持 8/10 秒，Omni References 只开放 8 秒；Veo 3.1 本轮公开能力均为 8 秒。

| Capability ID | 生成方式 | 图片要求 | 时长 | 16:9 内部映射 | 9:16 内部映射 |
|---|---|---:|---|---|---|
| `omni-flash` | 文生视频 | 0 张 | 8/10 秒 | `omni` / `omni_10s` | `omni_portrait` / `omni_portrait_10s` |
| `omni-flash-references` | 参考图生视频 | 1–3 张 | 8 秒 | `omni` | `omni_portrait` |
| `omni-1.1-flash` | 文生视频 | 0 张 | 8/10 秒 | `omni_1_1` / `omni_1_1_10s` | `omni_1_1_portrait` / `omni_1_1_portrait_10s` |
| `omni-1.1-flash-references` | 参考图生视频 | 1–3 张 | 8 秒 | `omni_1_1` | `omni_1_1_portrait` |
| `veo-3.1-lite` | 文生视频 | 0 张 | 8 秒 | `veo_3_1_t2v_lite_landscape_8s` | `veo_3_1_t2v_lite_portrait_8s` |
| `veo-3.1-lite-first-frame` | 首帧生视频 | 恰好 1 张 | 8 秒 | `veo_3_1_i2v_lite_landscape_8s` | `veo_3_1_i2v_lite_portrait_8s` |
| `veo-3.1-lite-first-last` | 首尾帧生视频 | 恰好 2 张 | 8 秒 | `veo_3_1_interpolation_lite_landscape_8s` | `veo_3_1_interpolation_lite_portrait_8s` |
| `veo-3.1-fast` | 文生视频 | 0 张 | 8 秒 | `veo_3_1_t2v_fast_landscape_8s` | `veo_3_1_t2v_fast_portrait_8s` |
| `veo-3.1-fast-first-frame` | 首帧生视频 | 恰好 1 张 | 8 秒 | `veo_3_1_i2v_s_fast_landscape_8s_fl` | `veo_3_1_i2v_s_fast_portrait_8s_fl` |
| `veo-3.1-fast-first-last` | 首尾帧生视频 | 恰好 2 张 | 8 秒 | `veo_3_1_i2v_s_fast_landscape_8s_fl` | `veo_3_1_i2v_s_fast_portrait_8s_fl` |
| `veo-3.1-fast-references` | 参考图生视频 | 1–3 张 | 8 秒 | `veo_3_1_r2v_fast_landscape` | `veo_3_1_r2v_fast_portrait` |
| `veo-3.1-quality` | 文生视频 | 0 张 | 8 秒 | `veo_3_1_t2v_landscape_8s` | `veo_3_1_t2v_portrait_8s` |
| `veo-3.1-quality-first-frame` | 首帧生视频 | 恰好 1 张 | 8 秒 | `veo_3_1_i2v_s_landscape_8s` | `veo_3_1_i2v_s_portrait_8s` |
| `veo-3.1-quality-first-last` | 首尾帧生视频 | 恰好 2 张 | 8 秒 | `veo_3_1_i2v_s_landscape_8s` | `veo_3_1_i2v_s_portrait_8s` |

本轮没有增加新的底层模型表；以上公开 capability 全部映射到现有 `MODEL_CONFIG` 可调用 ID。Omni 10 秒 References **不开放**，Veo 3.1 Quality 的 Ingredients/References **不开放**；调用方不能靠图片张数让普通文生 capability 自动猜成另一种生成方式。Extend 仍是成功视频后的动作，不是普通可选 capability。

Google 官方能力表已列出 Gemini Omni Flash 1.1；本增强版按“不改变旧 Omni 行为、只增加新入口”的原则新增上述两个 capability。官方还列出 4/6 秒、10 秒 References、Frames 和 Video-to-Video 等能力，但本项目按既定菜单偏好和真实验证边界暂不暴露，避免把官方网页可选项误当成当前兼容 API 已验收能力。官方参考：[Google Flow models & supported features](https://support.google.com/flow/answer/16352836)。

影策/巨天的视频清晰度选择 `720P` 时，兼容层将其解释为**上游原生输出**，不会触发放大；空值、`native`、`720p`/`720P` 等价。`1080P`、`4K`、`2160P`、`480P` 未在本轮完成该兼容链真实验证，因此 `/v1/videos` 明确返回 `unsupported_video_parameters`，不会静默降档，也不会改选某个 upsample 模型。

> **长视频边界**
>
> - 正式页面的单次时长上限为 10 秒；只有 `omni-flash` 文生视频可选 8/10 秒，其他本轮公开视频 capability 固定为 8 秒。
> - Omni References 的 10 秒组合不进入正式目录；4 秒、6 秒也不进入本轮正式菜单。
> - 超过单次上限的工作流仍依赖成功视频旁的“续写此视频”动作；没有成功源视频时页面不会提供可提交的 Extend。

> **旧调用兼容**
>
> `MODEL_CONFIG` 中已有的 legacy/default、4s、6s、派生清晰度及重复顺序别名没有被本轮删除；其他现有 resolver 也未重写。但影策/巨天使用的 `/v1/videos` 边界现在只接受目录明确允许的原生清晰度语义，不能再通过 `resolution_name` 触发未验证的 1080P/4K/2160P/480P 放大路径。

## 🎬 影策（Framefield Studio）兼容接入

影策里新建“自定义渠道”时，按下面填写即可：

- **Base URL**：`http://127.0.0.1:8000`，末尾不要再加 `/v1`。
- **API Key**：从 Flow2API 管理页进入 **系统配置 → API 密钥配置** 获取；影策兼容端点复用现有这套 Bearer API Key，不需要再配第二套密钥。
- **图片协议**：选择 `openai-image`。
- **视频协议**：选择 `newapi`。

图片请求使用 `POST /v1/images/generations`；参考图编辑使用 `POST /v1/images/edits`。当前图片生成只支持 `n=1`，`size` / `quality` 会交给现有模型 resolver；如果上传 `mask`，服务会直接返回 `400 mask_not_supported`，不会向上游提交。图片结果可以是本地/data URI 转出的 `b64_json`；如果 provider 返回的是官方 allowlist 内、HTTPS、标准端口且无 userinfo 的媒体 URL，也可以直接返回 OpenAI Images `url`，不做服务端二次下载。

视频请求使用 `POST /v1/videos` 创建任务，再用 `GET /v1/videos/{id}` 查询。完成后通过 `GET /v1/videos/{id}/content` 读取本地缓存二进制，不会把完整上游媒体 URL 存进任务 registry 或作为下载地址返回给影策。公开状态只会是 `queued`、`in_progress`、`completed`、`failed`、`cancelled`。同一个 `Idempotency-Key` 配同一个请求会复用原任务；同键改请求会返回 409。当前兼容视频 task registry 是进程内状态，服务重启后旧 task ID 不保证还能查询。

显式 HTTP 媒体代理会使用固定公网 IP 的安全 CONNECT 隧道，TLS 仍按原始官方域名验证，并在 redirect 每一跳重新做 allowlist/DNS 检查；没有代理时继续使用 RESOLVE 固定解析。未实现的代理类型会 fail-closed，不会退回到代理端自行解析官方域名的不安全模式。

视频参数严格受同一公开目录约束：调用方应直接选择上表的明确 capability；图片张数必须落在该 capability 的 `min_images`/`max_images` 范围内，否则在创建 task 前返回 `unsupported_video_parameters`。影策/巨天提交 `resolution_name=720P`（或 `720p` / `native` / 空值）时保持目录解析出的底层模型不变，按上游原生输出处理；`1080P`、`4K`、`2160P`、`480P` 明确拒绝。`preset` 仍作为兼容字段接受，但不会覆盖调用方明确选择的 capability。

## 📡 API 使用示例（需要使用流式）

> 除了下方 `OpenAI-compatible` 示例，服务也支持 Gemini 官方格式：
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Gemini 官方格式支持以下认证方式：
> - `Authorization: Bearer <your_api_key>`
> - `x-goog-api-key: <your_api_key>`
> - 本地文档不建议把 API Key 放进 URL；示例统一使用请求头。
>
> Gemini 官方图片请求体已兼容：
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gemini 官方 generateContent（文生图）

> 已使用真实 Token 实测通过。
> 如需流式返回，可将路径替换为 `:streamGenerateContent?alt=sse`。

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "一颗放在木桌上的红苹果，棚拍光线，极简背景"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### 文生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只可爱的猫咪在花园里玩耍"
      }
    ],
    "stream": true
  }'
```

### 图生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "将这张图片变成水彩画风格"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### 文生视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶"
      }
    ],
    "stream": true
  }'
```

### 首尾帧生成视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "从第一张图过渡到第二张图"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<首帧base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<尾帧base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### 多图生成视频

> `R2V` 会由服务端自动组装新版视频请求体，调用方仍然使用 OpenAI 兼容输入即可。
> 服务端会将横屏 `R2V` 自动映射到最新的 `*_landscape` 上游模型键。
> 当前最多传 **3 张参考图**。

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <your_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "以三张参考图的人物和场景为基础，生成一段镜头平滑推进的竖屏视频"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图1base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图2base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图3base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- [PearNoDec](https://github.com/PearNoDec) 提供的YesCaptcha打码方案
- [raomaiping](https://github.com/raomaiping) 提供的无头打码方案
感谢所有贡献者和使用者的支持！

---

## 📞 联系方式

- 提交 Issue：[GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ 如果这个项目对你有帮助，请给个 Star！**

## 最近更新

- `9f1d712` 同步 personal 打码逻辑，包含清理、浏览器参数和打码方式配置。
- `da2ad06` 合并 PR #133。
- `abd0c00` 修复 PR #133 合并后的集成问题。
- `55431c9` 将 origin/main 同步到 PR #133。
- `4b7a0ad` 新增 Prometheus 服务指标和 Token 健康监控。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://www.star-history.com/#TheSmallHanCat/flow2api&type=date&legend=top-left)

