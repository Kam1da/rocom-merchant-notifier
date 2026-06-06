"""远行商人数据获取模块 — 纯逻辑，无 UI 依赖。"""

import json
import re
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# ── 常量 ──────────────────────────────────────────────────────

def _load_env_config() -> dict[str, str]:
    """从项目根目录 .env 文件读取配置（仅支持 KEY=VALUE 格式）。"""
    if getattr(sys, "frozen", False):
        env_path = Path(sys.executable).parent / ".env"
    else:
        env_path = Path(__file__).resolve().parents[2] / ".env"
    config: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip()
    return config

_env = _load_env_config()

# 数据源地址从 .env 读取（不硬编码真实地址）
SOURCE_URL = _env.get("MERCHANT_API_URL", "")
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# 打包后数据文件放在 exe 同级目录；源码运行时放在项目根目录 data/ 下。
if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).parent
else:
    APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = APP_ROOT / "data" / "latest.json"

# ── 时间工具 ──────────────────────────────────────────────────

def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── 网络请求 ──────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _fetch_html(url: str) -> str:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=20) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset)


# ── HTML 解析 ─────────────────────────────────────────────────

def _parse_js_var(html: str, name: str, type_: str = "int") -> int | list | str | None:
    """从页面 <script> 中提取 var name = value; 形式的变量。"""
    if type_ == "int":
        m = re.search(rf"\bvar\s+{name}\s*=\s*(\d+)\s*;", html)
        return int(m.group(1)) if m else None
    if type_ == "array":
        m = re.search(rf"\bvar\s+{name}\s*=\s*\[([\d,\s]+)\]", html)
        return [int(x) for x in m.group(1).split(",")] if m else None
    if type_ == "str":
        m = re.search(rf"\bvar\s+{name}\s*=\s*'([^']*)'\s*;", html)
        return m.group(1) if m else None
    return None


def _parse_merchant_html(html: str) -> dict:
    """从远行商人查询器页面 HTML 中解析商品数据。返回与旧 API 兼容的 dict 结构。"""
    # ── JS 变量 ──
    index = _parse_js_var(html, "index", "int") or 0
    server_now = _parse_js_var(html, "serverNow", "int") or 0
    refresh_hour = _parse_js_var(html, "refreshHour", "array") or [0, 8, 12, 16, 20]

    # ── 遍历 <li> 提取当前时间段商品 ──
    items: list[dict] = []
    for li_match in re.finditer(
        r"<li\b([^>]*)>(.*?)</li>",
        html,
        re.DOTALL,
    ):
        tag_attrs = li_match.group(1)
        body = li_match.group(2)

        # 提取关键属性
        class_m = re.search(r'class="([^"]*)"', tag_attrs)
        style_m = re.search(r'style="([^"]*)"', tag_attrs)

        classes = class_m.group(1) if class_m else ""
        style = style_m.group(1) if style_m else ""

        # 只处理当前时间段且可见的商品
        if "display:none" in style.replace(" ", ""):
            continue
        show_class = f"show_{index}"
        if show_class not in classes:
            continue

        # ── 从 HTML 标签提取基本字段 ──
        name_m = re.search(r'class=["\']shop_name["\'][^>]*>([^<]+)', body)
        price_m = re.search(r'class=["\']shop_price["\'][^>]*>价格：([^<]+)', body)
        limit_m = re.search(r"<em>限购([^<]+)</em>", body)
        img_m = re.search(r'<img\s+src="([^"]+)"', body)

        name = name_m.group(1).strip() if name_m else "未命名"
        price_raw = price_m.group(1).strip() if price_m else ""
        limit = limit_m.group(1).strip() if limit_m else ""
        img_url = img_m.group(1) if img_m else ""
        if img_url.startswith("//"):
            img_url = "https:" + img_url

        items.append({
            "name": name,
            "price": price_raw,
            "limit": limit,
            "image": img_url,
        })

    # ── 时间计算 ──
    server_utc = datetime.fromtimestamp(server_now, tz=timezone.utc)
    server_beijing = server_utc.astimezone(BEIJING_TZ)

    # 当前轮次起始时间 = refreshHour[index] 对应的小时
    current_start_h = refresh_hour[index] if index < len(refresh_hour) else 0
    started = server_beijing.replace(
        hour=current_start_h, minute=0, second=0, microsecond=0
    )

    # 下一刷新时间
    next_idx = index + 1
    if next_idx < len(refresh_hour):
        next_h = refresh_hour[next_idx]
        next_refresh = server_beijing.replace(
            hour=next_h, minute=0, second=0, microsecond=0
        )
    else:
        # 下一个是次日 0:00
        next_refresh = (
            server_beijing.date() + timedelta(days=1)
        )
        next_refresh = datetime.combine(
            next_refresh, dtime(0, 0), tzinfo=BEIJING_TZ
        )

    return {
        "fetchedAt": server_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "status": "open",
        "round": index,
        "startedAtBeijing": beijing_stamp(started),
        "nextRefreshBeijing": beijing_stamp(next_refresh),
        "items": items,
    }


def fetch_merchant_data() -> dict:
    """抓取并解析商品数据。失败抛 RuntimeError。"""
    if not SOURCE_URL:
        raise RuntimeError("未配置 MERCHANT_API_URL，请在 .env 中填写")
    html = _fetch_html(SOURCE_URL)
    data = _parse_merchant_html(html)
    if not data["items"]:
        raise RuntimeError("页面解析未找到商品，可能页面结构已变更")
    data["_local"] = {
        "savedAtBeijing": beijing_stamp(now_beijing()),
    }
    return data


# ── 缓存 ──────────────────────────────────────────────────────

def save_latest(data: dict, path: Path = DEFAULT_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_latest(path: Path = DEFAULT_CACHE) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── 商品解析 ──────────────────────────────────────────────────

def active_items(data: dict) -> list[dict]:
    items = data.get("items")
    return items if isinstance(items, list) else []

