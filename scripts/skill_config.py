#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_config.py — 读取本 skill 根目录下的 config.json，给脚本提供默认路径。

换电脑时分发包里没有 config.json（刻意排除，里面是原作者的机器路径），
首次使用把 `config.example.json` 复制成 `config.json` 并改成本机路径即可，
不用改任何 .py。找不到 config.json 时回落到代码里的原默认值，行为与旧版一致。

config.json 位置：与 SKILL.md 同级（skill 根目录），不是 scripts/ 里面。
支持的键：
  videoRoot   本地视频文件夹所在的根目录（默认 "D:/视频"）
  xlsxPath    账户清单 .xlsx 的路径（默认 桌面/账户ID.xlsx）
  chromePort  Chrome 调试端口（默认 9222）
"""
import json
import os

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

_cache = None


def load():
    """读取配置（带缓存）。文件不存在或格式错误时返回空字典，不抛异常。"""
    global _cache
    if _cache is None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
    return _cache


def get(key, default=None):
    """取配置项；缺失或为空时返回默认值。"""
    val = load().get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        return default
    return val


def describe():
    """给 setup_check 用：返回 (路径, 是否存在, 内容)。"""
    return CONFIG_PATH, os.path.isfile(CONFIG_PATH), load()
