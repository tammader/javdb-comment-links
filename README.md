# javdb-comment-links

每周定时爬取 JavDB 移动端 API 的评论区，针对 139 个人云指定目录下所有视频对应的影片，
提取 **2026-09-01 之后**评论里的 `magnet:` / `ed2k://` 链接，去重后输出 CSV / TXT，
并把汇总报告推送到 Telegram。

纯 Python 标准库，无需安装第三方依赖。

## 架构（云端直连，不依赖本机 alist）

```
GitHub Actions (每周一 03:17 北京)
  └─ tools/crawler.py
       ├─ ty_cloud_personal.py : 用 YDY_AUTHORIZATION 登录 139 个人云，递归列指定目录
       ├─ javdb_api.py         : JavDB 签名 + 登录 + 搜索/评论接口
       ├─ 番号归一化 → 影片ID → 评论区 → 抽 magnet/ed2k → 去重
       └─ 输出 CSV/TXT + 增量 state（commit 回仓库）
```

`ty_cloud_personal.py` 能在云端直接列 139 个人云目录，所以跑在 GitHub runner 上也不需要本机 alist。

## 配置（Secrets）

Settings → Secrets and variables → Actions 添加：

| Secret | 说明 |
| --- | --- |
| `YDY_AUTHORIZATION` | 139 个人云 authorization 串（约 30 天过期，见下方维护） |
| `YDY_FOLDERS` | 要合并抓取的目录名，逗号分隔（如 `目录A,目录B`），放在 Secret 里避免公开 |
| `JAVDB_USERNAME` | JavDB 登录邮箱（建议用专用小号，主号容易被风控） |
| `JAVDB_PASSWORD` | JavDB 登录密码 |
| `TG_TOKEN` | Telegram bot token（跑完自动发报告用，可留空则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（接收报告） |

> 公开仓库的 workflow 文件里不含任何目录名 / 账号 / token，全部走 Secrets。

## 触发

Actions → `javdb-comment-links` → Run workflow（可填起始日期 cutoff）。
或等每周一 03:17 自动跑。

## Telegram 报告

跑完 crawler 后自动执行 `tools/tg_javdb_report.py --send`，发一份合并报告：
各目录汇总（片数 / 链接数 magnet+ed2k / 时间范围）+ 最新 8 条链接。
消息底部带两个 inline 按钮：`⚡ 立即重跑`、`🔄 刷新报告`（按钮回调需另一个常驻轮询进程配合，可选）。

本地单独用：
```bash
JAVDB_OUT_DIR=. python tools/tg_javdb_report.py --text   # 只看文案
```

## 维护

### 139 token 约 30 天过期
到期后重新获取 authorization，更新 Secret `YDY_AUTHORIZATION` 即可。
过期表现：日志 `!! 139 登录失败`。

### JavDB 账号风控
- 脚本已限速（每请求 `SLEEP_REQ` 秒，默认 1.6）。**不要把 SLEEP_REQ 调太小**。
- 遇到 `!! BANNED`：账号被禁，换小号或等解封。
- 增量 state 保证已解析过的影片不会重复搜索，降低请求量。

## 增量说明
- `state_javdb.json` 记录：已解析番号→影片ID、无匹配番号、已抓链接、输出行。
- 每周运行只处理**新出现的视频/番号**；已处理的跳过。删掉 state 会全量重跑。

## 本地自测
```bash
export YDY_AUTHORIZATION='...'
export YDY_FOLDERS='目录A,目录B'
export JAVDB_USERNAME='...'
export JAVDB_PASSWORD='...'
export MAX_CODES=5           # 只跑前 5 个番号
python tools/crawler.py
```

## 文件
- `tools/crawler.py` — 主流程（多目录合并 + 增量 + 限速重试）
- `tools/javdb_api.py` — JavDB 移动端 API 客户端（含 jdsignature 签名）
- `tools/ty_cloud_personal.py` — 139 个人云客户端（列目录 / 取直链 / 刷新）
- `tools/tg_javdb_report.py` — Telegram 报告生成与发送
- `.github/workflows/javdb-comment-links.yml` — 定时工作流

## 免责声明
仅供个人学习与技术研究。请遵守目标站点的服务条款与当地法律法规。
