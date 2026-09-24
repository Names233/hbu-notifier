# hbu-notifier

河北大学教务处 / 学校主页通知自动抓取 + AI 一句话摘要 + 多渠道推送。跑在 GitHub Actions 上，每 30 分钟检查一次新通知，新消息即时推送（附 AI 摘要），每天早 8:20 / 晚 21:30（北京时间）各发一条汇总。

## AI 一句话摘要

每条通知推送时，程序会抓取详情页正文，交给 AI 概括成一句话（默认不超过 60 字），让标题党式的长通知一眼就能看明白重点。

- **模型**：走 freeshare 中转（`https://freeshare.cc.cd/v1`，OpenAI 兼容）。主模型 `kimi-k3`，失败自动回退 `deepseek-v4.1-flash` → `glm-5.3-flash`，每个模型重试 2 次。该中转站偶发限流/空回复，回退链就是为此设计的。
- **密钥**：需要在仓库 Secrets 里配 `FREESHARE_API_KEY`（就是那个 `sk-` 开头的 key）。**这是 AI 摘要能用的前提**，没配的话整条推送仍会正常发出，只是不带摘要。
- **降级原则**：AI 或抓正文任何一步失败，程序绝不会漏推或卡住，只是这条推送少一行摘要；摘要还会存进 `state.json`，供汇总复用。
- **换模型 / 关掉**：改 `config.toml` 的 `[ai]` 段。`enabled = false` 即完全关闭；换 `base_url` + `api_key` + `model` 即可切到任意 OpenAI 兼容服务（如官方 DeepSeek、智谱）。
- **换服务商时注意**：部分模型（如 kimi-k3）只接受默认 `temperature`，所以程序不传这个参数；思考型模型需要较大的 `max_tokens`（程序已设 2000），否则 `content` 会为空。

## 监控的信息源

| 源 | 地址 |
| --- | --- |
| 教务处·通知公告 | https://jwc.hbu.cn/index/tzgg.htm |
| 教务处·教务快讯 | https://jwc.hbu.cn/index/jwkx.htm |
| 学校主页·通知公告 | https://hbu.edu.cn/tzgg.htm |
| 学校主页·河大新闻 | https://hbu.edu.cn/hdxw.htm |
| 教育教学·本科生教育 | https://hbu.edu.cn/jyjx1/bksjy1.htm |

增删信息源：在 [config.toml](config.toml) 的 `[sources.xxx]` 里照抄一段改 URL 即可，任何"博达 CMS"风格的列表页（链接形如 `info/数字/数字.htm`）都能直接识别。

## 推送渠道（可同时开启多个）

| 渠道 | 需要的 Secrets | 怎么拿 |
| --- | --- | --- |
| AI 摘要 | `FREESHARE_API_KEY` | freeshare.cc.cd 中转站密钥（`sk-` 开头）。**不配也能用，只是推送不带摘要** |
| 微信（Server酱） | `SERVERCHAN_SENDKEY` | 用微信登录 [sct.ftqq.com](https://sct.ftqq.com/)，微信推送页面复制 SendKey。免费版每天约 5 条，高峰期不够可换 pushplus |
| 微信（pushplus） | `PUSHPLUS_TOKEN` | 微信登录 [pushplus.plus](https://www.pushplus.plus/) 首页复制 token |
| QQ 邮箱 | `SMTP_USER` `SMTP_PASS` `SMTP_TO` | QQ 邮箱 → 设置 → 账号 → 开启 SMTP 服务获得**授权码**（不是 QQ 密码）。USER=你的 QQ 邮箱，PASS=授权码，TO=收件邮箱（可以就是自己） |
| 钉钉 | `DINGTALK_WEBHOOK` `DINGTALK_SECRET` | 建个群 → 群设置 → 智能群助手 → 添加自定义机器人 → 安全设置选"加签"，得到 webhook 和密钥 |
| 飞书 | `FEISHU_WEBHOOK` `FEISHU_SECRET` | 群设置 → 群机器人 → 添加自定义机器人，安全设置选"签名校验" |

密钥配置在仓库 **Settings → Secrets and variables → Actions → New repository secret**，名字与上表一致。哪个渠道配了 Secrets 哪个就生效，没配的自动跳过。不想用某渠道也可以在 `config.toml` 里把它的 `enabled` 改为 `false`。

## 运行

- **GitHub Actions（主方式）**：推送到 GitHub 后自动生效，`.github/workflows/schedule.yml` 每 30 分钟抓一次，并在北京时间 8:20 / 21:30 发汇总；也可以在 Actions 页面手动 Run workflow 触发（可选 check / digest 模式）。
- **本地手动跑**：

  ```bash
  pip install -r requirements.txt
  python main.py --dry-run        # 只抓取打印，不推送不写状态
  python main.py                  # 抓取 + 推送新消息
  python main.py --mode digest    # 手动发一条汇总
  ```

## 状态与去重

已推送过的文章记录在 `state.json`（按文章 URL 去重），Actions 每次运行后会把它提交回仓库，重启/换机器都不丢。**首次运行只记录不推送**，避免把几百条历史消息刷给你；之后只推新出现的。摘要按"上次摘要时间 → 本次"之间的新增来发。

## 常见问题

- **Actions 定时为什么不准点？** GitHub 的 schedule 有几分钟到十几分钟的随机延迟，属正常现象。
- **Actions 分钟数**：公开仓库免费无限量；若仓库转私有，每 30 分钟一次约 1500 分钟/月，会吃掉私有仓库免费额度（2000 分钟/月）。
- **推送突然停了？** 大概率站点改版导致解析到 0 条，看 Actions 运行日志里有没有 `解析到 0 条` 或 `抓取失败` 的 WARN；有源挂了程序也会主动推送一条告警。
- **关键词过滤**：`config.toml` 的 `[filter]`，`include_keywords` 非空时只推命中的，`exclude_keywords` 命中则不推。

## 文件结构

```
main.py                  入口：抓取→去重→AI 摘要→推送 / 汇总
scraper.py               列表页通用解析 + 详情页正文抽取
ai.py                    AI 一句话摘要（freeshare 端点，多模型回退 + 失败降级）
notify.py                五个推送渠道实现
config.toml              信息源、AI、渠道、过滤配置（无敏感信息）
state.json               已推送记录（含摘要，Actions 自动提交回仓库）
.github/workflows/schedule.yml   定时任务
```
