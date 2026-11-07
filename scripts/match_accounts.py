#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配送员 · 匹配脚本
读取「账户ID.xlsx」(项目名称, 账户ID, 标签) 与本地视频根目录下的文件夹，
将每个文件夹匹配到对应账户，输出匹配清单 (CSV + MD)。标签列会透传到清单，
供 batch_upload 在上传前给素材打标签。

用法:
  python3 match_accounts.py
  python3 match_accounts.py --xlsx "账户ID.xlsx" --video-root "D:/视频" --out "匹配清单.csv"

默认路径来自 skill 根目录的 config.json（见 config.example.json）；没有该文件时
回落到下面的硬编码默认值。换电脑只需复制一份 config.json 改路径，不必改代码。

匹配规则 (文件夹名不含数字ID，需从名称提取产品词):
  1. 把文件夹名与项目名都按 '-' 切成词元 (token, 长度>=2)。
  2. 正向: 某个项目词元被文件夹名包含 -> 直接命中。
  3. 反向: 某个文件夹词元是某项目词元的子串 (简称⊂全称) -> 命中。
     例: 文件夹「草本枕」命中项目「十二味草本枕-金牛」。
  4. 正向优先使用更长的词元，避免「金牛」这类通用词误匹配。
"""
import argparse
import csv
import os
from datetime import datetime

try:
    import skill_config as cfg
except ImportError:          # 被别的目录调用时回落到无配置
    cfg = None


def _cfg(key, default):
    return cfg.get(key, default) if cfg else default

VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm")


def tokens(s):
    return [t.strip() for t in s.split("-") if len(t.strip()) >= 2]


def cell_text(v):
    """把单元格转成干净的字符串。

    openpyxl 会把数字单元格读成 int/float：float 直接 str() 会得到
    '100000001.0'，拼进 URL 就成了 ?__accountId__=100000001.0 —— 不报错，
    但会静默传到错误的账户。这里把整数值的 float 还原成 int 再转字符串。
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def match_folder(folder, data, extra_text=""):
    """返回 (项目名, 账户ID, hit_tuple) 或 None。

    extra_text: 文件夹内视频文件名拼接的文本，作为正向包含的补充匹配来源。
    """
    ftoks = tokens(folder)
    for name, aid, _label in data:
        ptoks = tokens(name)
        # 正向: 优先更长的项目词元 (先看文件夹名，再看视频文件名)
        for p in sorted(ptoks, key=len, reverse=True):
            if p in folder:
                return name, aid, ("direct", p)
        for p in sorted(ptoks, key=len, reverse=True):
            if len(p) >= 3 and p in extra_text:
                return name, aid, ("direct-video", p)
        # 反向: 文件夹简称⊂项目全称 (视频文件名词元也参与)
        for ft in sorted(set(ftoks) | set(tokens(extra_text)), key=len, reverse=True):
            for p in ptoks:
                if ft in p and len(ft) >= 2:
                    return name, aid, ("reverse", ft, p)
    return None


def scan_folders(video_root):
    folders = sorted(
        f for f in os.listdir(video_root) if os.path.isdir(os.path.join(video_root, f))
    )
    folder_videos = {}
    for fo in folders:
        vs = []
        for root, _, fs in os.walk(os.path.join(video_root, fo)):
            for f in fs:
                if f.lower().endswith(VIDEO_EXT):
                    # 必须用 walk 的 root，不能拼 video_root/fo：
                    # 子目录里的文件会被拼成不存在的路径（丢掉中间那层目录名）
                    vs.append(os.path.join(root, f))
        vs.sort()
        folder_videos[fo] = vs
    return folders, folder_videos


def read_xlsx(xlsx):
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.active
    data = []
    for row in ws.iter_rows(values_only=True):
        if row[0] is None:
            continue
        n = str(row[0]).strip()
        if n == "项目名称":
            continue
        aid = cell_text(row[1]) if len(row) > 1 else ""
        # 标签：纯文本。Excel 可能把数字存成 float（如 9.9 / 23.9），
        # cell_text 会转成 "9.9" / "23.9"（不强制取整），正好就是标签原值。
        label = cell_text(row[2]) if len(row) > 2 else ""
        data.append((n, aid, label))
    return data


