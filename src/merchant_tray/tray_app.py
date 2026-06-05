"""远行商人托盘程序 — 入口。

启动后常驻系统托盘，每小时抓取远行商人商品数据，
基于商品指纹智能决策是否推送 Windows 通知。
"""

import io
import hashlib
import os
import subprocess
import sys
import threading
import time as time_module
import webbrowser
from xml.sax.saxutils import escape as xml_escape

# Windows 控制台 UTF-8 输出（调试用，打包为 exe 后无影响）
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
from pathlib import Path

# ── 依赖 ──────────────────────────────────────────────────────
from PIL import Image, ImageDraw
import pystray

from .merchant import (
    active_items,
    beijing_stamp,
    fetch_merchant_data,
    load_latest,
    now_beijing,
    save_latest,
)

# ── 常量 ──────────────────────────────────────────────────────
# 打包后 sys.executable 指向 exe；源码运行时以项目根目录为应用目录。
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parents[2]
ASSETS_DIR = APP_DIR / "assets"
DATA_DIR = APP_DIR / "data"
ICON_PATH = ASSETS_DIR / "icon.png"
ICON_ICO_PATH = ASSETS_DIR / "icon.ico"
CACHE_FILE = DATA_DIR / "latest.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.txt"
APP_TITLE = "远行商人"

# 每小时抓取间隔（秒）
FETCH_INTERVAL = 3600
# 关注商品持续提醒间隔（秒）
WATCHLIST_REMINDER_INTERVAL = 1800
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


# ── 核心逻辑 ──────────────────────────────────────────────────

