
import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "tutorials.json"

API = "https://open.feishu.cn/open-apis"


def get_token():
    response = requests.post(
        "https://accounts.feishu.cn/oauth/v3/token",
        json={
            "grant_type": "client_credentials",
            "client_id": os.environ["FEISHU_APP_ID"],
            "client_secret": os.environ["FEISHU_APP_SECRET"],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code", 0) != 0 or not data.get("access_token"):
        raise RuntimeError(
            f"获取飞书凭证失败：{data.get('code')} "
            f"{data.get('error', data.get('msg', ''))}"
        )

    return data["access_token"]


def read_records(token):
    base = os.environ["FEISHU_BASE_TOKEN"]
    table = os.environ["FEISHU_TABLE_ID"]
    url = f"{API}/bitable/v1/apps/{base}/tables/{table}/records"

    records = []
    page_token = None

    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token

        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()

        if result.get("code") != 0:
            raise RuntimeError(
                f"飞书读取失败：{result.get('code')} "
                f"{result.get('msg')}"
            )

        data = result.get("data") or {}
        records.extend(data.get("items") or [])

        if not data.get("has_more"):
            break

        page_token = data.get("page_token")
        if not page_token:
            raise RuntimeError("飞书分页信息缺失")

    return records


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(to_text(item) for item in value)
    if isinstance(value, dict):
        for field in ("link", "text", "value", "name"):
            if value.get(field) is not None:
                return to_text(value[field])
        return ""
    return str(value)



def find_url(fields):
    """
    链接获取优先级：
    1. 正式字段：资料链接
    2. 公式字段：AI提取-链接
    3. 原始分享内容中的网址
    """

    for field in (
        "资料链接",
        "AI提取-链接",
        "原始分享内容"
    ):
        content = to_text(fields.get(field))

        match = re.search(
            r'https?://[^\s<>()\[\]{}"\'，。；！？、]+',
            content,
        )

        if match:
            return match.group(0).rstrip(".,;，。；")

    return ""


def get_platform(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme != "https":
        return None

    def matches(domain):
        return host == domain or host.endswith("." + domain)

    if matches("bilibili.com") or matches("b23.tv"):
        return "bilibili"

    if matches("youtube.com") or matches("youtu.be"):
        return "youtube"

    if (
        matches("xiaohongshu.com")
        or matches("xhslink.cn")
        or matches("xhslink.com")
    ):
        return "xiaohongshu"

    return None


def url_key(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")

    match = re.search(r"/video/(BV[a-zA-Z0-9]+)", path)
    if "bilibili.com" in host and match:
        return "bilibili:" + match.group(1).lower()

    if host == "youtu.be":
        return "youtube:" + path.strip("/")

    if "youtube.com" in host:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if video_id:
            return "youtube:" + video_id

    return host + path


def convert_record(record):
    fields = record.get("fields") or {}

    if to_text(fields.get("处理状态")).strip() != "已审核":
        return None

    category = to_text(fields.get("资料类型")).strip()
    if category not in ("视频教程", "图文教程"):
        return None

    url = find_url(fields)
    platform = get_platform(url)
    if not platform:
        return None

    title = to_text(fields.get("资料标题")).strip()
    summary = to_text(fields.get("内容简介")).strip()

    invalid = ("", "待核验", "无法核验", "待人工命名")

    if (
        title in invalid
        or summary in invalid
        or title.startswith("无法核验")
        or summary.startswith("无法核验")
    ):
        return None

    difficulty = {
        "零基础": "beginner",
        "初级实操": "intermediate",
        "进阶项目": "advanced",
    }.get(to_text(fields.get("学习难度")).strip(), "unknown")

    return {
        "id": "feishu-" + record["record_id"],
        "platform": platform,
        "title": title,
        "creator": to_text(fields.get("作者/博主")),
        "url": url,
        "difficulty": difficulty,
        "topic": to_text(fields.get("学习方向")),
        "language": "zh" if platform != "youtube" else "unknown",
        "summary": summary,
        "recommended_sections": "",
        "learning_value": to_text(fields.get("推荐理由")),
        "verified": True,
        "source": "feishu",
    }


def main(dry_run):
    print("开始读取飞书学习资料库")

    records = read_records(get_token())
    print(f"飞书记录总数：{len(records)}")

    if not records:
        raise RuntimeError("飞书表格为空，为安全起见不覆盖网站")

    if not any(
        "处理状态" in (record.get("fields") or {})
        for record in records
    ):
        raise RuntimeError("未找到处理状态字段，请检查表格配置")

    approved = []
    for record in sorted(
        records, key=lambda r: r.get("record_id", "")
    ):
        item = convert_record(record)
        if item:
            approved.append(item)

    print(f"符合发布条件的教程：{len(approved)}")

    existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
    if not isinstance(existing.get("items"), list):
        raise RuntimeError("tutorials.json 格式不正确")

    # 保留原有人工精选教程。
    # 每次重新生成飞书内容，支持撤销审核后下架。
    manual = [
        item for item in existing["items"]
        if item.get("source") != "feishu"
    ]

    result = []
    seen = set()

    for item in manual + approved:
        key = url_key(item.get("url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)

    output_text = json.dumps(
        {"items": result},
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    print(f"最终网站教程总数：{len(result)}")

    if dry_run:
        print("预览模式：未修改 tutorials.json")
        for item in approved[:5]:
            print("拟发布：", item["title"])
        return

    if OUTPUT.read_text(encoding="utf-8") != output_text:
        OUTPUT.write_text(output_text, encoding="utf-8")
        print("tutorials.json 已更新")
    else:
        print("数据无变化，无需更新")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    main(args.dry_run)
