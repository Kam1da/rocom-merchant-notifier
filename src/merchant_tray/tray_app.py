"""远行商人托盘程序 — 入口。

启动后常驻系统托盘，在 8:30/12:30/16:30/20:30 抓取远行商人商品数据，
无关注命中时每小时 :30 提醒，有关注命中时每 10 分钟提醒。
"""

import io
import hashlib
import os
import re as _re
import shutil
import subprocess
import sys
import threading
import time as time_module
from datetime import datetime, time as dtime, timedelta
from xml.sax.saxutils import escape as xml_escape

# Windows 控制台 UTF-8 输出（调试用，打包为 exe 后无影响）
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
from urllib.request import Request as UrlRequest, urlopen as url_urlopen

# ── 依赖 ──────────────────────────────────────────────────────
from PIL import Image, ImageDraw, ImageTk
import pystray

from .merchant import (
    APP_ROOT,
    USER_AGENT,
    active_items,
    beijing_stamp,
    fetch_merchant_data,
    load_latest,
    now_beijing,
    save_latest,
)

# ── 常量 ──────────────────────────────────────────────────────
# 应用目录（打包/源码两种场景）由 merchant.APP_ROOT 统一计算。
APP_DIR = APP_ROOT
ASSETS_DIR = APP_DIR / "assets"
DATA_DIR = APP_DIR / "data"
ICON_PATH = ASSETS_DIR / "icon.png"
ICON_ICO_PATH = ASSETS_DIR / "icon.ico"
CACHE_FILE = DATA_DIR / "latest.json"
IMAGES_DIR = DATA_DIR / "images"
WATCHLIST_FILE = DATA_DIR / "watchlist.txt"
APP_TITLE = "远行商人"

# 抓取时间点（北京时间）
FETCH_SCHEDULE = [(8, 30), (12, 30), (16, 30), (20, 30)]
# 关注提醒间隔（分钟）
REMINDER_INTERVAL_NORMAL = 60   # 无关注命中时，每小时 :30 提醒
REMINDER_INTERVAL_HIT = 10      # 有关注命中时，每 10 分钟提醒
# 连续抓取失败多少次后弹窗告警
MAX_SILENT_FAILURES = 3


def build_names_summary(data: dict, watchlist: set[str] | None = None) -> str:
    """构建仅含商品名的通知摘要，两列对齐显示。"""
    items = active_items(data)
    if not items:
        return "当前无商品"

    watchlist = watchlist or set()
    names = []
    for it in items:
        name = it.get("name") or "未命名"
        prefix = "【★】" if name in watchlist else ""
        names.append(f"{prefix}{name}")

    # 两列对齐：每行放两个名称，用空格补齐左列宽度
    COL_WIDTH = 8  # 中文字符宽度（全角空格对齐）
    lines = []
    for i in range(0, len(names), 2):
        left = names[i]
        if i + 1 < len(names):
            right = names[i + 1]
            # 计算左列显示宽度（中文字符占 2 个单位）
            left_width = sum(2 if ord(ch) > 127 else 1 for ch in left)
            pad = max(2, (COL_WIDTH - left_width) * 2 + 4)
            lines.append(f"{left}{' ' * pad}{right}")
        else:
            lines.append(left)

    return "\n".join(lines)


# ── 通知 ──────────────────────────────────────────────────────

def _xml_text(value: str) -> str:
    return xml_escape(str(value), {'"': "&quot;", "'": "&apos;"})


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _toast_tag(title: str, message: str) -> str:
    digest = hashlib.sha1(f"{title}\0{message}".encode("utf-8")).hexdigest()[:16]
    return f"merchant-{digest}"


def _hidden_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def _run_powershell(command: str) -> None:
    subprocess.Popen(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=_hidden_startupinfo(),
    )


