#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdp_upload.py — 直连 Chrome 调试端口，用 CDP 原生 DOM.setFileInputFiles 把本地文件
注入浏览器文件输入框。

为什么不用 bsk 的 upload
------------------------
bsk 会把文件字节搬进「会话暂存通道」，受 512MiB **会话累计**预算限制（observe 的截图
探针也占额度），且失败后暂存不释放。本脚本只把**本地路径**交给浏览器，由 Chrome 自己
从磁盘读，没有这段搬运 —— 单次上限回到页面自身的规则（如快手：100 个/批、单个 <500MB）。

注入机制（关键）
----------------
Chrome 要求**真实用户手势**才会打开文件选择器，JS 的 input.click() 无效。
正确做法是 CDP 的 chooser 拦截流程：
    1) Page.setInterceptFileChooserDialog {enabled: true}
    2) Input.dispatchMouseEvent 对「拖拽/选择区」派发**可信**鼠标点击
    3) 收到 Page.fileChooserOpened 事件（含 backendNodeId）
    4) DOM.setFileInputFiles {files, backendNodeId}   ← 目标方法
若拿不到 chooser 事件（如普通测试页面），自动回退为直接用 nodeId 注入。

前置条件
--------
Chrome 带调试端口启动（用原 profile，保留登录态）：
    chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\Users\\<你>\\AppData\\Local\\Google\\Chrome\\User Data"

用法
----
  python cdp_upload.py --list-tabs

  # 快手视频库：打开上传框 -> 注入整个目录 -> 提交 -> 等对话框消失
  python cdp_upload.py \
      --url-match "material/supervideo" \
      --open-text "上传视频" \
      --selector '.kcfe-ad-file-selector' \
      --verify-text "视频上传要求" \
      --dir "D:/视频/2026年10月27日-张三-断痔膏-1" --ext .mp4 \
      --submit-text "上传" \
      --wait-gone '.kcfe-ad-file-selector'

  # 试运行：只注入并报告，然后取消（不真正上传）
  ... --dry-run

  # 每次运行前刷新页面（清除反复开关弹窗留下的残留节点，推荐批量时使用）
  ... --reload
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

# 本地调试端口永远不该走系统代理：机器配了 HTTP(S)_PROXY 时，urllib 默认连
# 127.0.0.1 也会被劫持（表现为 502 Bad Gateway / upstream connect failed）。
# 用空 ProxyHandler 的 opener 直连。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _local_json(url, timeout=10):
    with _NO_PROXY_OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

import websocket

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class CDP:
    def __init__(self, ws_url):
        # Chrome 会校验 WS 握手的 Origin：若浏览器不是用 --remote-allow-origins=*
        # 启动的，带 Origin 会被 403 拒绝。省略该头即可直连，无需重启浏览器。
        self.ws = websocket.create_connection(ws_url, timeout=120, suppress_origin=True)
        self._id = 0
        self.events = []

    def call(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if msg.get("method"):
                self.events.append(msg)

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS 异常: {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")

    def wait_event(self, name, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events:
                if e["method"] == name:
                    self.events.remove(e)
                    return e["params"]
            self.ws.settimeout(1)
            try:
                m = json.loads(self.ws.recv())
                if m.get("method"):
                    self.events.append(m)
            except Exception:
                pass
            finally:
                self.ws.settimeout(120)
        return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 页面操作

def click_text(cdp, text, tags="button, a, [role=button]"):
    """点击含该文本的元素；命中多个时取文本最短的那个，避免点到外层容器。"""
    r = cdp.js(f"""(() => {{
        const t = {text!r};
        const hits = [];
        for (const e of document.querySelectorAll({tags!r})) {{
            const s = (e.textContent || '').trim();
            if (!s.includes(t)) continue;
            if (e.offsetParent === null && e.getClientRects().length === 0) continue;
            hits.push({{el: e, txt: s}});
        }}
        if (!hits.length) return 'NOT_FOUND';
        hits.sort((a, b) => a.txt.length - b.txt.length);
        hits[0].el.click();
        return 'clicked <' + hits[0].el.tagName.toLowerCase() + '>: ' + hits[0].txt.slice(0, 40);
    }})()""")
    if r == "NOT_FOUND":
        raise RuntimeError(f"找不到含文本 {text!r} 的可点击元素")
    return r


def click_selector(cdp, selector):
    r = cdp.js(f"""(() => {{
        const e = document.querySelector({selector!r});
        if (!e) return 'NOT_FOUND';
        e.click();
        return 'clicked ' + e.tagName.toLowerCase();
    }})()""")
    if r == "NOT_FOUND":
        raise RuntimeError(f"找不到元素: {selector}")
    return r


def trusted_click(cdp, selector):
    """对元素派发可信鼠标点击（带 user activation，可触发文件选择器）。"""
    box = cdp.js(f"""(() => {{
        const e = document.querySelector({selector!r});
        if (!e) return null;
        e.scrollIntoView({{block: 'center', inline: 'center'}});
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        return {{x: r.x + r.width / 2, y: r.y + r.height / 2,
                vw: window.innerWidth, vh: window.innerHeight,
                top: r.top, left: r.left}};
    }})()""")
    if not box:
        raise RuntimeError(f"元素不可见或不存在，无法派发可信点击: {selector}")
    if not (0 <= box["x"] <= box["vw"] and 0 <= box["y"] <= box["vh"]):
        raise RuntimeError(
            f"元素中心 ({box['x']:.0f},{box['y']:.0f}) 不在视口内 "
            f"({box['vw']}x{box['vh']})，可能弹窗未打开或页面布局异常")
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=box["x"], y=box["y"])
    time.sleep(0.1)
    cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=box["x"], y=box["y"],
             button="left", clickCount=1, buttons=1)
    time.sleep(0.1)
    cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=box["x"], y=box["y"],
             button="left", clickCount=1, buttons=0)
    return f"({box['x']:.0f}, {box['y']:.0f})"


