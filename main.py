#!/usr/bin/env python3
"""hbu-notifier：河北大学教务处/主页通知抓取与推送。

用法：
  python main.py                 # 抓取全部源，新消息即时推送
  python main.py --mode digest   # 发送自上次摘要以来的新增汇总
  python main.py --dry-run       # 只抓取打印，不推送不写状态
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tomllib

import notify
import scraper

# 北京时间（UTC+8，无夏令时），写死偏移量避免依赖系统时区库
TZ = timezone(timedelta(hours=8))
STATE_MAX_ITEMS = 600  # 状态文件最多保留的条目数，防止无限膨胀


def now() -> datetime:
    return datetime.now(TZ)


def resolve(value):
    """配置值支持 $ENV / ${ENV} 形式引用环境变量，密钥不落仓库。"""
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value.lstrip("$").strip("{}").strip(), "")
    return value


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[WARN] 状态文件损坏，已重置：{path}")
    return {"items": {}, "last_digest": None}


def save_state(path: Path, state: dict) -> None:
    items = state["items"]
    if len(items) > STATE_MAX_ITEMS:
        keep = sorted(items.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)
        state["items"] = dict(keep[: STATE_MAX_ITEMS - 100])
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_channels(cfg: dict) -> list:
    cc = cfg.get("channels", {})

    def take(section, key):
        return resolve(cc.get(section, {}).get(key, ""))

    chans = []
    if cc.get("serverchan", {}).get("enabled"):
        if key := take("serverchan", "sendkey"):
            chans.append(notify.ServerChan(key))
        else:
            print("[WARN] Server酱未配置（缺 SERVERCHAN_SENDKEY），跳过")
    if cc.get("pushplus", {}).get("enabled"):
        if token := take("pushplus", "token"):
            chans.append(notify.PushPlus(token))
        else:
            print("[WARN] pushplus 未配置（缺 PUSHPLUS_TOKEN），跳过")
    if cc.get("smtp", {}).get("enabled"):
        user, password = take("smtp", "user"), take("smtp", "password")
        to = [x.strip() for x in take("smtp", "to").split(",") if x.strip()]
        if user and password and to:
            chans.append(notify.SMTPChannel(take("smtp", "host"), int(take("smtp", "port") or 465), user, password, to))
        else:
            print("[WARN] 邮件渠道未配置齐（SMTP_USER/SMTP_PASS/SMTP_TO），跳过")
    if cc.get("dingtalk", {}).get("enabled"):
        if webhook := take("dingtalk", "webhook"):
            chans.append(notify.DingTalk(webhook, take("dingtalk", "secret")))
        else:
            print("[WARN] 钉钉未配置（缺 DINGTALK_WEBHOOK），跳过")
    if cc.get("feishu", {}).get("enabled"):
        if webhook := take("feishu", "webhook"):
            chans.append(notify.Feishu(webhook, take("feishu", "secret")))
        else:
            print("[WARN] 飞书未配置（缺 FEISHU_WEBHOOK），跳过")
    if not chans:
        print("[WARN] 没有任何可用推送渠道，抓到的新消息只会写进 state.json")
    return chans


def channels_send(channels, title, markdown) -> int:
    ok = 0
    for ch in channels:
        try:
            ch.send(title, markdown)
            print(f"[推送→{ch.name}] 成功")
            ok += 1
        except Exception as e:
            print(f"[推送→{ch.name}] 失败: {e}")
    return ok


def keyword_ok(title: str, include: list, exclude: list) -> bool:
    if include and not any(k in title for k in include):
        return False
    return not any(k in title for k in exclude)


def run_check(cfg, state_path: Path, channels, dry_run=False) -> None:
    state = load_state(state_path)
    first_run = not state["items"]
    all_new, failed = [], []

    for _, s in cfg.get("sources", {}).items():
        if not s.get("enabled", True):
            continue
        print(f"抓取：{s['name']} <{s['url']}>")
        try:
            items = scraper.fetch_list(s["url"], s.get("base_url", ""), s["name"])
        except Exception as e:
            print(f"[WARN] 抓取失败 {s['name']}: {e}")
            failed.append(s["name"])
            continue
        if not items:
            print(f"[WARN] 源「{s['name']}」解析到 0 条，站点结构可能变化，请检查")
        new = [it for it in items if it.url not in state["items"]]
        print(f"  共 {len(items)} 条，新 {len(new)} 条")
        for it in new:
            state["items"][it.url] = {
                "title": it.title,
                "source": it.source,
                "date": it.date,
                "first_seen": now().isoformat(timespec="seconds"),
            }
        all_new.extend(new)

    flt = cfg.get("filter", {})
    todo = [it for it in all_new if keyword_ok(it.title, flt.get("include_keywords", []), flt.get("exclude_keywords", []))]

    if first_run and todo and not dry_run:
        print(f"首次运行：{len(todo)} 条已记录为已读，不推送（避免刷屏）")
        todo = []

    if dry_run:
        print(f"[dry-run] 候选推送 {len(todo)} 条：")
        for it in todo:
            print(f"  - 【{it.source}】{it.title} {it.date} {it.url}")
        return

    for it in todo:
        channels_send(channels, f"【{it.source}】{it.title}", notify.item_markdown(it))
    save_state(state_path, state)

    if failed:
        alert = f"⚠️ hbu-notifier 以下源抓取失败：{'、'.join(failed)}"
        channels_send(channels, alert, alert)


def run_digest(cfg, state_path: Path, channels, dry_run=False) -> None:
    state = load_state(state_path)
    last = state.get("last_digest")
    cutoff = datetime.fromisoformat(last) if last else None

    entries = sorted(
        (t, url, v)
        for url, v in state["items"].items()
        if (t := datetime.fromisoformat(v["first_seen"])) and (cutoff is None or t > cutoff)
    )

    lines = []
    if entries:
        by_source: dict[str, list] = {}
        for _, url, v in entries:
            by_source.setdefault(v["source"], []).append((url, v))
        for src, lst in by_source.items():
            lines.append(f"### {src}（{len(lst)} 条）\n")
            lines.extend(f"- [{v['title']}]({url})（{v.get('date') or '日期未知'}）" for url, v in lst)
            lines.append("")
    elif not cfg.get("general", {}).get("digest_empty", True):
        print("本时段无新通知，且配置为不发空摘要，跳过")
        return
    else:
        lines.append("本时段没有新通知。")

    window = f"{cutoff.astimezone(TZ):%m-%d %H:%M} → {now():%m-%d %H:%M}" if cutoff else "首次汇总"
    title = f"河大通知汇总 {now():%m-%d %H:%M}（新增 {len(entries)} 条）"
    md = f"## 📚 {title}\n\n**时段**：{window}\n\n" + "\n".join(lines)

    if dry_run:
        print(f"[dry-run] 摘要标题：{title}\n{md}")
        return
    channels_send(channels, title, md)
    state["last_digest"] = now().isoformat(timespec="seconds")
    save_state(state_path, state)


def main():
    ap = argparse.ArgumentParser(description="河北大学通知抓取推送")
    ap.add_argument("--mode", choices=["check", "digest"], default="check")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state_path = Path(cfg.get("general", {}).get("state_file", "state.json"))
    channels = build_channels(cfg)

    try:
        if args.mode == "digest":
            run_digest(cfg, state_path, channels, args.dry_run)
        else:
            run_check(cfg, state_path, channels, args.dry_run)
    except Exception:
        err = traceback.format_exc()[-800:]
        print(err, file=sys.stderr)
        if not args.dry_run:
            try:
                channels_send(channels, "⚠️ hbu-notifier 运行出错", f"```\n{err}\n```")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
