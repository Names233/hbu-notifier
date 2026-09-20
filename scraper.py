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
DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


@dataclass
class Item:
    url: str
    title: str
    date: str
    source: str


def _to_abs(href: str, page_url: str, base_url: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return urljoin(base_url or page_url, href)
    return urljoin(page_url, href)


def _find_date(a) -> str:
    """沿父级向上找日期，但一旦容器里的链接多于 3 个（说明已上升到整个列表）就放弃。"""
    node = a
    for _ in range(4):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        if len(node.find_all("a")) > 3:
            break
        m = DATE_RE.search(node.get_text(" ", strip=True))
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def fetch_list(url: str, base_url: str, source_name: str, timeout: int = 20) -> list[Item]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    # 这些老站点可能不声明编码，requests 会猜成 latin-1，需按内容重猜
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    found: dict[str, Item] = {}
    for a in soup.find_all("a", href=INFO_RE):
        title = a.get_text(" ", strip=True) or (a.get("title") or "").strip()
        title = title.strip()
        if len(title) < 4:  # 过滤图片锚点、导航残留
            continue
        full = _to_abs(a["href"].strip(), url, base_url)
        old = found.get(full)
        if old is None or len(title) > len(old.title):
            found[full] = Item(full, title, _find_date(a), source_name)
    return list(found.values())