class MerchantTrayApp:
    def __init__(self):
        self._icon = None
        self._running = False
        self._watchlist = set()
        # ── 智能通知状态 ──
        self._last_fp: tuple[str, ...] = ()          # 上次商品指纹
        self._current_round: int | None = None        # 当前轮次号
        self._fail_count: int = 0                     # 连续失败次数
        self._watchlist_hit_names: set[str] = set()   # 当前命中的关注商品
        self._watchlist_snoozed: bool = False          # 本轮是否已静音关注提醒

    # ── 指纹 ──────────────────────────────────────────────────

    @staticmethod
    def _fingerprint(items: list[dict]) -> tuple[str, ...]:
        """商品指纹：排序后的商品名元组，仅与商品名有关。"""
        return tuple(sorted(it.get("name", "") for it in items))

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

    # ── 数据抓取 + 智能通知 ──────────────────────────────────

    def _do_fetch_and_process(self) -> None:
        """抓取数据并按指纹决策树处理通知。"""
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

        items = active_items(data)
        new_fp = self._fingerprint(items)
        new_round = data.get("round")
        print(f"[抓取成功] 轮次={new_round} 指纹={new_fp} @ {ts}")

        # ── 轮次变化：重置关注提醒状态 ──
        round_changed = (new_round != self._current_round)
        if round_changed:
            print(f"[轮次变化] {self._current_round} → {new_round}")
            self._current_round = new_round
            self._watchlist_hit_names.clear()
            self._watchlist_snoozed = False

        # ── 指纹比较 ──
        fp_changed = (new_fp != self._last_fp)

        if fp_changed:
            print(f"[指纹变化] {self._last_fp} → {new_fp}")
            self._watchlist_snoozed = False  # 商品变化自动恢复提醒

        # 更新指纹和托盘提示（无论是否变化）
        self._last_fp = new_fp
        self._update_tooltip()

        # ── 关注名单命中检测 ──
        fresh_hits: set[str] = set()
        if self._watchlist:
            current_names = {it.get("name", "") for it in items}
            new_hits = current_names & self._watchlist
            fresh_hits = new_hits - self._watchlist_hit_names
            self._watchlist_hit_names = new_hits

        # ── 合并通知（最多弹一次） ──
        summary = build_names_summary(data, self._watchlist)
        if fp_changed and fresh_hits and not self._watchlist_snoozed:
            hit_list = ", ".join(sorted(fresh_hits))
            title = f"★ 商品更新（含关注：{hit_list}）"
            _notify(title, summary)
            print(f"[商品更新+关注命中] {hit_list}")
        elif fp_changed:
            _notify("商品更新", summary)
        elif fresh_hits and not self._watchlist_snoozed:
            hit_list = ", ".join(sorted(fresh_hits))
            title = f"★ 名单商品出现！{hit_list}"
            _notify(title, summary)
            print(f"[关注命中] {hit_list}")

    # ── 菜单回调 ──────────────────────────────────────────────

    def _on_open_url(self, icon, item):
        """打开数据中返回的源网址。"""
        data = load_latest(CACHE_FILE)
        url = (data or {}).get("sourceUrl", "")
        if url:
            webbrowser.open(url)
        else:
            _notify("暂无源网址", "暂无源网址信息。")

    def _on_manage_watchlist(self, icon, item):
        """打开 tkinter 弹窗管理关注名单。"""
        threading.Thread(target=self._show_watchlist_dialog, daemon=True).start()

    def _on_snooze_watchlist(self, icon, item):
        """本轮不再提醒关注商品。"""
        self._watchlist_snoozed = True
        self._watchlist_hit_names.clear()
        print("[静音] 本轮关注提醒已暂停")
        _notify("关注提醒已暂停", "本轮不再提醒关注商品。\n商品变化或下一轮后自动恢复。")

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
        root.title("管理关注名单")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        _set_tk_window_icon(root, tk)

        # 居中
        root.update_idletasks()

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

        # 居中窗口
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        sx = (root.winfo_screenwidth() - w) // 2
        sy = (root.winfo_screenheight() - h) // 2
        root.geometry(f"+{sx}+{sy}")

        root.mainloop()

    def _on_quit(self, icon, item):
        self._running = False
        icon.stop()

    # ── 托盘提示 ──────────────────────────────────────────────

    def _update_tooltip(self, text: str | None = None):
        """鼠标悬浮时只显示商品名称。"""
        if self._icon is None:
            return
        if text:
            self._icon.title = text
            return
        data = load_latest(CACHE_FILE)
        if data is None:
            self._icon.title = "暂无数据"
        else:
            items = active_items(data)
            names = []
            for it in items:
                name = it.get("name", "?")
                if name in self._watchlist:
                    names.append(f"★{name}")
                else:
                    names.append(name)
            self._icon.title = ", ".join(names) if names else "无商品"

    # ── 每小时抓取循环 ────────────────────────────────────────

    def _hourly_fetch_loop(self):
        """每小时抓取一次，按指纹决策树处理通知。"""
        while self._running:
            # 等待 FETCH_INTERVAL 秒，每 1 秒检查 _running
            for _ in range(FETCH_INTERVAL):
                if not self._running:
                    return
                time_module.sleep(1)
            if not self._running:
                return
            print(f"[定时抓取] 触发 @ {beijing_stamp(now_beijing())}")
            self._do_fetch_and_process()

    # ── 关注商品持续提醒循环 ─────────────────────────────────

    def _watchlist_reminder_loop(self):
        """每 30 分钟检查一次，如果有关注商品命中且未静音则提醒。"""
        while self._running:
            # 等待 30 分钟
            for _ in range(WATCHLIST_REMINDER_INTERVAL):
                if not self._running:
                    return
                time_module.sleep(1)
            if not self._running:
                return
            # 检查是否需要提醒
            if self._watchlist_hit_names and not self._watchlist_snoozed:
                hit_list = ", ".join(sorted(self._watchlist_hit_names))
                title = f"★ 关注商品仍在售：{hit_list}"
                data = load_latest(CACHE_FILE)
                summary = build_names_summary(data, self._watchlist) if data else hit_list
                _notify(title, summary)
                print(f"[持续提醒] {hit_list}")

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

        # 右键菜单
        menu = pystray.Menu(
            pystray.MenuItem("打开源网址", self._on_open_url),
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
        threading.Thread(target=self._hourly_fetch_loop, daemon=True).start()
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