def modal_state(cdp, marker):
    """取弹窗（含 marker 文本与『取消』按钮的最小共同祖先）的已暂存条目数。

    计数优先级：
      1) 快手弹窗自带权威计数器「已上传素材（N/100）」里的 N —— 最可靠，
         不依赖文件名解析。快手会把长文件名截断成「2026年10...一-1」
         （中间插 …、结尾是真实后缀，既不是 .mp4 结尾也不是 ...(N) 结尾），
         逐个解析文件名会恒为 0，曾导致注入其实成功却被判成 0 条目。
      2) 数卡片：列表里每个已暂存文件一张 `...-list-item` 卡片，排除「选择视频」占位卡。
         新版弹窗已经不显示「已上传素材（N/100）」计数器了，所以这条是主力。
      3) 回退：数文件名叶子（卡片结构再变时兜底）。

    ⚠️ 2026-10-27 踩过的坑：这里曾对 entries 写了 `.slice(0, 30)`，导致
    注入 104/87 个时条目数一律读成 30，汇总里出现假的「暂存30」并刷 warn。
    切片只能用于返回值展示，绝不能用它参与计数。
    """
    return cdp.js(f"""(() => {{
        const t = Array.from(document.querySelectorAll('*')).find(e =>
            e.children.length === 0 && (e.textContent || '').includes({marker!r}));
        if (!t) return null;
        let m = t;
        while (m && !(m.textContent || '').includes('取消')) m = m.parentElement;
        if (!m) return null;
        const inner = (m.innerText || '').replace(/\\s+/g, ' ').trim();
        // 1) 权威计数：「已上传素材（N/100）」里的 N（新版 UI 已不再渲染，兜底用）
        let count = -1;
        const cm = inner.match(/已上传素材[（(]\\s*(\\d+)/);
        if (cm) count = parseInt(cm[1], 10);
        // 2) 卡片数：每个已暂存文件一张卡片，排除「选择视频」占位卡
        let cards = -1;
        const cardEls = Array.from(m.querySelectorAll(
            '.kcfe-ad-upload-video-content-content-list-item'));
        if (cardEls.length) {{
            const ph = cardEls.filter(c => (c.innerText || '').includes('选择视频')).length;
            cards = cardEls.length - ph;
        }}
        // 3) 回退：数文件名叶子（被截断的长名也计，仅排除已知 UI 词）
        const UI = new Set(['添加标签','视频上传要求：','取消','上传','选择视频',
            '视频同步至快手个人主页','更多曝光机会','已上传素材']);
        const entries = Array.from(m.querySelectorAll('*'))
            .filter(e => e.children.length === 0)
            .map(e => (e.textContent || '').trim())
            .filter(s => s && !UI.has(s))
            .filter(s => /\\.\\.\\./.test(s)
                      || /\\.(mp4|mov|avi|mkv|flv|wmv|webm)$/i.test(s)
                      || /\\.\\.\\.\\(\\d+\\)\\s*$/.test(s));
        if (count < 0 && cards >= 0) count = cards;
        if (count < 0 && entries.length) count = entries.length;
        // entries 仅用于日志展示，切片不能参与上面的计数
        return {{count: count, cards: cards, entries: entries.slice(0, 20),
                text: inner.slice(0, 200)}};
    }})()""")