def main():
    ap = argparse.ArgumentParser(description="配送员: 文件夹→账户ID 匹配")
    ap.add_argument("--xlsx",
                    default=_cfg("xlsxPath", os.path.join(
                        os.path.expanduser("~"), "Desktop", "账户ID.xlsx")),
                    help="账户清单 .xlsx（默认取 config.json 的 xlsxPath，否则用桌面/账户ID.xlsx）")
    ap.add_argument("--video-root", default=_cfg("videoRoot", r"D:\视频"),
                    help="视频文件夹根目录（默认取 config.json 的 videoRoot）")
    ap.add_argument("--out", default=None, help="输出 CSV 路径 (默认当前目录/配送员_匹配清单.csv)")
    args = ap.parse_args()

    data = read_xlsx(args.xlsx)
    label_by_aid = {a: l for _, a, l in data}
    folders, folder_videos = scan_folders(args.video_root)

    matches, unmatched = [], []
    for fo in folders:
        extra = " ".join(os.path.basename(v) for v in folder_videos[fo])
        m = match_folder(fo, data, extra)
        if m:
            label = label_by_aid.get(m[1], "")
            matches.append((fo, m[0], m[1], m[2], label, folder_videos[fo]))
        else:
            unmatched.append(fo)

    matched_projects = {m[1] for m in matches}
    orphan_projects = [(n, a, l) for n, a, l in data if n not in matched_projects]

    out = args.out or os.path.join(os.getcwd(), "配送员_匹配清单.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["文件夹", "匹配项目", "账户ID", "标签", "匹配方式", "视频数", "视频文件"])
        for fo, name, aid, hit, label, vs in matches:
            if hit[0] == "reverse":
                mode = f"反向包含(简称{hit[1]}⊂全称{hit[2]})"
            elif hit[0] == "direct-video":
                mode = "正向包含(视频文件名)"
            else:
                mode = "正向包含"
            w.writerow([fo, name, aid, label, mode, len(vs),
                        " | ".join(os.path.basename(v) for v in vs)])
        for fo in unmatched:
            w.writerow([fo, "（未匹配）", "", "", "", len(folder_videos[fo]), ""])

    out_md = os.path.splitext(out)[0] + ".md"
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("# 配送员 · 匹配清单\n\n")
        fh.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        fh.write(f"数据: {os.path.basename(args.xlsx)} ({len(data)} 个项目) / {args.video_root} ({len(folders)} 个文件夹)\n\n")
        fh.write(f"## 已匹配 ({len(matches)})\n\n")
        fh.write("| # | 文件夹 | 项目 | 账户ID | 标签 | 方式 | 视频数 |\n|---|---|---|---|---|---|---|\n")
        for i, (fo, name, aid, hit, label, vs) in enumerate(matches, 1):
            mode = {"direct": "正向", "direct-video": "正向(视频名)", "reverse": "反向"}[hit[0]]
            fh.write(f"| {i} | {fo} | {name} | {aid} | {label} | {mode} | {len(vs)} |\n")
        fh.write(f"\n## 未匹配 ({len(unmatched)})\n\n")
        fh.write("\n".join(f"- {f}" for f in unmatched) or "无")
        fh.write(f"\n\n## 表格中无对应文件夹的项目 ({len(orphan_projects)})\n\n")
        fh.write("| 项目 | 账户ID | 标签 |\n|---|---|---|\n")
        for n, a, l in orphan_projects:
            fh.write(f"| {n} | {a} | {l} |\n")

    print(f"匹配 {len(matches)}/{len(folders)} 文件夹, 未匹配 {len(unmatched)}")
    for fo, name, aid, hit, label, vs in matches:
        print(f"  [OK] {fo}\n       -> {name} ({aid})  标签={label!r}  视频 {len(vs)}")
    if unmatched:
        print("  [未匹配]", unmatched)
    if orphan_projects:
        print("  [无文件夹项目]", [n for n, a, _ in orphan_projects])
    print(f"输出: {out}\n      {out_md}")


if __name__ == "__main__":
    main()
