"""推送渠道实现：Server酱 / pushplus / SMTP 邮件 / 钉钉 / 飞书。

所有渠道的 send() 统一签名为 (title, markdown)，各自负责把 markdown
转换成自家格式（邮件/飞书转纯文本，钉钉/Server酱直接吃 markdown）。
"""
import base64
import hashlib
import hmac
import re
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import requests

TIMEOUT = 20
MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# 北京时间，无夏令时
TZ = timezone(timedelta(hours=8))


def md_links_to_text(md: str) -> str:
    """[text](url) → text：url，供不支持 markdown 的渠道用。"""
    return MD_LINK.sub(lambda m: f"{m.group(1)}：{m.group(2)}", md).replace("**", "")


# 时间节点类型的展示配置：(图标, 前缀, 是否催办)
TIMING_STYLE = {
    "start":    ("🕐", "开始", False),
    "deadline": ("⏰", "截止", True),
    "signup":   ("⏰", "报名截止", True),
    "event":    ("📅", "举行", False),
    "exam":     ("📝", "考试", False),
}


def _parse_dt(value: str, end_of_day: bool = False) -> datetime | None:
    """把 YYYY-MM-DD[ HH:MM] 解析成北京时间 datetime；失败返回 None。"""
    if not value:
        return None
    try:
        if len(value) > 10:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
        else:
            dt = datetime.strptime(value, "%Y-%m-%d")
            if end_of_day:  # 只给了日期时，截止按当天 23:59 计
                dt = dt.replace(hour=23, minute=59)
        return dt.replace(tzinfo=TZ)
    except ValueError:
        return None


def _fmt_dt(dt: datetime, now: datetime | None) -> str:
    """展示日期：同年省略年份，带时间则一并显示。"""
    same_year = now is None or dt.year == now.year
    base = f"{dt:%m-%d}" if same_year else f"{dt:%Y-%m-%d}"
    if dt.hour or dt.minute:
        base += f" {dt:%H:%M}"
    return base


