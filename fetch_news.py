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
    "agent": ["agent", "agents", "agentic", "tool use", "workflow", "automation", "enterprise"],
    "model": ["model", "llm", "gemini", "gpt", "claude", "qwen", "deepseek", "reasoning", "benchmark"],
    "apps": ["app", "product", "launch", "assistant", "customer", "healthcare", "finance", "education", "video", "browser", "workspace"],
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
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc.lower(),
                       parts.path.rstrip("/"), urlencode(query), ""))


def classify(text):
    lower = text.lower()
    scores = {category: sum(1 for word in words if word in lower)
              for category, words in KEYWORDS.items()}
    if max(scores.values()) == 0:
        return "apps"
    return max(scores, key=scores.get)


def item_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def parsed_time(entry):
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct_time:
        return None
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
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
            item = make_item(entry.get("title", ""), entry.get("link", ""),
                             entry.get("summary", entry.get("description", "")),
                             source, date)
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
            params={"q": query, "sort": "updated", "order": "desc", "per_page": 20},
            headers={"Accept": "application/vnd.github+json"}, timeout=20,
        )
        response.raise_for_status()
        for repo in response.json().get("items", []):
            # GitHub pushed_at 代表最近代码更新，不是项目新闻发布日期。
            created = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
            updated = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
            if updated < CUTOFF:
                continue
            url = repo["html_url"]
            items.append({
                "id": item_id(url),
                "category": "github",
                "title": repo["full_name"],
                "summary": clean_text(repo.get("description") or "暂无项目描述")[:700],
                "source": "GitHub",
                "date": updated.isoformat(),
                "url": url,
                "change": "", "innovation": "", "practice": "", "insight": "",
                "stars": repo.get("stargazers_count", 0),
                "created_at": created.isoformat(),
                "date_type": "代码最近推送时间",
            })
        print(f"[成功] GitHub: {len(items)} 条近期候选")
    except (requests.RequestException, ValueError, KeyError) as error:
        print(f"[失败] GitHub: {error}")
    return items


def parse_news_date(item):
    """解析新闻实际日期。缺失或错误的旧记录不冒充近期资讯。"""
    try:
        date = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def recent_items(items):
    """只保留滚动 7 天窗口内且拥有有效 URL、日期的记录。"""
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        date = parse_news_date(item)
        if date is None or not (CUTOFF <= date <= NOW + timedelta(hours=24)):
            continue
        url = clean_url(item.get("url", ""))
        if not url:
            continue
        item = {**item, "url": url, "id": item_id(url)}
        result.append(item)
    return result


def merge_weekly(existing_items, daily_items):
    """旧近 7 日资讯 + 每日精选；URL 去重，新的记录优先。"""
    merged = {}
    for item in recent_items(existing_items):
        merged[item["url"]] = item
    for item in recent_items(daily_items):
        merged[item["url"]] = item
    return sorted(merged.values(), key=lambda x: parse_news_date(x), reverse=True)


def main():
    candidates = []
    for source, url in FEEDS.items():
        candidates.extend(fetch_rss(source, url))
    candidates.extend(fetch_github())

    # 当天每个栏目最多选两条、合计最多八条，保持原先的采集策略。
    unique = {item["url"]: item for item in candidates if item.get("url")}
    daily_picks = []
    for category in ("apps", "agent", "model", "github"):
        group = [item for item in unique.values() if item["category"] == category]
        group.sort(key=lambda x: x["date"], reverse=True)
        daily_picks.extend(group[:2])
    daily_picks.sort(key=lambda x: x["date"], reverse=True)
    daily_picks = daily_picks[:8]

    # 所有抓取源均失败时，保留历史 JSON，防止偶发故障清空页面。
    if not candidates:
        print("[警告] 没有采集到候选信息，保留原有 news.json")
        return

    previous = []
    if OUTPUT.exists():
        try:
            previous_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if not isinstance(previous_data, dict) or not isinstance(previous_data.get("items"), list):
                raise ValueError("news.json 缺少 items 数组")
            previous = previous_data["items"]
        except (ValueError, OSError) as error:
            # 不要在旧档案损坏时悄悄覆盖掉它，以免不可恢复。
            raise RuntimeError(f"现有 news.json 读取失败，请检查并恢复：{error}") from error

    weekly = merge_weekly(previous, daily_picks)
    data = {
        "generated_at": NOW.isoformat(),
        "count": len(weekly),
        "items": weekly,
    }
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[完成] 今日精选 {len(daily_picks)} 条，保留最近 7 天 {len(weekly)} 条到 {OUTPUT}")


if __name__ == "__main__":
    main()
