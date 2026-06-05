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
# 抓取时间点（北京时间）
FETCH_TIMES = ((8, 30), (12, 30), (16, 30), (20, 30))
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
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


def _parse_onclick_args(onclick: str) -> list[str] | None:
    """
    解析 showShopinfo('img','name','cat','desc') 中的四个单引号字符串参数。
    正确处理 JS 的 \\' 转义。
    """
    m = re.search(r"showShopinfo\((.+)\)", onclick, re.DOTALL)
    if not m:
        return None
    raw = m.group(1).strip()
    args: list[str] = []
    i = 0
    while len(args) < 4:
        # 找到下一个单引号开头
        q = raw.find("'", i)
        if q == -1:
            break
        i = q + 1
        chars: list[str] = []
        while i < len(raw):
            c = raw[i]
            if c == "\\":
                # 转义字符：取下一个字符
                i += 1
                if i < len(raw):
                    chars.append(raw[i])
                i += 1
            elif c == "'":
                # 字符串结束
                i += 1
                break
            else:
                chars.append(c)
                i += 1
        args.append("".join(chars))
        # 跳过 ', ' 分隔符
        while i < len(raw) and raw[i] in ", ":
            i += 1
    return args if args else None


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
        dtime_m = re.search(r'data-time="(\d+)"', tag_attrs)
        # onclick 值用双引号包裹，内部含有单引号——不能用 [^"']*
        onclick_m = re.search(r'onclick="((?:[^"\\]|\\.)*)"', tag_attrs)

        classes = class_m.group(1) if class_m else ""
        style = style_m.group(1) if style_m else ""
        end_ts = int(dtime_m.group(1)) if dtime_m else 0
        onclick_raw = onclick_m.group(1) if onclick_m else ""

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

        # ── 从 onclick 提取分类和描述 ──
        category = ""
        description = ""
        args = _parse_onclick_args(onclick_raw)
        if args:
            if not img_url and len(args) > 0:
                img_url = args[0]
            if len(args) > 2:
                category = args[2]
            if len(args) > 3:
                description = args[3]

        # 将 "48w" → "48w" 原样保留；"1000" → "1000"
        items.append({
            "name": name,
            "price": price_raw,
            "priceRaw": price_raw,
            "limit": limit,
            "image": img_url,
            "category": category,
            "description": description,
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


def price_text(item: dict) -> str:
    raw = str(item.get("priceRaw") or "").strip()
    price = str(item.get("price") or "").strip()
    return raw or price or "-"


# ── 消息构建 ──────────────────────────────────────────────────

def build_short_summary(data: dict, watchlist: set[str] | None = None) -> str:
    """用于托盘通知的简短摘要。"""
    items = active_items(data)
    rnd = data.get("round") or "-"
    watchlist = watchlist or set()

    lines = [f"第 {rnd} 轮"]

    if not items:
        lines.append("当前无商品")
    else:
        for it in items:
            name = it.get("name") or "未命名"
            p = price_text(it)
            lim = it.get("limit") or "-"
            prefix = "★ " if name in watchlist else ""
            lines.append(f"{prefix}{name}  {p}洛克贝  限购{lim}")

    return "\n".join(lines)


# ── 定时 ──────────────────────────────────────────────────────

def next_fetch_time(dt: datetime | None = None) -> datetime:
    """返回下一个抓取时间点（北京时间 08:30 / 12:30 / 16:30 / 20:30）。"""
    dt = dt or now_beijing()
    for h, m in FETCH_TIMES:
        cand = datetime.combine(dt.date(), dtime(h, m), tzinfo=BEIJING_TZ)
        if cand > dt:
            return cand
    # 今天已过，推到明天 08:30
    tomorrow = dt.date() + timedelta(days=1)
    return datetime.combine(tomorrow, dtime(8, 30), tzinfo=BEIJING_TZ)
