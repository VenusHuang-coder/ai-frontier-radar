
import json
import hashlib
import re
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import feedparser

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "news.json"
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=7)

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
}

KEYWORDS = {
    "agent": [
        "agent", "agents", "agentic", "tool use",
        "workflow", "automation", "enterprise",
    ],
    "model": [
        "model", "llm", "gemini", "gpt", "claude",
        "qwen", "deepseek", "reasoning", "benchmark",
    ],
    "apps": [
        "app", "product", "launch", "assistant",
        "customer", "healthcare", "finance", "education",
        "video", "browser", "workspace",
    ],
}

session = requests.Session()
session.headers.update({
    "User-Agent": "AI-Frontier-Radar/0.1 (personal research)",
    "Accept": "application/rss+xml, application/xml, application/json, */*",
})

def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def clean_url(value):
    parts = urlsplit(value or "")
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return ""

    query = [
        (k, v) for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_")
    ]
    return urlunsplit((
        parts.scheme, parts.netloc.lower(),
        parts.path.rstrip("/"), urlencode(query), ""
    ))

def classify(text):
    lower = text.lower()
    scores = {
        category: sum(
            1 for word in words if word in lower
        )
        for category, words in KEYWORDS.items()
    }
    if max(scores.values()) == 0:
        return "apps"
    return max(scores, key=scores.get)

def item_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]

def parsed_time(entry):
    struct_time = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )
    if not struct_time:
        return None
    try:
        return datetime(
            *struct_time[:6], tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None

def make_item(title, url, summary, source, date, category=None):
    url = clean_url(url)
    if not title or not url or not date:
        return None
    if date < CUTOFF or date > NOW + timedelta(hours=24):
        return None

    summary = clean_text(summary)[:700]
    return {
        "id": item_id(url),
        "category": category or classify(title + " " + summary),
        "title": clean_text(title),
        "summary": summary or "请访问官方来源查看详细内容。",
        "source": source,
        "date": date.isoformat(),
        "url": url,
        "change": "",
        "innovation": "",
        "practice": "",
        "insight": "",
    }

def fetch_rss(source, url):
    items = []
    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        feed = feedparser.parse(response.content)

        if not feed.entries:
            print(f"[提示] {source} 暂无可解析条目")
        for entry in feed.entries:
            date = parsed_time(entry)
            item = make_item(
                entry.get("title", ""),
                entry.get("link", ""),
                entry.get("summary", entry.get("description", "")),
                source,
                date,
            )
            if item:
                items.append(item)
        print(f"[成功] {source}: {len(items)} 条近期候选")

    except (requests.RequestException, ValueError) as error:
        print(f"[失败] {source}: {error}")

    return items

def fetch_github():
    items = []
    try:
        date_str = (NOW - timedelta(days=30)).date().isoformat()
        query = f"topic:ai-agents pushed:>={date_str} stars:>=30"

        response = session.get(
            "https://api.github.com/search/repositories",
            params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": 20,
            },
            headers={"Accept": "application/vnd.github+json"},
            timeout=20,
        )
        response.raise_for_status()

        for repo in response.json().get("items", []):
            # GitHub 的 pushed_at 是代码更新，不是发布日期。
            # 因此展示项目创建日期，不冒充新闻发布日期。
            created = datetime.fromisoformat(
                repo["created_at"].replace("Z", "+00:00")
            )
            updated = datetime.fromisoformat(
                repo["pushed_at"].replace("Z", "+00:00")
            )

            if updated < CUTOFF:
                continue

            url = repo["html_url"]
            items.append({
                "id": item_id(url),
                "category": "github",
                "title": repo["full_name"],
                "summary": clean_text(
                    repo.get("description") or "暂无项目描述"
                )[:700],
                "source": "GitHub",
                "date": updated.isoformat(),
                "url": url,
                "change": "",
                "innovation": "",
                "practice": "",
                "insight": "",
                "stars": repo.get("stargazers_count", 0),
                "created_at": created.isoformat(),
                "date_type": "代码最近推送时间",
            })
        print(f"[成功] GitHub: {len(items)} 条近期候选")

    except (requests.RequestException, ValueError, KeyError) as error:
        print(f"[失败] GitHub: {error}")

    return items

def main():
    candidates = []

    for source, url in FEEDS.items():
        candidates.extend(fetch_rss(source, url))

    candidates.extend(fetch_github())

    # 使用规范化后的 URL 去重
    unique = {}
    for item in candidates:
        unique[item["url"]] = item

    items = list(unique.values())

    # 初步平衡四个栏目，每类最多两条
    picked = []
    for category in ("apps", "agent", "model", "github"):
        group = [
            item for item in items
            if item["category"] == category
        ]
        group.sort(key=lambda x: x["date"], reverse=True)
        picked.extend(group[:2])

    # 按时间倒序，最多 8 条
    picked.sort(key=lambda x: x["date"], reverse=True)
    picked = picked[:8]

    # 所有来源均失败时不覆盖上一次的有效数据
    if not candidates:
        print("[警告] 没有采集到候选信息，保留原有 news.json")
        return

    data = {
        "generated_at": NOW.isoformat(),
        "count": len(picked),
        "items": picked,
    }

    OUTPUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[完成] 已保存 {len(picked)} 条到 {OUTPUT}")

if __name__ == "__main__":
    main()
