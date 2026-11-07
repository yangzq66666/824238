#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_check.py — 配送员环境自检。换电脑后第一步先跑这个。

检查项：
  1. Python 版本 >= 3.8
  2. 依赖包 websocket-client / openpyxl（缺了用 --fix 自动装）
  3. config.json 是否存在、里面的路径是否真实存在
  4. Chrome 调试端口是否连通、有没有快手标签页

用法：
  python scripts/setup_check.py          只检查，不改任何东西
  python scripts/setup_check.py --fix    缺依赖时自动 pip install
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

# 本地调试端口不走系统代理（机器配了 HTTP(S)_PROXY 时，连 127.0.0.1 也会被劫持成 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _local_json(url, timeout=4):
    with _OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK = "  [OK]  "
BAD = "  [!!]  "
WARN = "  [--]  "


def check_python():
    v = sys.version_info
    tag = ".".join(str(x) for x in v[:3])
    if v >= (3, 8):
        print(OK + f"Python {tag}")
        return True
    print(BAD + f"Python {tag} 版本过低，需要 3.8 及以上")
    return False


def check_pkg(modname, pipname, fix):
    try:
        __import__(modname)
        print(OK + f"依赖 {pipname} 已安装")
        return True
    except ImportError:
        pass
    if not fix:
        print(BAD + f"缺少依赖 {pipname} —— 跑 `pip install {pipname}`，或加 --fix 自动装")
        return False
    print(WARN + f"缺少 {pipname}，正在安装 ...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pipname],
                              stdout=subprocess.DEVNULL)
        __import__(modname)
        print(OK + f"{pipname} 安装成功")
        return True
    except Exception as e:
        print(BAD + f"{pipname} 安装失败：{e}。请手动执行 pip install {pipname}")
        return False


def check_config():
    try:
        import skill_config as cfg
    except ImportError:
        print(BAD + "读不到 skill_config.py，确认 scripts/ 目录完整")
        return None

    path, exists, data = cfg.describe()
    if not exists:
        print(WARN + "没有 config.json —— 将使用代码里的默认路径")
        print(f"        需要自定义就复制 config.example.json 为 config.json：")
        print(f"        {os.path.join(os.path.dirname(HERE), 'config.example.json')}")
        data = {}
    else:
        print(OK + f"config.json 已读取：{path}")

    ok = True
    root = data.get("videoRoot") or "D:/视频"
    if os.path.isdir(root):
        n = len([d for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))])
        print(OK + f"视频根目录存在：{root}（{n} 个文件夹）")
    else:
        print(BAD + f"视频根目录不存在：{root} —— 在 config.json 里改成你的路径")
        ok = False

    xlsx = data.get("xlsxPath") or os.path.join(
        os.path.expanduser("~"), "Desktop", "账户ID.xlsx")
    if os.path.isfile(xlsx):
        note = "" if data.get("xlsxPath") else "（config 未配置，用的桌面默认位置）"
        print(OK + f"账户清单存在：{xlsx}{note}")
    else:
        print(BAD + f"账户清单不存在：{xlsx} —— 在 config.json 里改成你的路径")
        ok = False
    return ok


def check_chrome(port):
    try:
        info = _local_json(f"http://127.0.0.1:{port}/json/version")
        print(OK + f"Chrome 调试端口 {port} 连通：{info.get('Browser', '未知版本')}")
    except Exception as e:
        print(BAD + f"连不上 127.0.0.1:{port} —— {e}")
        print("        用调试端口重启 Chrome（先完全退出 Chrome）：")
        print(f'        chrome.exe --remote-debugging-port={port} '
              '--user-data-dir="C:\\Users\\<你>\\AppData\\Local\\Google\\Chrome\\User Data"')
        return False

    try:
        tabs = _local_json(f"http://127.0.0.1:{port}/json/list")
    except Exception:
        tabs = []
    pages = [t for t in tabs if t.get("type") == "page"]
    niu = [t for t in pages if "niu.e.kuaishou.com" in t.get("url", "")]
    jinfu = [t for t in pages if "jinfu.e.kuaishou.com" in t.get("url", "")]
    print(f"        标签页 {len(pages)} 个；niu 投放后台 {len(niu)} 个；"
          f"jinfu 服务商后台 {len(jinfu)} 个")
    if not niu and not jinfu:
        print(WARN + "没看到快手后台标签页 —— 请先在 Chrome 里登录磁力金牛，"
                     "并打开一个账户的视频库")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="配送员环境自检")
    ap.add_argument("--fix", action="store_true", help="缺依赖时自动 pip install")
    args = ap.parse_args()

    print("=== 配送员 环境自检 ===\n")
    results = [check_python()]
    results.append(check_pkg("websocket", "websocket-client", args.fix))
    results.append(check_pkg("openpyxl", "openpyxl", args.fix))
    cfg_ok = check_config()
    if cfg_ok is not None:
        results.append(cfg_ok)
    try:
        import skill_config as _c
        port = int(_c.get("chromePort", 9222))
    except Exception:
        port = 9222
    results.append(check_chrome(port))

    print()
    if all(results):
        print("全部通过。下一步：")
        print("  python scripts/match_accounts.py        # 生成匹配清单")
        print("  python scripts/batch_upload.py --map <清单.csv> --aggregate --dry-run")
    else:
        print("有未通过项，按上面的提示处理后重跑本脚本。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
