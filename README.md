# MaiBot B站视频插件

自动识别 B 站视频链接 → 渲染完整风格卡片 → 下载视频 → 合并转发为一条消息。

## ✨ 功能

- 🔍 **自动识别**：BV号、长链、短链（b23.tv）自动识别
- 🎨 **完整B站卡片**：哔哩哔哩标头、封面、6项数据胶囊、UP主信息、简介、弹幕样本、热门评论
- 📥 **视频下载**：自动下载视频（支持画质/大小/时长限制）
- 📦 **合并转发**：卡片 + 视频在一条合并转发消息中送达
- 🚫 **智能去重**：60秒内相同视频不重复处理；引用消息中的链接自动忽略
- 🧹 **自动清理**：缓存文件定期清理，避免占用硬盘

## 📦 安装

### 1. 复制插件

```bash
cp -r maibot_plugin_bilivideo E:\cp\maibot\MaiBotOneKey\modules\MaiBot\plugins\
```

### 2. 安装依赖

```bash
cd E:\cp\maibot\MaiBotOneKey
.\runtime\python31211\python.exe -m pip install bilibili-api-python httpx aiohttp jinja2 playwright

# 首次使用还需要安装 chromium
.\runtime\python31211\python.exe -m playwright install chromium
```

### 3. 配置

编辑 `config.toml`：

```toml
[bilibili]
sessdata = "你的SESSDATA"   # 强烈推荐填写，可下载高画质
bili_jct = "你的bili_jct"

[download]
enabled = true
quality = "720P"            # 360P/480P/720P/1080P/1080P+/4K
max_size_mb = 100
max_duration_seconds = 600  # 视频时长上限（秒）
```

### 4. 重启 MaiBot

## 🎮 使用

直接在群聊或私聊发送 B 站链接即可：

```
用户: https://www.bilibili.com/video/BV1xx411c7mD
```

麦麦会回复一条**合并转发消息**，里面包含：
1. 完整 B 站风格卡片图片（封面/标题/统计/UP主/简介/弹幕/热评）
2. 下载好的视频文件

## ⚙️ 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `auto_detect.use_image_card` | `true` | 用图片卡片，false则发纯文本 |
| `auto_detect.ignore_replied_messages` | `true` | 忽略引用消息中的链接（防止解析对话历史里的链接） |
| `download.enabled` | `true` | 是否下载视频 |
| `download.quality` | `"720P"` | 默认画质 |
| `download.max_size_mb` | `100` | 视频大小上限 |
| `download.max_duration_seconds` | `600` | 视频时长上限（秒），超过则只发卡片 |
| `download.allow_quality_fallback` | `true` | 大小超限时自动降画质 |
| `cache.auto_clean` | `true` | 自动清理缓存 |
| `cache.max_age_hours` | `24` | 文件保留时间 |

## 📁 文件结构

```
maibot_plugin_bilivideo/
├── plugin.py                # 主插件 + 事件处理器
├── _manifest.json           # 插件元数据
├── config.toml              # 配置文件
├── requirements.txt
├── core/
│   ├── link_resolver.py     # 链接解析 + 弹幕/评论拉取
│   ├── video_downloader.py  # 视频下载
│   └── card_renderer.py     # Playwright卡片渲染
├── render/
│   └── bili_card.html       # 卡片HTML模板
└── utils/
    └── cache_manager.py     # 缓存清理
```

## 🐛 故障排查

| 现象 | 原因 / 解决 |
|---|---|
| 卡片渲染失败 | 检查是否安装了 `playwright` 和 `chromium`：`python -m playwright install chromium` |
| 视频下载失败 | 检查 `bilibili.sessdata` 是否填写；高画质需登录 |
| 没有弹幕/热评 | 视频可能关闭了弹幕或评论；接口偶尔抽风可重试 |
| 引用消息也被解析 | 确认 `auto_detect.ignore_replied_messages = true` |
| 不下载视频 | 检查 `download.enabled` / 视频时长是否超过 `max_duration_seconds` |

## 🤝 来源

整合自：
- [astrbot_plugin_link_resolver](https://github.com/Soulter/astrbot_plugin_link_resolver) - 链接解析与下载
- [astrbot_plugin_biliVideo](https://github.com/Soulter/astrbot_plugin_biliVideo) - 部分卡片设计灵感

## 📄 License

MIT
