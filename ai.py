"""AI 分析：把通知概括成一句话，并抽取各类时间节点。

时间节点分两类语气：
- 催办类（deadline 截止 / signup 报名截止）——必须先完成某事，推送用"还剩 X 天"催办；
- 中性类（start 开始 / event 活动举行 / exam 考试）——某件事届时发生，用"X 天后"描述，不催办。

默认走 freeshare 中转（OpenAI 兼容端点，key 用环境变量 FREESHARE_API_KEY 引用）。
该中转站偶发 RPM/TPM 限流与空回复，故支持多模型回退 + 重试。

设计原则：任何失败都返回 None，由调用方降级为"不带摘要"推送，绝不阻塞通知。
"""
import json
import os
import re
import time

import requests

DEFAULT_BASE_URL = "https://freeshare.cc.cd/v1"
# 顺序按 GitHub Actions（美国网络）实测稳定性排：deepseek 最稳，kimi/glm 常整模型 500/503
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_FALLBACKS = ["kimi-k3", "glm-5.3-flash"]

# 时间节点类型白名单：催办类 3 个 + 中性类 2 个
URGENT_TYPES = ("deadline", "signup")
TIMING_TYPES = ("start", "deadline", "signup", "event", "exam")

# 注意：提示词里含大量 JSON 花括号，不能用 str.format（会把 {} 当占位符），改用 replace
SYSTEM_PROMPT = (
    "你是校园通知分析助手。请阅读学校通知，输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：\n"
    "{\n"
    '  "summary": "不超过 __LIMIT__ 个汉字的一句话概括，写清「谁、要做什么」",\n'
    '  "kind": "通知类型，只能取其一：action（要学生做事，如选课/报名/注册/申报/缴费/提交材料/投票）、'
    'event（活动，如比赛/运动会/晚会/典礼/讲座/展览）、exam（考试）、news（纯新闻或成果报道，无行动、无时间节点）",\n'
    '  "timings": [\n'
    '    {"type": "start",    "time": "行动/报名开始时间，如 2026-09-17 09:00；没有省略此项"},\n'
    '    {"type": "deadline", "time": "行动截止时间（选课截止、报名截止、提交截止、缴费截止等）"},\n'
    '    {"type": "signup",   "time": "活动类的报名/参与截止时间（若与 deadline 重复则只留 deadline）"},\n'
    '    {"type": "event",    "time": "活动举行时间", "end": "活动结束时间（仅跨天活动才填，单日省略）"},\n'
    '    {"type": "exam",     "time": "考试开始时间"}\n'
    "  ]\n"
    "}\n"
    "规则：\n"
    "1. timings 里每一项都必须是通知中明确写出、或据其规定完全可以确定的时间，禁止编造、推测；"
    "没有时间节点就输出空数组 []。\n"
    "2. 严格区分：截止时间（deadline/signup）是「必须在此前完成某动作」；"
    "活动、考试时间是「届时发生」，绝不要把活动举行时间写成 deadline。\n"
    "3. 同一类型可以有多个时间节点（如初赛和决赛各一条 event），分别列出。\n"
    "4. 时间格式一律 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；只写月日未写年份时，"
    "以通知发布日期（__PUBLISH__）的年份为准。\n"
    "5. summary 里不要罗列时间，时间由 timings 承载。"
)


def build_prompt(limit: int, publish: str) -> str:
    return SYSTEM_PROMPT.replace("__LIMIT__", str(limit)).replace("__PUBLISH__", publish or "未知")


# 归一化后的时间形态：YYYY-MM-DD 或 YYYY-MM-DD HH:MM
TIME_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$")


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


def normalize_time(value) -> str:
    """把模型给的时间归一化成 YYYY-MM-DD[ HH:MM]；无法识别返回空串。"""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    v = v.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    m = TIME_RE.match(v.replace("T", " ")) or re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", v)
    if not m:
        return ""
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    out = f"{year:04d}-{month:02d}-{day:02d}"
    if m.group(4) is not None:
        hour, minute = int(m.group(4)), int(m.group(5))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out += f" {hour:02d}:{minute:02d}"
    return out


def normalize_timings(value) -> list[dict]:
    """校验并归一化模型返回的 timings；丢弃类型非法或时间无法解析的项。"""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for t in value:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type") or "").strip().lower()
        if ttype not in TIMING_TYPES:
            continue
        time = normalize_time(t.get("time"))
        if not time:
            continue
        item = {"type": ttype, "time": time}
        if end := normalize_time(t.get("end")):
            if end > time:  # 结束早于开始说明模型给反了，丢掉
                item["end"] = end
        key = (ttype, time, item.get("end"))
        if key in seen:  # 只去完全重复的项，保留同类不同时间的（如初赛+决赛）
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_json(text: str) -> dict | None:
    """从模型回复里抠出 JSON 对象；容忍代码块包裹与前后废话。"""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


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

    def _call(self, model: str, title: str, body: str, publish_date: str) -> dict | None:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": build_prompt(self.max_summary, publish_date)},
                {"role": "user", "content": f"标题：{title}\n发布日期：{publish_date or '未知'}\n\n正文：{body}"},
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
        raw = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = _parse_json(raw)
        if data is None:
            # 模型没按 JSON 输出：退化为纯摘要，时间留空
            return {"summary": _clean(raw, self.max_summary), "kind": "", "timings": []}
        summary = _clean(str(data.get("summary") or ""), self.max_summary)
        if not summary:
            return None
        kind = str(data.get("kind") or "").strip().lower()
        return {
            "summary": summary,
            "kind": kind if kind in ("action", "event", "exam", "news") else "",
            "timings": normalize_timings(data.get("timings")),
        }

    def analyze(self, title: str, body: str, publish_date: str = "") -> dict | None:
        """返回 {"summary","kind","timings"}；所有模型都失败则返回 None（调用方降级）。"""
        if not self.ready:
            return None
        body = (body or "").strip()[: self.max_body]
        if len(body) < 20:  # 正文太短，摘要没有信息增量
            return None
        for model in self.models:
            for attempt in range(self.retries):
                try:
                    if result := self._call(model, title, body, publish_date):
                        return result
                    print(f"  [AI] {model} 返回空内容，换下一个")
                    break
                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else 0
                    if code == 429 and attempt + 1 < self.retries:
                        print(f"  [AI] {model} 限流，退避重试")
                        time.sleep(3 * (attempt + 1))
                        continue
                    print(f"  [AI] {model} HTTP {code}，换模型")
                    break
                except Exception as e:
                    print(f"  [AI] {model} 失败（{type(e).__name__}），换模型")
                    break
        return None
