"""AI 摘要：把通知正文压缩成一句话。

默认走 freeshare 中转（OpenAI 兼容端点，key 用环境变量 FREESHARE_API_KEY 引用）。
该中转站偶发 RPM/TPM 限流与空回复，故支持多模型回退 + 重试。

设计原则：任何失败都返回 None，由调用方降级为"不带摘要"推送，绝不阻塞通知。
"""
import os
import re
import time

import requests

DEFAULT_BASE_URL = "https://freeshare.cc.cd/v1"
DEFAULT_MODEL = "kimi-k3"
DEFAULT_FALLBACKS = ["deepseek-v4.1-flash", "glm-5.3-flash"]


def _clean(text: str, limit: int) -> str | None:
    """清掉模型爱加的前缀/换行/markdown 残留，截到目标长度。"""
    if not text:
        return None
    text = " ".join(text.split())
    text = text.replace("**", "").strip().strip('"“”\'')
    # 去掉 "通知概括：" "摘要：" 等开头标签（只吃标签词，不吃正文）
    text = re.sub(r"^(通知概括|摘要|概括|总结|一句话总结)\s*[:：]?\s*", "", text)
    # 去掉 "- " "• " "1. " "1、" 等列表符号；要求数字后跟标点，避免吃掉「9月15日」的 9
    text = re.sub(r"^(?:[-*•·]\s*|\d+\s*[.、)）]\s*)", "", text)
    return text.strip()[: limit + 10] or None

SYSTEM_PROMPT = (
    "你是校园通知摘要助手。请把收到的学校通知概括成一句话，帮助同学一眼看懂重点。"
    "要求：不超过{limit}个汉字；写清「谁、要做什么、关键时间或地点」；"
    "只输出概括正文，不要任何前缀、引号、解释或 markdown。"
)


class Summarizer:
    def __init__(self, base_url=DEFAULT_BASE_URL, api_key="", model=DEFAULT_MODEL,
                 fallback_models=None, max_body=1500, max_summary=60, timeout=90, retries=2):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("FREESHARE_API_KEY", "")
        # 主模型 + 回退链，去重保序
        self.models = list(dict.fromkeys([model or DEFAULT_MODEL] + list(fallback_models or DEFAULT_FALLBACKS)))
        self.max_body = max_body
        self.max_summary = max_summary
        self.timeout = timeout
        self.retries = retries

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _call(self, model: str, title: str, body: str) -> str | None:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(limit=self.max_summary)},
                {"role": "user", "content": f"标题：{title}\n\n正文：{body}"},
            ],
            "max_tokens": 2000,  # 给思考型模型留足额度，否则 content 可能为空
            # 不传 temperature：部分中转模型（如 kimi-k3）只接受默认值，传值会 400
        }
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        choice = (r.json().get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        return _clean(text, self.max_summary)

    def summarize(self, title: str, body: str) -> str | None:
        """返回一句话摘要；所有模型都失败或结果为空则返回 None（调用方据此降级）。"""
        if not self.ready:
            return None
        body = (body or "").strip()[: self.max_body]
        if len(body) < 20:  # 正文太短，摘要没有信息增量
            return None
        for model in self.models:
            for attempt in range(self.retries):
                try:
                    if text := self._call(model, title, body):
                        return text
                    print(f"  [AI] {model} 返回空内容，换下一个")
                except Exception as e:
                    print(f"  [AI] {model} 第{attempt + 1}次失败: {str(e)[:100]}")
                    time.sleep(2 * (attempt + 1))  # 限流时退避
        return None
