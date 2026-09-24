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
from email.header import Header
from email.mime.text import MIMEText

import requests

TIMEOUT = 20
MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def md_links_to_text(md: str) -> str:
    """[text](url) → text：url，供不支持 markdown 的渠道用。"""
    return MD_LINK.sub(lambda m: f"{m.group(1)}：{m.group(2)}", md).replace("**", "")


def item_markdown(it) -> str:
    summary = f"**摘要**：{it.summary}  \n\n" if getattr(it, "summary", "") else ""
    return (
        f"**来源**：{it.source}  \n"
        f"**日期**：{it.date or '未知'}  \n\n"
        f"{summary}"
        f"[📖 打开原文]({it.url})"
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
