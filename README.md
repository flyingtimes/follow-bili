# follow-bili

自动抓取关注的 B 站 UP 主最新视频，下载后提取音频、语音转文字、AI 摘要，并发送到微信。

## 工作流程

1. 读取 `config.csv` 中的 UP 主 username 列表
2. 通过 `opencli` 获取每位 UP 主最新 2 条视频信息
3. 存入 SQLite 数据库（`videos.db`），按 `username + vid` 去重
4. 下载新视频到 `video/` 目录
5. 使用 `ffmpeg` 提取音频到 `audio/` 目录
6. 使用 MLX Whisper（Apple Silicon 优化）进行语音转文字，保存到 `transcript/`
7. 通过 `openclaw agent` 生成中文摘要（300 字以内），保存到 `summary/`
8. 将音频文件 + 摘要文本发送到微信

## 目录结构

```
follow-bili/
├── config.csv      # UP 主 username 列表（每行一个）
├── videos.db       # SQLite 数据库
├── video/          # 下载的视频文件
├── audio/          # 提取的音频文件（MP3）
├── transcript/     # 语音转文字结果
├── summary/        # AI 摘要
├── main.py         # 主程序
└── .env            # 环境变量（WECHAT_TARGET）
```

## 使用方法

```bash
# 运行完整流程
python3 main.py

# 查看数据库中的所有视频记录
python3 main.py list
```

## 依赖

- [opencli](https://github.com/opencli/opencli) — Bilibili 视频 fetching 和下载
- [openclaw](https://github.com/openclaw/openclaw) — AI 摘要和微信消息发送
- ffmpeg — 音频提取
- mlx-whisper — Apple Silicon 优化的语音识别
- python-dotenv — 环境变量管理

## 配置

### config.csv

每行一个 B 站 UP 主的 username：

```csv
小Lin说
艾丽的无废话财经
AI超元域
刘悦的技术博客
李论科学
妈咪说MommyTalk
```

### .env

```env
WECHAT_TARGET=你的微信目标ID
```

## 数据库结构

`videos` 表：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| username | TEXT | UP 主名称 |
| rank | INTEGER | 排名 |
| title | TEXT | 视频标题 |
| plays | INTEGER | 播放量 |
| likes | INTEGER | 点赞数 |
| date | TEXT | 发布日期 |
| vid | TEXT | 视频 ID |
| url | TEXT | 视频链接 |
| downloaded | TEXT | 是否已下载（y/n） |
| download_path | TEXT | 下载路径 |