def read_account_context(cdp):
    """回读「页面当前是哪个账户」。

    为什么需要这个
    --------------
    navigate 到 `?__accountId__=<ID>` 之后，如果这个 ID 无效、或者当前登录账号
    对它没有权限，快手 SPA **不会报错**——它会继续显示上一个账户的视频库。
    于是视频就传到别人库里了，而流程照样记 UPLOADED。这是整个 skill 里唯一一种
    「东西传错地方还不报错」的失败模式，损失远大于传失败。

    抓三个证据，优先级从硬到软：
      1. url_aid    —— 地址栏里的 __accountId__（SPA 切换账户后可能把它改回旧值）
      2. state_aid  —— 页面全局状态里的 accountId / advertiserId（SPA 自己的真实状态）
      3. name       —— 页面上显示的账户名（软证据，只记录不单独用于阻断）

    返回 dict：{href, url_aid, state_aid, name}；读不到的字段为 None。
    """
    raw = cdp.js(r"""(() => {
        const out = {href: location.href || '', urlAid: null, stateAid: null, name: null};

        const m = out.href.match(/__accountId__=(\d+)/);
        if (m) out.urlAid = m[1];

        const IDK   = ['accountId', 'account_id', 'advertiserId', 'advertiser_id'];
        const NAMEK = ['accountName', 'account_name', 'advertiserName', 'advertiser_name'];
        const NOISE = /余额|充值|明细|设置|总资产|账户总|\d{4}-\d{2}-\d{2}/;

        const isAid = v => {
            if (typeof v === 'number') v = String(v);
            if (typeof v !== 'string') return false;
            return /^\d{6,20}$/.test(v.trim());
        };
        const pickName = o => {
            for (const k of NAMEK) {
                if (o && typeof o[k] === 'string' && o[k].trim()
                    && !NOISE.test(o[k])) return o[k].trim();
            }
            return null;
        };

        const seen = new Set();
        let budget = 20000;          // 全局状态可能很大，扫够就收手
        const scan = (o, d) => {
            if (!o || typeof o !== 'object' || d > 6 || budget <= 0) return;
            if (seen.has(o)) return;
            seen.add(o); budget--;
            if (!Array.isArray(o)) {
                if (out.stateAid === null) {
                    for (const k of IDK) {
                        if (isAid(o[k])) { out.stateAid = String(o[k]).trim(); break; }
                    }
                }
                if (out.name === null) { const n = pickName(o); if (n) out.name = n; }
            }
            for (const k of Object.keys(o)) {
                if (k.length > 40) continue;
                try { scan(o[k], d + 1); } catch (e) {}
            }
        };

        const GLOBALS = ['__INITIAL_STATE__', '__NEXT_DATA__', '__NUXT__', '__APP_STATE__',
                         '_store', 'store', '__STORE__', '__redux_store__', '__KS_STATE__'];
        for (const g of GLOBALS) {
            try { if (window[g]) scan(window[g], 0); } catch (e) {}
        }

        // DOM 兜底：顶部账户切换器（只在状态里没抓到名字时才用，噪声较大）
        if (out.name === null) {
            for (const e of document.querySelectorAll('[class*="account"], [class*="Account"]')) {
                const t = (e.innerText || '').trim().split('\n')[0].trim();
                if (t.length >= 2 && t.length <= 40 && !NOISE.test(t)) { out.name = t; break; }
            }
        }
        return out;
    })()""")
    if not isinstance(raw, dict):
        return {"href": "", "url_aid": None, "state_aid": None, "name": None}
    return {"href": raw.get("href") or "",
            "url_aid": raw.get("urlAid"),
            "state_aid": raw.get("stateAid"),
            "name": raw.get("name")}


# ---------------------------------------------------------------- 多账户 / 标签 / 结果

def create_tab(port, url):
    """在浏览器里新开一个标签页并等待就绪，返回它的 webSocketDebuggerUrl。

    用于「每个账户单独一标签页」：新标签页继承同一浏览器 profile 的登录态，
    直接以该账户的视频库 URL 打开，后续处理都在里面进行，跑完也不关，方便人工查看。

    实现：从浏览器级调试端点 /json/version 取 webSocketDebuggerUrl（属于
    /devtools/browser/<id> 会话），在其上 Target.enable + Target.createTarget 开新标签。
    浏览器级会话才暴露 Target 域；页面级会话调用会报 -32601 Target.enable wasn't found。
    （早期用 HTTP /json/new?url= 端点，本机 Chrome 返回 405，故改走此路。）
    """
    # 1) 浏览器级 WS（/devtools/browser/...），这是唯一暴露 Target 域的会话
    ver = _local_json(f"http://127.0.0.1:{port}/json/version", timeout=10)
    browser_ws = ver.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise RuntimeError(f"/json/version 未返回 webSocketDebuggerUrl：{ver}")

    # 2) 在浏览器级会话上开新标签
    # 注意：本机 Chrome（156.x）直接调 Target.createTarget 即可，先调 Target.enable
    # 反而报 -32601 'Target.enable' wasn't found；故省略 enable。
    bc = CDP(browser_ws)
    try:
        res = bc.call("Target.createTarget", url=url)
        target_id = res.get("targetId")
        if not target_id:
            raise RuntimeError(f"Target.createTarget 未返回 targetId：{res}")
    finally:
        bc.close()

    # 3) 轮询 /json/list 拿新标签的 WS URL（标签创建后略延迟才出现）
    for _ in range(80):
        for p in list_pages(port):
            if p.get("id") == target_id and p.get("webSocketDebuggerUrl"):
                return p["webSocketDebuggerUrl"]
        time.sleep(0.3)
    # 兜底：标签已存在但 /json/list 暂未刷新出 WS URL
    for p in list_pages(port):
        if p.get("id") == target_id:
            ws = p.get("webSocketDebuggerUrl")
            if ws:
                return ws
    raise RuntimeError(f"新标签 {target_id} 已创建，但拿不到 webSocketDebuggerUrl")