def _show_windows_toast(title: str, message: str) -> None:
    title_xml = _xml_text(title)
    message_xml = _xml_text(message)
    binding = f"""
        <binding template="ToastText02">
            <text id="1">{title_xml}</text>
            <text id="2">{message_xml}</text>
        </binding>
    """

    xml = f"""
<toast duration="short">
    <visual>
        {binding}
    </visual>
    <audio silent="true" />
</toast>
"""

    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$Template = @'
{xml}
'@
$SerializedXml = New-Object Windows.Data.Xml.Dom.XmlDocument
$SerializedXml.LoadXml($Template)
$Toast = [Windows.UI.Notifications.ToastNotification]::new($SerializedXml)
$Toast.Tag = {_ps_quote(_toast_tag(title, message))}
$Toast.Group = {_ps_quote(APP_TITLE)}
$Notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(APP_TITLE)})
$Notifier.Show($Toast)
"""
    _run_powershell(script)


def _notify(title: str, message: str) -> None:
    """发送 Windows toast 通知，失败时静默降级到 print。"""
    try:
        if sys.platform != "win32":
            raise RuntimeError("Windows toast only")
        _show_windows_toast(title, message)
    except Exception as exc:
        print(f"[通知降级] {exc}")
        print(f"{title}\n{message}")


# ── 图标 ──────────────────────────────────────────────────────

def _generate_icon_image() -> Image.Image:
    # Minimal fallback that keeps the crystal/globe identity if icon.png is missing.
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.ellipse([4, 4, 60, 61], fill=(68, 226, 220, 255), outline=(245, 255, 210, 210), width=2)
    draw.polygon([(9, 28), (28, 8), (43, 16), (22, 36)], fill=(188, 255, 225, 130))
    draw.polygon([(20, 38), (35, 22), (55, 31), (37, 49)], fill=(31, 88, 235, 150))
    draw.polygon([(24, 22), (36, 18), (46, 32), (32, 47), (18, 35)], fill=(232, 255, 248, 190), outline=(255, 255, 255, 130))
    draw.polygon([(32, 47), (46, 32), (40, 49)], fill=(235, 93, 226, 155))
    draw.polygon([(51, 28), (54, 33), (60, 34), (55, 38), (56, 44), (51, 40), (46, 44), (47, 38), (42, 34), (48, 33)], fill=(255, 239, 65, 220))
    draw.ellipse([39, 16, 42, 19], fill=(255, 255, 255, 180))
    return img


def _generate_placeholder_image(size: int = 60) -> Image.Image:
    """生成占位图（灰色背景 + 问号），用于图片加载失败时兜底。"""
    img = Image.new("RGBA", (size, size), (200, 200, 200, 255))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.text((size // 2 - 6, size // 2 - 10), "?", fill=(120, 120, 120, 255))
    return img


def _download_image(url: str, size: int = 60) -> Image.Image:
    """从 URL 下载图片并缩放到指定尺寸；失败时返回占位图。"""
    if not url:
        return _generate_placeholder_image(size)
    try:
        req = UrlRequest(url, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://patchwiki.biligame.com/",
        })
        with url_urlopen(req, timeout=10) as resp:
            img_data = resp.read()
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    except Exception as exc:
        print(f"[图片下载失败] {url}: {exc}")
        return _generate_placeholder_image(size)


def _sanitize_filename(name: str) -> str:
    """将商品名转为安全文件名：去除非法字符，截断过长名称。"""
    name = _re.sub(r'[\\/:*?"<>|]', '', name).strip()
    return name[:50] if name else "unnamed"


def _download_all_images(data: dict) -> None:
    """抓取成功后将所有商品图片下载到 IMAGES_DIR，覆盖旧文件。"""
    items = active_items(data)
    if not items:
        return
    try:
        # 清空旧图片目录
        if IMAGES_DIR.exists():
            shutil.rmtree(IMAGES_DIR)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        for i, item in enumerate(items):
            img_url = item.get("image", "")
            name = item.get("name") or f"item_{i}"
            filename = f"{_sanitize_filename(name)}.png"
            pil_img = _download_image(img_url, 60)
            pil_img.save(IMAGES_DIR / filename, "PNG")
            print(f"[图片保存] {filename}")
    except Exception as exc:
        print(f"[图片批量下载失败] {exc}")


def _load_local_image(item_name: str, size: int = 60) -> Image.Image:
    """从本地缓存加载商品图片；不存在时返回占位图。"""
    filename = f"{_sanitize_filename(item_name)}.png"
    img_path = IMAGES_DIR / filename
    if img_path.exists():
        try:
            return Image.open(img_path).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    return _generate_placeholder_image(size)


def _load_icon() -> Image.Image:
    if ICON_PATH.exists():
        try:
            return Image.open(ICON_PATH).convert("RGBA")
        except Exception:
            pass
    return _generate_icon_image()


def _set_tk_window_icon(window, tk_module) -> None:
    """尽量把应用图标放到 tkinter 标题栏左侧；失败时保持系统默认。"""
    if sys.platform == "win32" and ICON_ICO_PATH.exists():
        try:
            window.iconbitmap(str(ICON_ICO_PATH))
            return
        except Exception:
            pass
    if ICON_PATH.exists():
        try:
            window._merchant_icon = tk_module.PhotoImage(file=str(ICON_PATH))
            window.iconphoto(True, window._merchant_icon)
        except Exception:
            pass


def _center_and_show(root) -> None:
    """布局完成后将窗口移到屏幕中央并置顶聚焦。

    采用 off-screen 定位（窗口先创建在屏幕外，布局完再移入），
    避免 withdraw/deiconify 在部分系统上造成的残影。
    """
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sx = (root.winfo_screenwidth() - w) // 2
    sy = (root.winfo_screenheight() - h) // 2
    root.geometry(f"+{sx}+{sy}")
    root.lift()
    root.focus_force()


# ── 核心逻辑 ──────────────────────────────────────────────────

class MerchantTrayApp:
    def __init__(self):
        self._icon = None
        self._running = False
        self._watchlist = set()
        # ── 通知状态 ──
        self._current_round: int | None = None        # 当前轮次号
        self._fail_count: int = 0                     # 连续失败次数
        self._watchlist_hit_names: set[str] = set()   # 当前命中的关注商品
        self._watchlist_snoozed: bool = False          # 本轮是否已静音关注提醒
        self._last_reminder_ts: float = 0              # 上次提醒时间戳（避免与抓取通知重复）

    # ── 名单 ──────────────────────────────────────────────────

    def _load_watchlist(self) -> set[str]:
        """从 watchlist.txt 加载着重关注的商品名称（每行一个，# 开头为注释）。"""
        if not WATCHLIST_FILE.exists():
            return set()
        try:
            lines = WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
            return {
                line.strip() for line in lines
                if line.strip() and not line.strip().startswith("#")
            }
        except Exception:
            return set()

    def _save_watchlist(self) -> None:
        """将当前名单写入 watchlist.txt。"""
        try:
            WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            content = "# 远行商人关注名单\n# 每行一个商品名称\n\n"
            if self._watchlist:
                content += "\n".join(sorted(self._watchlist)) + "\n"
            WATCHLIST_FILE.write_text(content, encoding="utf-8")
        except Exception as exc:
            print(f"[名单保存失败] {exc}")

    # ── 数据抓取 + 通知 ──────────────────────────────────────

    def _do_fetch_and_process(self, notify: bool = True) -> None:
        """抓取数据并处理通知。notify=False 时静默抓取不弹窗。"""
        ts = beijing_stamp(now_beijing())
        try:
            data = fetch_merchant_data()
        except Exception as exc:
            # ── 抓取失败 ──
            self._fail_count += 1
            print(f"[抓取失败] 第 {self._fail_count} 次 @ {ts}: {exc}")
            if self._fail_count >= MAX_SILENT_FAILURES:
                _notify(
                    "抓取失败",
                    f"已连续失败 {self._fail_count} 次\n{exc}",
                )
            return

        # ── 抓取成功 ──
        self._fail_count = 0
        save_latest(data, CACHE_FILE)
        _download_all_images(data)

        items = active_items(data)
        new_round = data.get("round")
        print(f"[抓取成功] 轮次={new_round} @ {ts}")

        # ── 轮次变化：重置关注提醒状态 ──
        round_changed = (new_round != self._current_round)
        if round_changed:
            print(f"[轮次变化] {self._current_round} → {new_round}")
            self._current_round = new_round
            self._watchlist_hit_names.clear()
            self._watchlist_snoozed = False

        # 更新托盘提示
        self._update_tooltip()

        # ── 关注名单命中检测 ──
        if self._watchlist:
            current_names = {it.get("name", "") for it in items}
            self._watchlist_hit_names = current_names & self._watchlist

        # ── 通知 ──
        if notify:
            summary = build_names_summary(data, self._watchlist)
            if self._watchlist_hit_names and not self._watchlist_snoozed:
                hit_list = ", ".join(sorted(self._watchlist_hit_names))
                _notify(f"★ 关注商品在售：{hit_list}", summary)
                print(f"[关注命中] {hit_list}")
            else:
                _notify("当前出售商品", summary)
        # 重置提醒计时器，避免与提醒循环重复
        self._last_reminder_ts = now_beijing().timestamp()

    # ── 菜单回调 ──────────────────────────────────────────────

    def _on_manage_watchlist(self, icon, item):
        """打开 tkinter 弹窗管理关注名单。"""
        threading.Thread(target=self._show_watchlist_dialog, daemon=True).start()

    def _on_snooze_watchlist(self, icon, item):
        """本轮不再提醒关注商品。"""
        self._watchlist_snoozed = True
        self._watchlist_hit_names.clear()
        print("[静音] 本轮关注提醒已暂停")
        _notify("关注提醒已暂停", "本轮不再提醒关注商品。")

    def _on_show_products(self, icon, item):
        """打开"当前商品"弹窗（在新线程中运行 tkinter）。"""
        threading.Thread(target=self._show_products_dialog, daemon=True).start()

    def _show_products_dialog(self):
        """弹窗展示当前商品名和图片。"""
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            _notify("当前商品不可用", "tkinter 不可用，无法打开商品弹窗。")
            return

        data = load_latest(CACHE_FILE)
        items = active_items(data) if data else []

        root = tk.Tk()
        root.geometry("+9999+9999")  # 先放到屏幕外，布局完再移到中间
        root.title("当前出售商品")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        _set_tk_window_icon(root, tk)

        main_frame = ttk.Frame(root, padding=16)
        main_frame.pack(fill="both", expand=True)

        if not items:
            ttk.Label(
                main_frame,
                text="（暂无商品数据）",
                foreground="gray",
                font=("Microsoft YaHei UI", 11),
            ).pack(pady=20)
        else:
            # 引用列表，防止 PhotoImage 被垃圾回收
            _photo_refs = []

            # 配色
            HL_BG = "#FFF8DC"       # 关注商品背景（浅黄）
            HL_BORDER = "#DAA520"   # 关注商品边框（金色）
            NORMAL_BG = "#FFFFFF"    # 普通商品背景
            HL_TEXT = "#B8860B"     # 关注商品价格文字（暗金）

            for item in items:
                name = item.get("name", "未命名")
                is_watched = name in self._watchlist

                # ── 整行容器（关注商品带金色边框 + 浅黄底）──
                row = tk.Frame(
                    main_frame,
                    bg=HL_BG if is_watched else NORMAL_BG,
                    highlightbackground=HL_BORDER if is_watched else NORMAL_BG,
                    highlightthickness=2 if is_watched else 0,
                )
                row.pack(fill="x", pady=4, ipady=4)

                # ── 商品图片（从本地缓存读取）──
                pil_img = _load_local_image(name, 60)
                tk_img = ImageTk.PhotoImage(pil_img)
                _photo_refs.append(tk_img)

                img_label = tk.Label(
                    row, image=tk_img,
                    bg=HL_BG if is_watched else NORMAL_BG,
                )
                img_label.image = tk_img  # 防 GC
                img_label.pack(side="left", padx=(8, 12))

                # ── 商品信息 ──
                info_frame = tk.Frame(
                    row, bg=HL_BG if is_watched else NORMAL_BG,
                )
                info_frame.pack(side="left", fill="x", expand=True, padx=(0, 8))

                display_name = f"★ {name}" if is_watched else name
                name_label = tk.Label(
                    info_frame,
                    text=display_name,
                    font=("Microsoft YaHei UI", 11, "bold"),
                    fg=HL_TEXT if is_watched else "#000",
                    bg=HL_BG if is_watched else NORMAL_BG,
                )
                name_label.pack(anchor="w")

                price = item.get("price", "-")
                limit = item.get("limit", "-")
                detail = f"价格: {price} 洛克贝"
                if limit:
                    detail += f"  |  限购: {limit}"
                detail_label = tk.Label(
                    info_frame,
                    text=detail,
                    font=("Microsoft YaHei UI", 9),
                    fg=HL_TEXT if is_watched else "#666",
                    bg=HL_BG if is_watched else NORMAL_BG,
                )
                detail_label.pack(anchor="w")

        # 居中显示
        _center_and_show(root)

        # 点击窗口外部区域关闭
        root.bind("<FocusOut>", lambda e: root.destroy() if not root.focus_get() else None)

        root.mainloop()

    def _show_watchlist_dialog(self):
        """用 tkinter 展示当前商品列表，勾选添加到关注名单。"""
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
        except ImportError:
            _notify("名单管理不可用", "tkinter 不可用，无法打开名单管理。")
            return

        data = load_latest(CACHE_FILE)
        products = [it.get("name", "未命名") for it in active_items(data)] if data else []
        product_set = set(products)

        root = tk.Tk()
        root.geometry("+9999+9999")  # 先放到屏幕外，布局完再移到中间
        root.title("管理关注名单")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        _set_tk_window_icon(root, tk)

        main_frame = ttk.Frame(root, padding=16)
        main_frame.pack(fill="both", expand=True)

        check_vars = {}

        # ── 当前在售商品 ──
        ttk.Label(main_frame, text="当前在售商品（勾选即加入名单）：", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 4))

        # 用独立 frame 放商品勾选框，方便手动添加时追加
        product_frame = ttk.Frame(main_frame)
        product_frame.pack(fill="x", padx=8)

        if products:
            for name in products:
                var = tk.BooleanVar(value=name in self._watchlist)
                check_vars[name] = var
                cb = ttk.Checkbutton(product_frame, text=name, variable=var)
                cb.pack(anchor="w")
        else:
            ttk.Label(product_frame, text="（暂无商品数据）", foreground="gray").pack(anchor="w")

        # ── 已在名单但当前不在售的商品 ──
        saved_only = sorted(self._watchlist - product_set)
        # 懒初始化：手动添加时若此区尚不存在，按需创建
        saved_section_ref = {}

        def _ensure_saved_section():
            if "frame" in saved_section_ref:
                return
            ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)
            ttk.Label(main_frame, text="已关注但当前不在售（取消勾选即移除）：", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 4))
            sf = ttk.Frame(main_frame)
            sf.pack(fill="x", padx=8)
            saved_section_ref["frame"] = sf

        if saved_only:
            _ensure_saved_section()
            for name in saved_only:
                var = tk.BooleanVar(value=True)
                check_vars[name] = var
                cb = ttk.Checkbutton(saved_section_ref["frame"], text=name, variable=var)
                cb.pack(anchor="w")

        # 分割线
        ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)

        # 手动输入区
        ttk.Label(main_frame, text="手动添加（输入商品名，回车添加）：", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 4))

        input_frame = ttk.Frame(main_frame)
        input_frame.pack(fill="x", padx=8)

        entry = ttk.Entry(input_frame, width=28)
        entry.pack(side="left", fill="x", expand=True)

        def _add_to_saved_section(name: str):
            """将新商品懒加载添加到"已关注但当前不在售"区域。"""
            _ensure_saved_section()
            var = tk.BooleanVar(value=True)
            check_vars[name] = var
            cb = ttk.Checkbutton(saved_section_ref["frame"], text=name, variable=var)
            cb.pack(anchor="w")

        def _add_manual():
            name = entry.get().strip()
            if not name:
                return
            if name in check_vars:
                check_vars[name].set(True)
            elif name in product_set:
                # 是当前在售商品，加到上方商品区
                var = tk.BooleanVar(value=True)
                check_vars[name] = var
                cb = ttk.Checkbutton(product_frame, text=name, variable=var)
                cb.pack(anchor="w")
            else:
                # 非当前在售商品，加到下方"已关注但当前不在售"区
                _add_to_saved_section(name)
            entry.delete(0, "end")

        entry.bind("<Return>", lambda e: _add_manual())
        ttk.Button(input_frame, text="添加", command=_add_manual).pack(side="left", padx=(4, 0))

        # 分割线
        ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)

        # 按钮区
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x")

        def _save():
            new_watchlist = {name for name, var in check_vars.items() if var.get()}
            self._watchlist = new_watchlist
            self._save_watchlist()
            self._update_tooltip()
            messagebox.showinfo("成功", f"名单已保存，共 {len(new_watchlist)} 个商品。")
            root.destroy()

        def _cancel():
            root.destroy()

        ttk.Button(btn_frame, text="保存", command=_save).pack(side="left")
        ttk.Button(btn_frame, text="取消", command=_cancel).pack(side="left", padx=(8, 0))

        # 居中显示
        _center_and_show(root)

        root.mainloop()

    def _on_quit(self, icon, item):
        self._running = False
        icon.stop()

    # ── 托盘提示 ──────────────────────────────────────────────

    def _update_tooltip(self, text: str | None = None):
        """鼠标悬浮时只显示"远行商人"四个字。"""
        if self._icon is None:
            return
        self._icon.title = text if text else APP_TITLE

    # ── 定时抓取循环 ────────────────────────────────────────

    def _scheduled_fetch_loop(self):
        """在指定时间点（8:30/12:30/16:30/20:30）抓取数据。"""
        while self._running:
            now = now_beijing()
            target = None
            for h, m in FETCH_SCHEDULE:
                cand = datetime.combine(now.date(), dtime(h, m), tzinfo=now.tzinfo)
                if cand > now:
                    target = cand
                    break
            if target is None:
                tomorrow = now.date() + timedelta(days=1)
                fh, fm = FETCH_SCHEDULE[0]
                target = datetime.combine(tomorrow, dtime(fh, fm), tzinfo=now.tzinfo)

            wait = int((target - now).total_seconds())
            print(f"[调度] 下次抓取: {target.strftime('%H:%M')} (等待 {wait}s)")
            for _ in range(wait):
                if not self._running:
                    return
                time_module.sleep(1)
            if not self._running:
                return
            print(f"[定时抓取] 触发 @ {beijing_stamp(now_beijing())}")
            self._do_fetch_and_process(notify=False)

    # ── 关注商品提醒循环 ─────────────────────────────────────

    def _watchlist_reminder_loop(self):
        """关注商品命中时每 10 分钟提醒，否则每小时 :30 提醒。"""
        while self._running:
            # 每 30 秒检查一次
            for _ in range(30):
                if not self._running:
                    return
                time_module.sleep(1)

            now = now_beijing()
            now_ts = now.timestamp()

            if self._watchlist_hit_names and not self._watchlist_snoozed:
                # 有关注命中：每 10 分钟提醒
                if now_ts - self._last_reminder_ts >= REMINDER_INTERVAL_HIT * 60:
                    self._last_reminder_ts = now_ts
                    hit_list = ", ".join(sorted(self._watchlist_hit_names))
                    title = f"★ 关注商品仍在售：{hit_list}"
                    data = load_latest(CACHE_FILE)
                    summary = build_names_summary(data, self._watchlist) if data else hit_list
                    _notify(title, summary)
                    print(f"[关注提醒] {hit_list}")
            else:
                # 无关注命中：每小时 :30 提醒
                if now.minute == 30:
                    data = load_latest(CACHE_FILE)
                    if data:
                        summary = build_names_summary(data, self._watchlist)
                        _notify("商品数据提醒", summary)
                        print(f"[定时提醒] @ {beijing_stamp(now)}")
                    # 等 60 秒避免同一分钟重复触发
                    for _ in range(60):
                        if not self._running:
                            return
                        time_module.sleep(1)

    # ── 启动 ──────────────────────────────────────────────────

    def run(self):
        self._running = True

        # 加载关注名单
        self._watchlist = self._load_watchlist()
        if self._watchlist:
            print(f"[启动] 关注名单: {', '.join(self._watchlist)}")

        # 启动时立即抓取一次
        print("[启动] 首次抓取…")
        self._do_fetch_and_process()

        # 启动后提醒一次（如果有关注商品命中）
        if self._watchlist_hit_names and not self._watchlist_snoozed:
            hit_list = ", ".join(sorted(self._watchlist_hit_names))
            title = f"★ 关注商品在售：{hit_list}"
            data = load_latest(CACHE_FILE)
            summary = build_names_summary(data, self._watchlist) if data else hit_list
            _notify(title, summary)
            print(f"[启动提醒] {hit_list}")

        # 右键菜单
        menu = pystray.Menu(
            pystray.MenuItem("当前商品", self._on_show_products),
            pystray.MenuItem("管理关注名单", self._on_manage_watchlist),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "★ 本轮不再提醒关注商品",
                self._on_snooze_watchlist,
                enabled=lambda item: bool(self._watchlist_hit_names),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._on_quit),
        )

        # 创建托盘图标
        self._icon = pystray.Icon(
            name="merchant_tray",
            icon=_load_icon(),
            title=APP_TITLE,
            menu=menu,
        )

        self._update_tooltip()

        # 启动两个独立调度线程
        threading.Thread(target=self._scheduled_fetch_loop, daemon=True).start()
        threading.Thread(target=self._watchlist_reminder_loop, daemon=True).start()

        # 阻塞主线程（pystray 需要）
        self._icon.run()


# ── 入口 ──────────────────────────────────────────────────────

def main():
    app = MerchantTrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