def _countdown(target: datetime, now: datetime, urgent: bool) -> str:
    """距目标时间的人话描述。urgent=催办语气（还剩），否则中性（X 天后）。"""
    secs = (target - now).total_seconds()
    if secs < 0:
        return "已过期"
    if secs < 3600:
        n = max(1, int(secs // 60))
        return f"还剩 {n} 分钟" if urgent else f"{n} 分钟后"
    if secs < 86400:
        n = int(secs // 3600)
        return f"还剩 {n} 小时" if urgent else f"{n} 小时后"
    n = int(secs // 86400)
    return f"还剩 {n} 天" if urgent else f"{n} 天后"


def _iter_timings(it):
    """统一的 timings 迭代器，含对旧 start/deadline 字段的兼容。"""
    timings = list(getattr(it, "timings", None) or [])
    if not timings:
        if s := getattr(it, "start", ""):
            timings.append({"type": "start", "time": s})
        if d := getattr(it, "deadline", ""):
            timings.append({"type": "deadline", "time": d})
    return timings


def timing_lines(it, now: datetime | None = None) -> list[str]:
    """按类型生成时间展示行。

    it 需有 timings 字段（list[dict]），兼容旧的 start/deadline 字段。
    """
    now = now or datetime.now(TZ)
    timings = _iter_timings(it)
    order = {t: i for i, t in enumerate(TIMING_STYLE)}
    timings = sorted(timings, key=lambda t: order.get(t.get("type", ""), 99))

    lines = []
    for t in timings:
        ttype = t.get("type", "")
        icon, label, urgent = TIMING_STYLE.get(ttype, ("🕐", ttype, False))
        # 展示用原始日期；催办判定用"当天结束"，避免只给日期时被当成当天 00:00 而误判过期
        show = _parse_dt(t.get("time", ""))
        cmp_dt = _parse_dt(t.get("time", ""), end_of_day=urgent)
        if show is None or cmp_dt is None:
            continue
        text = f"{icon} {label} {_fmt_dt(show, now)}"
        if end_dt := _parse_dt(t.get("end", "")):
            text += f" 至 {_fmt_dt(end_dt, now)}"
        text += f"（{_phase_note(cmp_dt, now, urgent, ttype)}）"
        lines.append(text)
    return lines


def _phase_note(target: datetime, now: datetime, urgent: bool, ttype: str) -> str:
    """生成括号里的状态说明：未来的催办提示/中性描述，过去的按类型给出完成态。"""
    if target >= now:
        return _countdown(target, now, urgent)
    if ttype == "start":
        return "已开始"
    if ttype in ("event", "exam"):
        return "已举行" if ttype == "event" else "已结束"
    return "已过期"


def due_soon(it, now: datetime | None = None, hours: int = 24) -> list[dict]:
    """返回在 hours 小时内到期、且尚未过期的催办类时间节点（用于临近提醒）。"""
    now = now or datetime.now(TZ)
    soon = []
    for t in _iter_timings(it):
        if t.get("type") not in ("deadline", "signup"):
            continue
        dt = _parse_dt(t.get("time", ""), end_of_day=True)
        if dt and now <= dt <= now + timedelta(hours=hours):
            soon.append(t)
    return soon


def item_markdown(it, now: datetime | None = None) -> str:
    summary = f"**摘要**：{it.summary}  \n" if getattr(it, "summary", "") else ""
    timing = "".join(f"**{line}**  \n" for line in timing_lines(it, now))
    return (
        f"**来源**：{it.source}  \n"
        f"**日期**：{it.date or '未知'}  \n\n"
        f"{summary}"
        f"{timing}\n"
        f"[📖 打开原文]({it.url})"
    )


def alert_markdown(it, timings: list[dict], now: datetime | None = None) -> str:
    """临近截止提醒的正文。"""
    now = now or datetime.now(TZ)
    rows = []
    for t in timings:
        icon, label, urgent = TIMING_STYLE.get(t.get("type", ""), ("⏰", "截止", True))
        show = _parse_dt(t.get("time", ""))
        cmp_dt = _parse_dt(t.get("time", ""), end_of_day=urgent)
        if show and cmp_dt:
            rows.append(f"**{icon} {label} {_fmt_dt(show, now)}（{_countdown(cmp_dt, now, urgent)}）**")
    return (
        f"**来源**：{it.source}  \n"
        f"**通知**：{getattr(it, 'title', '')}\n\n"
        + "".join(f"{r}  \n" for r in rows)
        + f"\n[📖 打开原文]({it.url})"
    )


class ServerChan:
    """Server酱·Turbo，密钥从 https://sct.ftqq.com/ 扫码获取。"""

    name = "Server酱"

    def __init__(self, sendkey):
        self.sendkey = sendkey

    def send(self, title, markdown):
        r = requests.post(
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            data={"title": title, "desp": markdown},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Server酱返回异常: {data}")


class PushPlus:
    """pushplus，token 从 https://www.pushplus.plus/ 获取。"""

    name = "pushplus"

    def __init__(self, token):
        self.token = token

    def send(self, title, markdown):
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": self.token, "title": title, "content": markdown, "template": "markdown"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"pushplus 返回异常: {data}")


class DingTalk:
    """钉钉群自定义机器人（markdown 消息，支持加签）。"""

    name = "钉钉"

    def __init__(self, webhook, secret=""):
        self.webhook, self.secret = webhook, secret

    def send(self, title, markdown):
        url = self.webhook
        if self.secret:
            ts = str(round(time.time() * 1000))
            sign = base64.b64encode(
                hmac.new(self.secret.encode(), f"{ts}\n{self.secret}".encode(), hashlib.sha256).digest()
            ).decode()
            url += f"&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
        r = requests.post(
            url,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": markdown}},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"钉钉返回异常: {data}")


class Feishu:
    """飞书群自定义机器人（富文本 post 消息，支持签名校验）。"""

    name = "飞书"

    def __init__(self, webhook, secret=""):
        self.webhook, self.secret = webhook, secret

    def send(self, title, markdown):
        payload = {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title, "content": self._post_content(markdown)}}},
        }
        if self.secret:
            ts = str(int(time.time()))
            sign = base64.b64encode(
                hmac.new(f"{ts}\n{self.secret}".encode(), b"", hashlib.sha256).digest()
            ).decode()
            payload.update({"timestamp": ts, "sign": sign})
        r = requests.post(self.webhook, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"飞书返回异常: {data}")

    @staticmethod
    def _post_content(markdown):
        rows = []
        for raw in markdown.split("\n"):
            raw = raw.replace("**", "").lstrip("# ").rstrip()
            row, pos = [], 0
            for m in MD_LINK.finditer(raw):
                if m.start() > pos:
                    row.append({"tag": "text", "text": raw[pos : m.start()]})
                row.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
                pos = m.end()
            if raw[pos:] or not row:
                row.append({"tag": "text", "text": raw[pos:]})
            rows.append(row)
        return rows


class SMTPChannel:
    """SMTP 邮件。QQ 邮箱需在设置里开启 SMTP 并使用授权码。"""

    name = "邮件"

    def __init__(self, host, port, user, password, to_addrs):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.to_addrs = to_addrs

    def send(self, title, markdown):
        msg = MIMEText(md_links_to_text(markdown), "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = self.user
        msg["To"] = ", ".join(self.to_addrs)
        with smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT) as s:
            s.login(self.user, self.password)
            s.sendmail(self.user, self.to_addrs, msg.as_string())