def _react_set_value(cdp, finder_js, value):
    """用 React 友好的方式给受控输入框赋值并触发 input/change 事件。

    finder_js 必须是一段「返回 input 元素」的 JS 表达式（不要带外层函数包裹）。
    返回 'SET' / 'NO_INPUT'。
    """
    return cdp.js(f"""(() => {{
        const inp = (() => {{ {finder_js} }})();
        if (!inp || inp.tagName !== 'INPUT') return 'NO_INPUT';
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        setter.call(inp, {value!r});
        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'SET';
    }})()""")


# ---- 「添加标签」气泡的真实结构（2026-10-27 实测 dump）
#
#   [class*="popoverContainer"]                 ← ant-popover 的真实面板（portal 渲染）
#     [class*="tagContainer"]
#       [class*="popoverContainerItem"]         ← 一个标签一行
#         <label class="ant-checkbox-wrapper">
#           <span class="ant-checkbox">
#             <input type="checkbox" class="ant-checkbox-input">
#             <span class="ant-checkbox-inner"></span>
#           </span>
#         </label>
#         心心相印                               ← 标签文字是 label 的【兄弟节点】，label 自己是空的
#         <span class="anticon anticon-system-delete-line …">   ← 删除图标
#     [class*="add"]   <input placeholder="限10字以内" maxlength="10">
#       [class*="submit"]（内容为空时带 index-module__disable___*，此时点了没反应）
#         [class*="addText"] <span class="anticon anticon-plus"></span>添加标签
#     底部 [取消] [确定]
#
# 以前打不上标签的两个根因：
#   1) 按 label.innerText 匹配文字 —— label 里没有文字，永远匹配不到；
#      => 必须用「整行 textContent」匹配，且点击目标是 label/checkbox 本身（点文字无效）。
#   2) 「+ 添加标签」不是一个带该文字的按钮，而是 div 提交区；
#      => 不能按文字点，必须定位 [class*="submit"] 并等它脱离 disable。

_POP_HEAD = """
const _vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
};
const _norm = s => (s || '').replace(/\\s+/g, '');
const _panel = () => Array.from(document.querySelectorAll('[class*="popoverContainer"]'))
    .filter(_vis)
    .find(p => p.querySelector('input[placeholder*="限"], input[placeholder*="标签"]')) || null;
const _rows = p => Array.from(p.querySelectorAll('[class*="popoverContainerItem"]')).map(it => {
    const box = it.querySelector('label.ant-checkbox-wrapper')
             || it.querySelector('input[type=checkbox]') || it;
    const r = box.getBoundingClientRect();
    const cb = it.querySelector('input[type=checkbox]');
    return {
        txt: _norm(it.textContent),
        checked: !!it.querySelector('.ant-checkbox-checked') || !!(cb && cb.checked),
        x: r.x + r.width / 2, y: r.y + r.height / 2
    };
});
"""


def open_label_popover(cdp, timeout=12):
    """点开「添加标签」气泡。必须点 .ant-btn 的那个按钮（标题栏同名 DIV 点不动）。"""
    box = cdp.js("""(() => {
        const btn = Array.from(document.querySelectorAll('.ant-btn'))
            .find(b => (b.textContent || '').trim() === '添加标签'
                       && b.getBoundingClientRect().width > 0);
        if (!btn) return null;
        try { btn.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        const r = btn.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()""") or None
    if not box:
        return False
    _click_box(cdp, box)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cdp.js("(() => {" + _POP_HEAD + "return !!_panel();})()"):
            return True
        time.sleep(0.3)
    return False


def _tag_rows(cdp):
    """气泡里所有标签行：[{txt, checked, x, y}]（txt 已去掉空白）。"""
    return cdp.js("(() => {" + _POP_HEAD + """
        const p = _panel();
        if (!p) return [];
        return _rows(p);
    })()""") or []


def _click_tag(cdp, name):
    """勾选某个已存在的标签（默认未勾选）。返回 True=最终处于已勾选态。"""
    want = re.sub(r"\s+", "", name or "")
    if not want:
        return False
    rows = _tag_rows(cdp)
    row = next((r for r in rows if r["txt"] == want), None) \
        or next((r for r in rows if want in r["txt"]), None)
    if row is None:
        return False
    if row.get("checked"):
        return True
    _click_box(cdp, {"x": row["x"], "y": row["y"]})
    time.sleep(0.35)
    rows = _tag_rows(cdp)
    row2 = next((r for r in rows if r["txt"] == want), None)
    return bool(row2 and row2.get("checked"))


