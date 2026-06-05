# 远行商人托盘助手

洛克王国世界远行商人商品信息自动抓取与 Windows 托盘通知程序。

## 环境要求

- Windows 10 / 11
- Python 3.10+

## 功能

- 启动后常驻 Windows 系统托盘，无主窗口
- 启动时立即抓取一次商品数据
- 每小时自动抓取一次最新数据
- 商品变化时推送 Windows Toast 通知
- 支持关注商品名单，命中后提醒，并每 30 分钟持续提醒
- 本地缓存最新数据，断网时可继续查看上次结果

## 配置

在项目根目录创建 `.env` 文件（可参考 `.env.example`），填入 API 地址：

```ini
MERCHANT_API_URL=你的API地址
```

> `.env` 文件包含你的私有配置，已加入 `.gitignore`，不会上传到仓库。

## 快速运行

双击运行：

```bat
setup.bat
```

它会安装依赖、检查运行目录，并把 `start.bat` 加入开机自启。

之后可以双击：

```bat
start.bat
```

## 手动运行

```bat
python -m pip install -r requirements.txt
set PYTHONPATH=%cd%\src
python -m merchant_tray.tray_app
```

如果系统里 `python` 不可用，可以把上面的 `python` 换成 `py`。

## 托盘菜单

| 菜单项 | 功能 |
| --- | --- |
| 打开源网址 | 打开当前数据里的来源页面 |
| 管理关注名单 | 勾选或手动添加关注商品 |
| 本轮不再提醒关注商品 | 暂停当前轮次的关注商品持续提醒 |
| 退出 | 退出托盘程序 |

## 目录结构

```text
远行商人/
├── assets/                  # 图标资源
│   ├── icon.png             # 运行时托盘图标
│   ├── icon.ico             # Windows 图标备用
│   └── icon_256.png         # 高清图标备用
├── data/                    # 运行数据（自动生成，不上传）
│   ├── latest.json          # 最新抓取缓存
│   └── watchlist.txt        # 关注商品名单
├── src/
│   └── merchant_tray/
│       ├── __init__.py
│       ├── launcher.py      # 轻量启动入口
│       ├── merchant.py      # 数据抓取与解析逻辑
│       └── tray_app.py      # 托盘 UI、通知、定时逻辑
├── .env                     # API 配置（不上传，需自行创建）
├── .env.example             # 配置模板
├── .gitignore
├── LICENSE
├── requirements.txt         # Python 依赖
├── setup.bat                # 首次安装与开机自启
├── start.bat                # 后台启动托盘程序
└── README.md
```

## 依赖

- pystray：系统托盘图标
- Pillow：图标图像加载
- Windows PowerShell / Windows Runtime：Windows Toast 通知

## 许可证

[MIT License](LICENSE)
