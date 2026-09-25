# zcode2api

<div align="center">

**强大的 ZCode / Z.AI / 智谱 BigModel 多账号管理与全协议 AI API 反代网关**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-green.svg)](https://nodejs.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](Dockerfile)
[![Protocol](https://img.shields.io/badge/Protocol-OpenAI%20%7C%20Anthropic-orange.svg)](#api-反代与客户端接入指南)

将 ZCode (zcode.z.ai) Coding Plan 与智谱开放平台 (BigModel) 凭证无缝转化为标准的 **OpenAI** 与 **Anthropic** 兼容 API，支持多账号智能负载均衡、额度耗尽自动切号、套餐资格检测与自动抢领、实时用量监控与无浏览器无痕验证码自动续期。

[功能亮点](#-核心功能亮点) • [快速开始](#-快速开始) • [客户端配置指南](#-客户端配置指南) • [Docker 部署](#-docker-部署) • [模型支持](#-支持的模型列表) • [安全与防封建议](#-安全与防封建议)

</div>

---

## ✨ 核心功能亮点

* 🌐 **全协议反代网关（OpenAI + Anthropic 双兼容）**：
  * **OpenAI 兼容端点**：`/v1/chat/completions` 与 `/v1/models`，完美适配市面上 99% 的 AI 工具（Cherry Studio、NextChat、Cursor、One-API、New API、Chatbox、LobeChat 等）。
  * **Anthropic 兼容端点**：`/v1/messages`，原生支持 Claude 客户端、Cline、Roo Code 及 Claude Code。
* 🔀 **多账号智能轮询与无感自动切号 (`auto_switch`)**：
  * 多个账号通过 Round-Robin 算法自动负载均衡。
  * 遇到账号额度耗尽（HTTP 402/提示 balance exhaust）、上游限流（429）或凭证失效时，网关在**同一次请求中自动无感故障转移**到下一个可用账号，调用绝不中断。
* 🎁 **套餐资格刷新与自动领取 (`auto_claim`)**：
  * 深度参考 Z-Accounts 核心原理，提供 **「刷新资格」** 接口，一键并发探测账号池中所有账号在官方当前可领取的体验套餐包（如 Start Plan 3M/5M 配额）。
  * 开启 **「自动领取」** 后，后台监控或新账号入池时会自动求解人机验证并抢领免费方案，彻底告别手动续期。
* 📊 **实时用量监控与精确原因诊断**：
  * 可视化图表展示每个账号各模型的总额度、已消耗、剩余额度与重置周期。
  * **彻底告别含糊不清的“未获取”**：账号无额度时会精确诊断并展示原因（如 *未开通方案（新号/未领取）*、*Token 失效 401/403*、*额度耗尽 0 剩余*、*官方限流 429* 等），并直接附带快捷领取/检测入口。
* 🖼️ **多模态视觉 (Vision) 完整兼容**：
  * 原生支持 `GLM-5.3` / `GLM-5.3-Flash` 视觉识图，客户端上传或粘贴的截图（Base64 `image_url`）会自动转译为上游规范，直接识图写代码、排查报错截图。
* 🛡️ **轻量级纯内存无痕验证求解器**：
  * 基于 Node.js + jsdom 补齐浏览器环境，**无需安装臃肿的 Chromium 或无头浏览器**，极速求解阿里云无痕人机验证（`X-Aliyun-Captcha-Verify-Param`）。
* 🖥️ **清新现代的管理后台**：
  * 提供卡片化 Web 后台，支持 OAuth 一键授权登录导入、批量凭证粘贴、JSON 导入导出、一键开关自动切号与自动领取。
* ⚡ **极速开箱与防端口冲突**：
  * Windows 内置 `start.bat` / `start.ps1` 与 `stop.bat` / `stop.ps1`，自动检测端口占用并智能递增顺延，双击即可直接运行。

---

## 🚀 快速开始

### 方式一：Windows 一键运行（推荐）

1. 确保电脑已安装 [Python 3.10+](https://www.python.org/) 和 [Node.js 18+](https://nodejs.org/)。
2. 双击运行根目录下的 **`start.bat`**。
   * 脚本会自动初始化虚拟环境、安装依赖、检查端口占用（默认 `3335`，被占用则自动顺延）并启动后台服务。
   * 如需停止服务，双击运行 **`stop.bat`** 即可。

### 方式二：手动命令行启动 (Windows / Linux / macOS)

```bash
# 1. 克隆代码
git clone https://github.com/zhengwuji/zcode2api.git
cd zcode2api

# 2. 安装 Python 依赖
python -m venv venv
# Linux / macOS: source venv/bin/activate
# Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. 安装无痕验证求解器依赖 (Node.js)
cd captcha_node && npm install && cd ..

# 4. 复制并调整配置文件（可选）
cp .env.example .env

# 5. 启动服务（默认监听端口 3335）
python main.py serve
```

服务启动后，可在浏览器访问：
* **管理后台**：`http://localhost:3335/admin`（默认管理密码：`zcode`）
* **OpenAI 兼容反代**：`http://localhost:3335/v1/chat/completions`
* **Anthropic 兼容反代**：`http://localhost:3335/v1/messages`
* **模型列表**：`http://localhost:3335/v1/models`

---

## 🔌 API 反代与客户端接入指南

通过 `zcode2api`，你可以将 Z.AI / BigModel 的模型无缝挂载到任何支持自定义 OpenAI 或 Claude 接口的客户端中：

### 核心接入参数一览

| 参数项 | 填写内容 | 说明 |
| :--- | :--- | :--- |
| **OpenAI 接口地址 (Base URL)** | `http://127.0.0.1:3335/v1` | 大多数软件（Cherry Studio / NextChat / Cursor 等）填此项 |
| **Claude 接口地址 (Base URL)** | `http://127.0.0.1:3335` | 针对 Cline、Roo Code 等 Anthropic 专用插件 |
| **API Key (网关密钥)** | `sk-zcode2api-...`（系统默认自动生成） | 初次启动默认随机生成一个安全密钥；可在后台「API 反代接入说明」弹窗或「设置」页直接一键复制，也支持随时自定义修改或点击随机重置 |
| **推荐模型名称** | `GLM-5.3`<br>`GLM-5.3-Flash`<br>`GLM-5.2`<br>`GLM-5-Turbo` | 支持别名映射，如 `glm-4-plus`、`claude-3-7-sonnet-20250219` |

---

### 常见客户端配置步骤

#### 1. Cherry Studio
1. 打开 **设置** → **模型服务商** → 点击 **添加**，类型选择 **OpenAI**。
2. **API 域名**：填入 `http://127.0.0.1:3335/v1`。
3. **API 密钥**：填入后台自动生成的密钥（在管理后台点击右上角【API 反代接入】即可一键复制，如 `sk-zcode2api-...`；由于 Cherry Studio 校验非空，请勿留空）。
4. **模型**：点击【添加模型】，输入 `GLM-5.3` 和 `GLM-5.3-Flash`，勾选启用并在右侧**勾选「视觉」**（支持识图）。

#### 2. NextChat (ChatGPT-Next-Web)
1. 点击左下角 **设置** → **模型服务商** 选择 **OpenAI**。
2. **接口地址 (URL)**：填入 `http://127.0.0.1:3335/v1`。
3. **API Key**：填入网关密钥或任意字符。
4. **自定义模型**：在输入框末尾追加 `+GLM-5.3,+GLM-5.3-Flash`，保存即可。

#### 3. Cursor
1. 进入 **Settings** → **Features** → **Models**。
2. 开启 **OpenAI API Key**。
3. 勾选 **Override OpenAI Base URL**，填入：`http://127.0.0.1:3335/v1`。
4. 在下方 **Model Names** 中点击【Add model】，添加 `GLM-5.3`。

#### 4. One-API / New API / Chatbox
1. **添加渠道** → 类型选择 **OpenAI**。
2. **Base URL**：填入 `http://127.0.0.1:3335`。
3. **密钥**：填入网关密钥。
4. **模型**：填入 `GLM-5.3,GLM-5.3-Flash,GLM-5.2,GLM-5-Turbo`。

#### 5. cURL 命令行测试
```bash
curl -X POST "http://127.0.0.1:3335/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-zcode" \
  -d '{
    "model": "GLM-5.3",
    "messages": [{"role": "user", "content": "你好，请做个自我介绍！"}],
    "stream": true
  }'
```

---

## 🐳 Docker 部署

镜像内已打包 Python 运行环境与 Node.js 无痕验证求解器，开箱即用：

```bash
# 方式一：Docker Compose（推荐）
docker compose up -d --build

# 方式二：Docker 原生命令
docker build -t zcode2api:latest .
docker run -d --name zcode2api \
  -p 3335:3335 \
  -v "$(pwd)/data:/data" \
  -e ZCODE_ADMIN_KEY=zcode \
  -e ZCODE_PORT=3335 \
  --restart unless-stopped \
  zcode2api:latest
```

* 容器内 `/data` 目录用于持久化 SQLite 数据库（账号池、设置等），挂载到宿主机即可持久化。

---

## 📦 支持的模型列表

| 模型名 (Model ID) | 视觉支持 (Vision) | 特点与推荐场景 |
| :--- | :---: | :--- |
| **`GLM-5.3`** | ✅ **支持** | 官方主力旗舰多模态模型，支持识图、看代码报错与页面设计图（推荐） |
| **`GLM-5.3-Flash`** | ✅ **支持** | 轻量极速多模态模型，响应延迟极低，写代码补全体验极佳 |
| **`GLM-5.2`** | ⚪ 部分支持 | 上一代通用旗舰模型 |
| **`GLM-5-Turbo`** | ❌ 仅文本 | 超长文本与纯代码推理模型 |
| **`glm-4-plus`** | 自动映射 | 兼容上一代接口命名规范 |
| **`claude-3-7-sonnet-20250219`** | 自动映射 | 兼容 Claude Code / Cline 的别名路由 |

---

## 🛡️ 安全与防封建议

1. **官方 API Key 模式**：
   * 官方本身对外提供商业调用，完全合法合规，**几乎无封号风险**。
2. **Coding Plan (JWT) 模式**：
   * **个人写代码、正常聊天**：行为特征、QPS 与桌面客户端一致，且系统固定携带设备指纹（`X-Device-Mid`），**非常安全**。
   * **请勿进行超高并发轰炸**：避免挂载到全量批量扫描器、爬虫等超高频工具上，避免单账号持续触发 429。
   * **建议开启自动切号**：账号池多放 2~3 个账号，开启顶部【自动切号】与【自动领取】，网关会自动进行安全平滑轮换。

---

## 📁 项目结构

```
├── app/
│   ├── main.py            # FastAPI 应用入口与生命周期管理
│   ├── settings.py        # 环境变量与默认配置
│   ├── models.py          # Account 实体、数据状态与 Public View
│   ├── store.py           # SQLite 持久化、轮询游标与设置存储
│   ├── agent.py           # 官方上游请求头组装（指纹、MID、版本仿真）
│   ├── claim.py           # 套餐资格查询（preview）与自动抢领（claim）
│   ├── captcha.py         # 阿里云无痕验证码管理器
│   ├── openai_bridge.py   # OpenAI / Anthropic 协议与多模态双向桥接器
│   ├── quota.py           # 实时额度/用量抓取与精确原因诊断
│   ├── oauth.py           # Z.AI / BigModel OAuth 授权登录
│   ├── auth_admin.py      # 后台与网关密钥鉴权
│   ├── logs.py            # 格式化彩色日志输出
│   ├── routes/            # API 路由：gateway / admin_api / pages
│   └── statics/           # 前端样式、JS 逻辑与后台页面 (accounts.html 等)
├── captcha_node/          # 无浏览器无痕验证求解器（Node + jsdom）
├── main.py                # CLI 入口与服务启动调度
├── start.bat / start.ps1  # Windows 一键启动脚本（含端口碰撞自增）
├── stop.bat / stop.ps1    # Windows 一键停止脚本
├── requirements.txt       # Python 依赖清单
├── Dockerfile             # 容器镜像构建文件
├── docker-compose.yml     # Compose 编排文件
└── .env.example           # 环境变量示例模板
```

---

## 📄 许可证与免责声明

本项目采用 [AGPL-3.0](LICENSE) 许可证开源。

**免责声明**：本项目仅供学习、研究、个人开发辅助及实验验证使用。请勿将本项目用于违反相关平台服务协议或法律法规的场景。使用者应对自身的使用行为及账号安全承担全部责任。