def _create_tag(cdp, name, timeout=10):
    """气泡里新建标签：键入 -> 等提交区可用 -> 点提交区 -> 等新行出现。

    返回 True 表示新标签已出现在列表（注意：新标签默认【未勾选】，调用方还要再点一次）。
    """
    box = cdp.js("(() => {" + _POP_HEAD + """
        const p = _panel();
        if (!p) return null;
        const inp = p.querySelector('input[placeholder*="限"], input[placeholder*="标签"]')
                 || Array.from(p.querySelectorAll('input'))
                        .find(i => (i.type === 'text' || !i.type) && _vis(i));
        if (!inp) return null;
        inp.scrollIntoView({block: 'center'});
        const r = inp.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()""") or None
    if not box:
        return False
    _click_box(cdp, box)
    time.sleep(0.2)
    try:
        cdp.call("Input.insertText", text=name)
    except Exception:
        _react_set_value(cdp, "return document.activeElement;", name)
    # 等提交区脱离 disable（内容为空时它是 disabled，点了没反应）
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cdp.js("(() => {" + _POP_HEAD + """
            const p = _panel();
            if (!p) return false;
            const sub = p.querySelector('[class*="submit"]');
            return !!sub && !/disable/i.test(sub.className);
        })()"""):
            break
        time.sleep(0.25)
    sub = cdp.js("(() => {" + _POP_HEAD + """
        const p = _panel();
        if (!p) return null;
        const sub = p.querySelector('[class*="submit"]');
        if (!sub || !_vis(sub)) return null;
        const r = sub.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()""") or None
    if not sub:
        return False
    _click_box(cdp, {"x": sub["x"], "y": sub["y"]})
    want = re.sub(r"\s+", "", name)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(r["txt"] == want for r in _tag_rows(cdp)):
            return True
        time.sleep(0.25)
    return False


def _close_label_popover(cdp):
    """点气泡里的「确定」关掉（限定在气泡内找，避免点到页面别处的确定）。"""
    box = cdp.js("(() => {" + _POP_HEAD + """
        const p = _panel();
        if (!p) return null;
        const b = Array.from(p.querySelectorAll('button, .ant-btn'))
            .find(x => (x.textContent || '').trim() === '确定' && _vis(x));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()""") or None
    if box:
        _click_box(cdp, box)
        time.sleep(0.8)
        return True
    try:
        click_text(cdp, "确定")
        time.sleep(0.6)
        return True
    except RuntimeError:
        return False


def read_effective_tags(cdp):
    """回读上传弹窗里「标签」区当前生效的标签文字（去空白），用于校验是否真的加上。"""
    return cdp.js("""(() => {
        const boxes = Array.from(document.querySelectorAll('[class*="tagList"]'));
        return boxes.map(b => (b.textContent || '').replace(/\\s+/g, '')).join('|');
    })()""") or ""


def _click_box(cdp, box):
    """对 box {x,y} 派发可信鼠标点击（带 user activation）。"""
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=box["x"], y=box["y"])
    time.sleep(0.1)
    cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=box["x"], y=box["y"],
             button="left", clickCount=1, buttons=1)
    time.sleep(0.1)
    cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=box["x"], y=box["y"],
             button="left", clickCount=1, buttons=0)


def apply_labels(cdp, labels, allow_create=True, timeout=12):
    """在上传弹窗里给本批素材加标签（真实交互，按 2026-10-27 实测 DOM 重写）。

    labels: list[str]（已按逗号/顿号/分号拆好）。
    返回 (applied, created, missing) 三个 set。

    流程：
      1) 点 .ant-btn 的「添加标签」开气泡 —— 标题栏那个同名 DIV 点不动，不能按文字点；
      2) 每个标签：已在列表里 -> 直接勾选（列表项默认未勾选）；
         不在且 allow_create -> 键入 -> 等提交区脱离 disable -> 点提交区新建 ->
         **新建后必须再点一次勾选**（新标签默认未勾选，用户明确要求补这步）；
      3) 点气泡内「确定」关闭；
      4) 回读上传弹窗标签区校验，没出现在标签区的降级为 missing（避免假成功）。

    allow_create=False（dry-run 用）：只勾已有标签、不新建，避免污染账户标签集。
    """
    applied, created, missing = set(), set(), set()
    todo = [x.strip() for x in (labels or []) if x and x.strip()]
    if not todo:
        return applied, created, missing
    if not open_label_popover(cdp):
        missing.update(todo)
        return applied, created, missing
    time.sleep(0.4)
    for lab in todo:
        if _click_tag(cdp, lab):
            applied.add(lab)
            continue
        if not allow_create:
            missing.add(lab)
            continue
        if not _create_tag(cdp, lab):
            missing.add(lab)
            continue
        # 新建成功后必须重新勾选一次（默认未勾选）
        if _click_tag(cdp, lab):
            applied.add(lab)
            created.add(lab)
        else:
            created.add(lab)
            missing.add(lab)
    _close_label_popover(cdp)
    # 回读校验：标签区里看不到的降级为 missing（标签区读不到内容时不做降级，避免误报）
    seen = read_effective_tags(cdp)
    if seen:
        for lab in list(applied):
            if re.sub(r"\s+", "", lab) not in seen:
                applied.discard(lab)
                missing.add(lab)
    return applied, created, missing


VIDEO_RE = r"\.\.\.|\.(mp4|mov|avi|mkv|flv|wmv|webm)$"


_TIP_BAD = ("新增视频分析", "知道了", "加载问卷", "体验调研")


