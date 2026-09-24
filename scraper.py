"""列表页解析：博达 CMS 类站点通用抓取。

思路：页面里所有 href 形如 info/栏目ID/文章ID.htm 的 <a> 即文章链接，
标题取 <a> 文本（或 title 属性），日期从最近的单条目容器文本里用正则提取。
"""
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 hbu-notifier/1.0",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
INFO_RE = re.compile(r"info/\d+/\d+\.s?html?", re.I)
# 标准写法：2026-09-23 / 2026年9月23日
DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
# 该 CMS 部分模板把日期拆成「09-23」+「2026」两段，需单独识别
DATE_MD_Y_RE = re.compile(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\D{0,4}(20\d{2})")


def parse_date(text: str) -> str:
    """从文本里解析日期，返回 YYYY-MM-DD 或空串。"""
    m = DATE_RE.search(text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = DATE_MD_Y_RE.search(text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


@dataclass
class Item:
    url: str
    title: str
    date: str
    source: str
    summary: str = ""
    start: str = ""
    deadline: str = ""


def _to_abs(href: str, page_url: str, base_url: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return urljoin(base_url or page_url, href)
    return urljoin(page_url, href)


def _find_date(a) -> str:
    """先找条目内的日期元素（.date 等），再沿父级向上找文本里的日期。"""
    for cls in ("date", "time", "rq"):
        el = a.find(class_=re.compile(rf"\b{cls}\b"))
        if el and (d := parse_date(el.get_text(" ", strip=True))):
            return d

    node = a
    for _ in range(4):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        if len(node.find_all("a")) > 3:
            break
        if d := parse_date(node.get_text(" ", strip=True)):
            return d
    return ""


# 标题元素类名：不少列表页把日期/摘要和标题一起塞进 <a>，需要单独取标题
TITLE_CLASSES = ("tit", "title", "bt", "mc", "name")
MAX_TITLE_LEN = 80  # 超过此长度视为误把正文当标题


def _in_nav(a) -> bool:
    """锚点是否位于导航/页眉/页脚/侧栏内——那些是栏目入口或推荐位，而非当前列表条目。"""
    for p in a.parents:
        if p.name in ("nav", "header", "footer", "aside"):
            return True
        cls = " ".join(p.get("class") or [])
        if re.search(r"\b(nav|head|header|footer|crumb|menu|hot|sidebar|side-bar|tuijian)\b", cls):
            return True
    return False


def _find_title(a) -> str:
    """按优先级取干净标题：title 属性 > 标题类元素 > 锚点文本（截断过长者）。"""
    attr = (a.get("title") or "").strip()
    if len(attr) >= 4:
        return attr

    for cls in TITLE_CLASSES:
        el = a.find(class_=re.compile(rf"\b{cls}\b"))
        if el:
            text = el.get_text(" ", strip=True)
            if 4 <= len(text) <= MAX_TITLE_LEN:
                return text

    text = a.get_text(" ", strip=True)
    if len(text) > MAX_TITLE_LEN:
        # 锚点把日期/摘要也包进来了：取第一个较长文本块当标题
        for el in a.find_all(["div", "span", "p"]):
            t = el.get_text(" ", strip=True)
            if 6 <= len(t) <= MAX_TITLE_LEN:
                return t
        text = text[:MAX_TITLE_LEN]
    return text


def fetch_list(url: str, base_url: str, source_name: str, timeout: int = 20) -> list[Item]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    # 这些老站点可能不声明编码，requests 会猜成 latin-1，需按内容重猜
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    found: dict[str, Item] = {}
    for a in soup.find_all("a", href=INFO_RE):
        if _in_nav(a):
            continue
        title = _find_title(a).strip()
        if len(title) < 4:  # 过滤图片锚点、导航残留
            continue
        full = _to_abs(a["href"].strip(), url, base_url)
        old = found.get(full)
        if old is None or len(title) > len(old.title):
            found[full] = Item(full, title, _find_date(a), source_name)
    return list(found.values())


# 正文容器：博达 CMS 常见的几种类名/ID，按优先级尝试
CONTENT_SELECTORS = [
    ("div", {"class": "v_news_content"}),
    ("div", {"id": "vsb_content"}),
    ("div", {"class": "ny-cont"}),
    ("div", {"id": re.compile(r"^vsb_content")}),
    ("div", {"class": "art-body"}),
    ("div", {"class": "content"}),
    ("article", {}),
]

# 正文里需要剔除的噪声块。注意只用绝不会包裹正文的标签——
# 这些老 CMS 常把正文放在 <form> 里，所以 form/nav/header/footer 都不能删。
NOISE_TAGS = ("script", "style", "noscript", "iframe")


def fetch_article_body(url: str, timeout: int = 20, max_len: int = 4000) -> str:
    """抓详情页正文纯文本，供 AI 摘要。失败返回空字符串，不影响主流程。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return ""
    for tag in soup(NOISE_TAGS):
        tag.decompose()

    for name, attrs in CONTENT_SELECTORS:
        el = soup.find(name, attrs)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) >= 20:
                return text[:max_len]
    return ""
