"""远行商人数据获取模块 — 纯逻辑，无 UI 依赖。"""

import json
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

# API 地址从 .env 读取，不硬编码（.env 不上传到 GitHub）
API_URL = _env.get("MERCHANT_API_URL", "")
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

def _fetch_json(url: str) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "merchant-tray/1.0",
        },
    )
    with urlopen(req, timeout=20) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def fetch_merchant_data() -> dict:
    """从 API 获取商品数据（只保留当前轮次商品）。失败抛 RuntimeError。"""
    if not API_URL:
        raise RuntimeError("未配置 MERCHANT_API_URL，请在 .env 中填写")
    data = _fetch_json(API_URL)
    items = _extract_current_items(data)
    filtered = {
        "fetchedAt": data.get("fetchedAt", ""),
        "status": data.get("status", ""),
        "round": data.get("round"),
        "startedAtBeijing": data.get("startedAtBeijing", ""),
        "nextRefreshBeijing": data.get("nextRefreshBeijing", ""),
        "items": items,
    }
    filtered["_local"] = {
        "savedAtBeijing": beijing_stamp(now_beijing()),
    }
    return filtered


def _extract_current_items(data: dict) -> list[dict]:
    """提取当前轮次的商品列表，不包含其他轮次。"""
    if isinstance(data.get("items"), list):
        return data["items"]
    rnd = data.get("round")
    rounds = data.get("rounds") or {}
    items = rounds.get(str(rnd)) or rounds.get(rnd) or []
    return items if isinstance(items, list) else []


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