def hover_and_read_tooltip(cdp, box, wait=0.7):
    """把可信鼠标移到 box 中心，读**贴着这个点出现**的浮层文字；拿不到返回 ''。

    为什么不能只取「最后一个可见 tooltip」：页面上常驻平台公告（“新增视频分析功能…知道了”）
    和反馈弹窗，那样会把公告文字当成失败原因（上一版就串了）。
    这里只接受：宽高 > 0、不在 hidden、含文字、水平距离悬停点 < 600px、
    垂直距离 < 260px，并按面积取最小（即 .ant-tooltip-inner 而非外层容器）。
    """
    if not box:
        return ""
    x, y = box.get("x"), box.get("y")
    if x is None or y is None:
        return ""
    try:
        cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=2, y=2)
    except Exception:
        pass
    time.sleep(0.25)
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    time.sleep(wait)
    return cdp.js("""(() => {
        const x = %r, y = %r, bad = %r;
        const vis = el => {
            if (!el) return false;
            if (el.classList && el.classList.contains('ant-tooltip-hidden')) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const out = [];
        for (const t of Array.from(document.querySelectorAll(
                '.ant-tooltip, .ant-tooltip-inner, [role=tooltip]'))) {
            if (!vis(t)) continue;
            const txt = (t.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!txt) continue;
            if (bad.some(b => txt.indexOf(b) >= 0)) continue;
            const r = t.getBoundingClientRect();
            if (Math.abs(r.left - x) > 600) continue;
            if (r.bottom < y - 260 || r.top > y + 260) continue;
            out.push({txt: txt, area: r.width * r.height});
        }
        if (!out.length) return '';
        out.sort((a, b) => a.area - b.area);
        return out[0].txt;
    })()""" % (x, y, list(_TIP_BAD))) or ""


def _count_failed_cards(cdp):
    """失败卡片数量（含 <video>/<img> 且有『原因』链接的 list-item）。"""
    return cdp.js("""(() => {
        return Array.from(document.querySelectorAll(
                '.kcfe-ad-upload-video-content-content-list-item'))
            .filter(c => c.querySelector('video, img')
                && Array.from(c.querySelectorAll('*')).some(e =>
                    e.children.length === 0 && (e.textContent || '').trim() === '原因'))
            .length;
    })()""") or 0


def _probe_failed_card(cdp, idx):
    """定位第 idx 个失败卡片：滚入视口后返回名称/原因链接的真实坐标与可见文字。

    名称在卡片里默认被截断成 '...'，完整名只有悬停才知道；若能读到 title 属性就直接用。
    """
    return cdp.js("""(() => {
        const idx = %d;
        const cards = Array.from(document.querySelectorAll(
                '.kcfe-ad-upload-video-content-content-list-item'))
            .filter(c => c.querySelector('video, img')
                && Array.from(c.querySelectorAll('*')).some(e =>
                    e.children.length === 0 && (e.textContent || '').trim() === '原因'));
        const c = cards[idx];
        if (!c) return null;
        try { c.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
        const rectOf = el => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        };
        const leaves = Array.from(c.querySelectorAll('*'))
            .filter(e => e.children.length === 0);
        const reasonEl = leaves.find(e => (e.textContent || '').trim() === '原因');
        const nameEl = leaves.find(e => {
            const t = (e.textContent || '').trim();
            return /\\.\\.\\.$/.test(t) && !/原因|请检查|加载失败/.test(t);
        });
        // 卡片内带 title 的元素里，最长的那个通常就是完整文件名
        let title = '';
        for (const e of Array.from(c.querySelectorAll('[title], [data-title]'))) {
            const t = (e.getAttribute('title') || e.getAttribute('data-title') || '').trim();
            if (t.length > title.length) title = t;
        }
        return {
            nameText: nameEl ? (nameEl.textContent || '').trim() : '',
            nameTitle: title,
            nameBox: rectOf(nameEl),
            reasonBox: rectOf(reasonEl),
            cardText: (c.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)
        };
    })()""" % idx) or None


def scan_failed_items(cdp):
    """上传完成后，抓取失败素材的「完整名称」与「失败原因」。

    真实 UI（2026-10-27 实测）：失败的是缩略图卡片
        .kcfe-ad-upload-video-content-content-list-item(-small)
          > <video> + 蒙层「视频加载失败」 + 蓝色『原因』链接
    卡片里的名称默认只显示 '...'——完整名必须把鼠标移到名称上，由 tooltip 给出；
    失败原因把鼠标移到『原因』上，tooltip 文案形如
        「视频宽高比需满足条件 / 视频上传失败，未取得视频ID」。
    公告横幅（“新增视频分析功能…知道了”）不是 tooltip，已由坐标+黑名单过滤掉。

    返回 list[dict]: {"name": 完整名(或截断名), "reason": 原因}。
    """
    n = _count_failed_cards(cdp)
    if not n:
        return []
    out, order = {}, []
    for i in range(n):
        info = _probe_failed_card(cdp, i)
        if not info:
            continue
        # 完整名只认悬停 tooltip：卡片里 title 属性本身是被截断的
        # （实测 title="年10月27日-项目A"，tooltip 才是
        #  "2026年10月27日-项目A一-1"），所以顺序不能反。
        name = ""
        full = hover_and_read_tooltip(cdp, info.get("nameBox"))
        if full:
            name = full.strip()
        if not name:
            name = (info.get("nameTitle") or "").strip()
        if not name:
            name = (info.get("nameText") or "").strip()
        reason = hover_and_read_tooltip(cdp, info.get("reasonBox")) or ""
        for head in ("上传的视频需满足以下条件：", "上传的视频需满足以下条件:"):
            if reason.startswith(head):
                reason = reason[len(head):].strip()
        reason = re.sub(r"\s+", " ", reason).strip()
        key = name or f"card{i}"
        if key in out:
            # 同名多条时合并原因，避免「同原因不同文件」被误当重复
            if reason and reason not in out[key]["reason"]:
                out[key]["reason"] = (out[key]["reason"] + "；" + reason).strip("；")
            continue
        out[key] = {"name": name, "reason": reason}
        order.append(key)
    return [out[k] for k in order]


def dismiss_upload_modal(cdp, timeout=10):
    """收掉上传抽屉/结果弹窗。部分失败时它不会自动消失，必须主动关掉才能继续下一批。

    顺序：抽屉内「完成/关闭/知道了/取消」文字按钮 -> 抽屉关闭图标 -> Esc。
    只在抽屉容器内找文字按钮，避免点到页面别处（如公告的「知道了」）而关错东西。
    """
    box = cdp.js("""(() => {
        const hosts = Array.from(document.querySelectorAll(
            '.kcfe-ad-upload-video-content, .ant-drawer-content, .ant-modal-content'))
            .filter(h => h.getBoundingClientRect().width > 0);
        for (const h of hosts) {
            for (const txt of ['完成', '关闭', '知道了', '取消']) {
                const b = Array.from(h.querySelectorAll('button, .ant-btn, [role=button]'))
                    .find(x => (x.textContent || '').trim() === txt
                               && x.getBoundingClientRect().width > 0);
                if (b) {
                    const r = b.getBoundingClientRect();
                    return {txt: txt, x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
            }
            const ic = h.querySelector('.ant-drawer-close, .ant-modal-close');
            if (ic && ic.getBoundingClientRect().width > 0) {
                const r = ic.getBoundingClientRect();
                return {txt: 'close-icon', x: r.x + r.width / 2, y: r.y + r.height / 2};
            }
        }
        return null;
    })()""") or None
    if box:
        _click_box(cdp, {"x": box["x"], "y": box["y"]})
        time.sleep(0.8)
        return box.get("txt")
    try:
        cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape",
                 windowsVirtualKeyCode=27)
        cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape",
                 windowsVirtualKeyCode=27)
        time.sleep(0.6)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 工具

def collect_files(files=None, directory=None, ext=None):
    if files:
        picked = [os.path.abspath(f) for f in files]
    elif directory:
        d = os.path.abspath(directory)
        if not os.path.isdir(d):
            raise SystemExit(f"目录不存在: {d}")
        exts = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in (ext or []))
        picked = [os.path.join(d, n) for n in sorted(os.listdir(d))
                  if (not exts or n.lower().endswith(exts))
                  and os.path.isfile(os.path.join(d, n))]
        if not picked:
            raise SystemExit(f"目录里没有匹配文件: {d} (ext={ext})")
    else:
        raise SystemExit("需要 --files 或 --dir")

    missing = [p for p in picked if not os.path.exists(p)]
    if missing:
        raise SystemExit("以下文件不存在:\n  " + "\n  ".join(missing))
    return picked  # Windows 下正斜杠/反斜杠 Chrome 都能识别


def list_pages(port):
    return _local_json(f"http://127.0.0.1:{port}/json/list", timeout=10)


def pick_page(pages, url_match=None, tab_id=None):
    cands = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    if tab_id:
        hit = [p for p in cands if p["id"] == tab_id]
        if not hit:
            raise SystemExit(f"找不到 tab id={tab_id}")
        return hit[0]
    if url_match:
        hit = [p for p in cands if url_match in p.get("url", "")]
        if not hit:
            raise SystemExit(f"没有标签页的 URL 包含 {url_match!r}")
        if len(hit) > 1:
            print(f"[warn] {len(hit)} 个标签页命中，取第一个；可用 --tab-id 指定", file=sys.stderr)
        return hit[0]
    raise SystemExit("需要 --url-match 或 --tab-id（或 --list-tabs）")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="通过 CDP DOM.setFileInputFiles 把本地文件注入浏览器文件输入框",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--url-match", help="按 URL 子串选标签页")
    ap.add_argument("--tab-id")
    ap.add_argument("--list-tabs", action="store_true")
    ap.add_argument("--reload", action="store_true", help="先刷新页面，清除残留的旧弹窗节点")

    ap.add_argument("--open-text", help="注入前点击的文本，用于打开上传框（如『上传视频』）")
    ap.add_argument("--open-selector", help="或用 CSS 选择器打开上传框")
    ap.add_argument("--settle-ms", type=int, default=3000, help="打开上传框后的等待毫秒数")

    ap.add_argument("--selector", default='input[type=file]',
                    help="触发文件选择器的元素（快手传视频填 .kcfe-ad-file-selector）")
    ap.add_argument("--verify-text", help="弹窗标识文本，用于回读已暂存条目（如『视频上传要求』）")

    ap.add_argument("--files", nargs="*")
    ap.add_argument("--dir")
    ap.add_argument("--ext", nargs="*", default=[".mp4"])

    ap.add_argument("--submit-text", help="注入后点击的提交按钮文本（如『上传』）")
    ap.add_argument("--submit-selector")
    ap.add_argument("--cancel-text", default="取消", help="--dry-run 时用于关闭弹窗的按钮文本")
    ap.add_argument("--dry-run", action="store_true", help="只注入并报告，然后取消（不上传）")
    ap.add_argument("--wait-gone", help="提交后轮询等待该选择器消失")
    ap.add_argument("--wait-timeout", type=float, default=600)
    ap.add_argument("--chooser-timeout", type=float, default=15, help="等待文件选择器事件的秒数")
    args = ap.parse_args()

    pages = list_pages(args.port)
    if args.list_tabs:
        for p in pages:
            if p.get("type") == "page":
                print(f"{p['id']}\t{p.get('title','')}\t{p.get('url','')}")
        return 0

    page = pick_page(pages, args.url_match, args.tab_id)
    print(f"[tab] {page.get('title')} -> {page.get('url')}")
    cdp = CDP(page["webSocketDebuggerUrl"])
    try:
        cdp.call("DOM.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        if args.reload:
            print("[reload]")
            cdp.call("Page.reload", ignoreCache=False)
            time.sleep(7)

        if args.open_text:
            print("[open]", click_text(cdp, args.open_text))
        elif args.open_selector:
            print("[open]", click_selector(cdp, args.open_selector))
        if args.open_text or args.open_selector:
            time.sleep(args.settle_ms / 1000.0)

        files = collect_files(args.files, args.dir, args.ext)
        total = sum(os.path.getsize(f) for f in files)
        print(f"[files] {len(files)} 个，合计 {total / 1024 / 1024:.1f} MB")

        # chooser 拦截流程
        cdp.call("Page.setInterceptFileChooserDialog", enabled=True)
        print("[trigger]", trusted_click(cdp, args.selector))

        ev = cdp.wait_event("Page.fileChooserOpened", timeout=args.chooser_timeout)
        if ev:
            print(f"[fileChooserOpened] mode={ev.get('mode')} backendNodeId={ev.get('backendNodeId')}")
            cdp.call("DOM.setFileInputFiles", files=files, backendNodeId=ev["backendNodeId"])
            print("[DOM.setFileInputFiles] OK")
        else:
            # 回退：普通页面可直接对 input 注入
            print("[warn] 未收到 fileChooserOpened，回退为 nodeId 直接注入", file=sys.stderr)
            cdp.call("DOM.getDocument", depth=0)
            root = cdp.call("DOM.getDocument", depth=0)["root"]["nodeId"]
            node = cdp.call("DOM.querySelector", nodeId=root, selector=args.selector).get("nodeId", 0)
            if not node:
                raise RuntimeError(f"找不到文件输入框: {args.selector}")
            cdp.call("DOM.setFileInputFiles", files=files, nodeId=node)
            print(f"[DOM.setFileInputFiles] OK (nodeId={node})")

        time.sleep(3)

        if args.verify_text:
            st = modal_state(cdp, args.verify_text)
            if not st:
                print("[warn] 未定位到弹窗，无法校验暂存结果", file=sys.stderr)
            else:
                print(f"[verify] 弹窗内条目数 = {st['count']}（本次注入 {len(files)} 个）")
                print("[verify] 条目:", json.dumps(st["entries"], ensure_ascii=False))
                print("[verify] 弹窗文本:", st["text"])
                if st["count"] != len(files):
                    print("[warn] 条目数与注入数不一致，请人工确认弹窗", file=sys.stderr)

        if args.dry_run:
            print("[dry-run] 取消，不提交")
            try:
                print("[cancel]", click_text(cdp, args.cancel_text))
            except Exception as e:
                print("[cancel] 跳过:", e)
            return 0

        if args.submit_text:
            print("[submit]", click_text(cdp, args.submit_text))
        elif args.submit_selector:
            print("[submit]", click_selector(cdp, args.submit_selector))

        if args.wait_gone:
            deadline = time.time() + args.wait_timeout
            print(f"[wait] 等待 {args.wait_gone} 消失（最多 {args.wait_timeout}s）")
            while time.time() < deadline:
                cdp.call("DOM.getDocument", depth=0)
                root = cdp.call("DOM.getDocument", depth=0)["root"]["nodeId"]
                n = cdp.call("DOM.querySelector", nodeId=root, selector=args.wait_gone).get("nodeId", 0)
                if not n:
                    print("[wait] 已消失")
                    return 0
                time.sleep(3)
            print(f"[wait] 超时 {args.wait_timeout}s", file=sys.stderr)
            return 2
        return 0
    finally:
        cdp.close()


if __name__ == "__main__":
    sys.exit(main())
