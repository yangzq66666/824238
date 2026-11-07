#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_upload.py — 按「文件夹 -> 账户」映射，批量把视频上传到快手磁力金牛视频库。

底层走 cdp_upload.py 的机制：Page.setInterceptFileChooserDialog + Input.dispatchMouseEvent
可信点击 + DOM.setFileInputFiles（原生 CDP，不经 bsk 暂存层）。

流程（--aggregate，推荐）
----------------------
对每个账户：
  1) navigate 到 material/supervideo?__accountId__=<ID>
  2) **账户回读校验**：回读页面上的账户ID（URL 参数 + SPA 全局状态），与目标不一致就
     一个文件都不传、跳过该账户并记 ACCOUNT_MISMATCH
  3) 把该账户下所有文件夹的待传文件聚合成一批（去重默认关闭，故无翻页扫描）
  4) 超过 100 个自动分批 -> 每批提交前再回读一次账户 -> 注入 -> 提交 -> 等弹窗消失

用法
----
  python batch_upload.py --map "D:/视频/配送员_匹配清单.csv" --root "D:/视频" --aggregate

  # 只跑指定账户（逗号分隔）；也可用 --only-folder / --skip-folder
  python batch_upload.py --map ... --root ... --aggregate --only-account "12345678,23456789"

  # 试运行（注入后取消，不真正上传）；改动后用这个先验证
  python batch_upload.py --map ... --root ... --aggregate --dry-run

  # 需要去重时再开（会翻页扫描，慢；默认关闭）
  python batch_upload.py --map ... --root ... --aggregate --dedupe

  # 不递归子目录（旧行为）：只取文件夹顶层
  python batch_upload.py --map ... --root ... --aggregate --no-recursive

结果 CSV 字段：account / account_name / project / folder / status / files /
skipped / staged / mb / ok_n / fail_n / failed_files / failed_reason / note。

- failed_files：本批上传失败的素材（本地全路径或显示名，分号分隔）。
- failed_reason：与 failed_files 一一对应的失败原因（悬停『原因』链接抓到的文案，
  分号分隔），用于定位不合规素材。两者均以 Markdown 表格形式在汇总里输出。
- ok_n / fail_n 来自页面上的「N个视频上传成功，M个视频上传失败」提示——该提示是瞬时的，
  抓不到就留空；status=UPLOADED 只表示弹窗已关闭，务必以 ok_n/fail_n 为准。
- account_name 是回读到的**页面实际账户名**，用来人工核对有没有传错账户。
  若多个不同 account 的 account_name 相同，说明切账户可能根本没生效。
- status=ACCOUNT_MISMATCH 表示账户回读不一致、一个文件都没传（账户ID 失效或无权限），
  此时别急着重跑，先去核对清单里的账户ID。

注意：以 root 下**现存的**文件夹为准；已删除的文件夹会被跳过（日志 [skip] 目录不存在）。
扫描默认递归子目录，与 match_accounts.py 的统计口径一致（旧版只扫顶层，
会导致清单视频数与实传数不符）。
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request

try:
    import skill_config as _cfgmod
except ImportError:          # 被别的目录调用时回落到无配置
    _cfgmod = None


def _cfg(key, default):
    return _cfgmod.get(key, default) if _cfgmod else default


from cdp_upload import (CDP, click_text, trusted_click, modal_state, list_pages,
                        read_account_context, create_tab, apply_labels,
                        read_effective_tags, scan_failed_items, dismiss_upload_modal)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

LIB_URL = "https://niu.e.kuaishou.com/material/supervideo?__accountId__={}"
UPLOAD_BTN = "上传视频"
DROP_SELECTOR = ".kcfe-ad-file-selector"
MODAL_MARKER = "视频上传要求"

# 页面上「N个视频上传成功，M个视频上传失败」的结果提示（瞬时，弹窗关闭后可能已消失）
RESULT_RE = re.compile(r"(\d+)个视频上传成功，(\d+)个视频上传失败")

# 汇总里视为「正常」的状态；其余都会计入需关注
GOOD_STATUS = ("UPLOADED", "DRYRUN")
NEUTRAL_STATUS = ("SKIPPED", "EMPTY")
CSV_FIELDS = ["account", "account_name", "project", "folder", "status", "files", "skipped",
              "staged", "mb", "ok_n", "fail_n", "failed_files", "failed_reason", "note"]


def log(msg=""):
    print(msg, flush=True)


def split_labels(s):
    """把标签单元格拆成多个标签：支持逗号/顿号/分号/空白分隔。空值返回 []。"""
    if not s:
        return []
    return [x.strip() for x in re.split(r"[,，、;；\s]+", str(s)) if x.strip()]


def list_videos(d, exts, recursive=True):
    """列出目录内的视频文件。

    默认递归，与 match_accounts.py 的 os.walk 口径一致 —— 旧版只扫顶层，
    会导致「匹配清单里 10 个、实际只传 5 个」这种不报错的漏传。
    """
    if recursive:
        out = []
        for root, _, fs in os.walk(d):
            for n in fs:
                if n.lower().endswith(exts):
                    out.append(os.path.join(root, n))
        return sorted(out)
    return [os.path.join(d, n) for n in sorted(os.listdir(d))
            if n.lower().endswith(exts)]


def read_upload_result(cdp):
    """抓页面上的「N个视频上传成功，M个视频上传失败」。

    该提示多为瞬时 toast，弹窗关闭后可能已不存在，所以要在等待弹窗消失的
    过程中反复尝试。抓不到返回 None。
    """
    try:
        txt = cdp.js("(document.body && document.body.innerText) || ''")
    except Exception:
        return None
    if not txt:
        return None
    m = RESULT_RE.search(txt)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------ 映射

def load_map(path, root, only_accounts=None, skip_folders=None, only_folders=None):
    """返回 [(folder_name, abs_dir, account_id, project)]，按账户分组且保持原顺序。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    items = []
    for r in rows:
        folder = (r.get("文件夹") or "").strip()
        account = (r.get("账户ID") or "").strip()
        project = (r.get("匹配项目") or "").strip()
        label = (r.get("标签") or "").strip()
        if not folder or not account:
            continue
        if only_accounts and account not in only_accounts:
            continue
        if skip_folders and folder in skip_folders:
            continue
        if only_folders and folder not in only_folders:
            continue
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            log(f"[skip] 目录不存在: {d}")
            continue
        items.append((folder, d, account, project, label))
    return items


def group_by_account(items):
    groups = {}
    for it in items:
        groups.setdefault(it[2], []).append(it)
    return groups


# ------------------------------------------------------------------ 页面

def wait_lib_ready(cdp, timeout=60):
    """等视频库列表加载完成。

    已知坑：库里 0 素材时 antd 分页器可能不渲染，会走到超时。这里额外接受
    "空状态" 与 "上传视频" 按钮出现作为就绪信号，避免新账户被误判为未就绪。
    """
    end = time.time() + timeout
    while time.time() < end:
        ok = cdp.js("""(() => {
            if (document.querySelector('.ant-pagination')) return true;
            const t = document.body ? (document.body.innerText || '') : '';
            if (t.indexOf('暂无数据') >= 0 || t.indexOf('没有数据') >= 0
                || t.indexOf('暂无素材') >= 0) return true;
            for (const e of document.querySelectorAll('button')) {
                if ((e.textContent || '').indexOf('上传视频') >= 0) return true;
            }
            return false;
        })()""")
        if ok:
            return True
        time.sleep(2)
    return False


def _observed_aids(ctx):
    """取出页面上能读到的所有账户ID证据。"""
    return [x for x in (ctx.get("url_aid"), ctx.get("state_aid")) if x]


def verify_account(cdp, acc, timeout=15, label=""):
    """切换/刷新页面后，确认页面真的是目标账户。

    返回 (verdict, ctx)，verdict 取其一：
      OK       —— 页面上读到的账户ID 与目标一致
      MISMATCH —— 读到的账户ID 与目标不符（SPA 多半还停在别的账户上）
      UNKNOWN  —— 读不到账户ID（页面结构变了 / 被重定向），无法判定

    MISMATCH 是唯一会造成「传到别人库里」的情况，必须阻断点提交。
    UNKNOWN 只告警不阻断：页面改版导致读不到 ID 就全量阻断的话，误伤太大。
    """
    tag = f"{label} " if label else ""
    end = time.time() + timeout
    ctx = {"href": "", "url_aid": None, "state_aid": None, "name": None}
    try:
        while True:
            ctx = read_account_context(cdp)
            got = _observed_aids(ctx)
            if got and all(g == acc for g in got):
                nm = ctx.get("name") or "（未读到账户名）"
                log(f"    {tag}账户回读一致：ID={acc} / 页面账户名「{nm}」")
                return "OK", ctx
            if time.time() >= end:
                break
            time.sleep(1.5)
    except Exception as e:
        log(f"    [warn] {tag}账户回读异常：{type(e).__name__}: {e}")
        return "UNKNOWN", None

    got = _observed_aids(ctx)
    if not got:
        log(f"    [warn] {tag}读不到页面账户ID（href={ctx.get('href', '')[:80]}），"
            f"无法确认是否切到 {acc} —— 继续但会标记 ACCOUNT_UNVERIFIED")
        return "UNKNOWN", ctx
    log(f"    [error] {tag}账户回读不一致：目标 {acc}，页面实际 {got}"
        f"（{ctx.get('name') or '未读到账户名'}）")
    return "MISMATCH", ctx


def guard_account(cdp, acc, label=""):
    """提交前的最后一道保险：单次快速回读，不一致就返回 False。

    与 verify_account 的区别：不循环等待（此时页面状态应该已经稳定），
    成本只有一次 JS 求值，所以在每批提交前都跑一次。
    """
    tag = f"{label} " if label else ""
    try:
        ctx = read_account_context(cdp)
    except Exception as e:
        log(f"    [warn] {tag}提交前账户回读失败（{type(e).__name__}: {e}），不阻断")
        return True
    got = _observed_aids(ctx)
    if got and all(g == acc for g in got):
        return True
    if not got:
        log(f"    [warn] {tag}读不到账户ID，无法确认目标账户（不阻断）")
        return True
    raise RuntimeError(
        f"提交前账户回读不一致：目标 {acc}，页面实际 {got} —— 放弃本批，避免传到别的账户")


def page_names(cdp):
    """当前页的素材名集合。

    读整页 innerText 再按 `xxx.mp4` 切。早先是「叶子元素 textContent 以 .mp4 结尾」收集，
    每页 10 条只认得出 8 条（名字节点里还嵌着子元素），造成 --dedupe 误判：
    实测库内已有 100 个，只认出 80 个，于是 25 个里 20 个被平台以「重复视频」拒——
    白传了 600 多 MB。改成读文本后识别率 100%。
    """
    txt = cdp.js("(document.body.innerText || '')") or ""
    return set(m.group(0) for m in re.finditer(r"[^\s]+?\.mp4", txt))


def collect_library_names(cdp, max_pages=40):
    """翻页采集该账户视频库全部素材名。

    统一用「下一页」按钮从第 1 页往后翻，**不要点页码项**：页数一多，分页会显示成
    「1 2 3 4 5 … 13」，`ant-pagination-item-6` 这样的节点根本不存在，click 静默无效
    也不报错 —— 实测 105 个素材只采到 80 个，--dedupe 因此漏判、把已在库的素材
    又提交了一遍（其中 20 个被平台以「重复视频」拒，白传 600 多 MB）。
    """
    meta = cdp.js("""(() => {
        const p = document.querySelector('.ant-pagination');
        if (!p) return null;
        const m = (p.innerText || '').match(/共\\s*(\\d+)\\s*条/);
        const nums = Array.from(p.querySelectorAll('.ant-pagination-item'))
            .map(e => parseInt((e.innerText || '').trim(), 10)).filter(n => !isNaN(n));
        return {total: m ? +m[1] : 0, pages: nums.length ? Math.max.apply(null, nums) : 1};
    })()""")
    if not meta:
        return page_names(cdp), 1

    # 先回到第 1 页（上次可能停在中途）。页码 1 总是存在，点它安全。
    cdp.js("""(() => {
        const el = document.querySelector('.ant-pagination-item-1');
        if (el) el.click();
    })()""")
    time.sleep(1.8)

    names, pages = set(), 0
    while pages < max_pages:
        got = page_names(cdp)
        names |= got
        pages += 1
        if meta["total"] and len(names) >= meta["total"]:
            break
        nxt = cdp.js("""(() => {
            const b = document.querySelector('.ant-pagination-next');
            if (!b) return 'no';
            if (String(b.className).indexOf('disabled') >= 0) return 'disabled';
            b.click(); return 'ok';
        })()""")
        if nxt != "ok":
            break
        # 等列表真的翻过去：固定 sleep 会偶尔读到上一页（实测 105 个只采到 85 个）
        for _ in range(8):
            time.sleep(1.0)
            now = page_names(cdp)
            if now and now != got:
                break
    if meta["total"] and len(names) < meta["total"] * 0.95:
        # 列表每页实际渲染 10 条，但只有 8 条的名字在文本里（另外两条读不到）。
        # 采集不全 = 去重漏判 = 已在库的素材又被提交一次（会被平台以「重复视频」拒，白传带宽）。
        log(f"    [warn] 去重扫描只采到 {len(names)}/{meta['total']} 个素材名"
            f"（列表页渲染不全），可能重复提交已在库的素材")
    return names, pages


def count_residue(cdp):
    return cdp.js(f"document.querySelectorAll({DROP_SELECTOR!r}).length") or 0


def upload_one(cdp, files, dry_run=False, chooser_timeout=15, settle_ms=3000,
               labels=None, allow_create_labels=True):
    """把一个文件夹的文件注入并提交。返回结果 dict。

    labels: 该账户需统一打的标签（list[str]）；allow_create_labels=False 时只勾选已有、
    不新建（dry-run 用，避免污染账户标签集）。
    """
    total = sum(os.path.getsize(f) for f in files)
    log(f"    打开上传弹窗 ...")
    click_text(cdp, UPLOAD_BTN)
    time.sleep(settle_ms / 1000.0)

    cdp.call("Page.setInterceptFileChooserDialog", enabled=True)
    pos = trusted_click(cdp, DROP_SELECTOR)
    log(f"    可信点击 {DROP_SELECTOR} @ {pos}")

    ev = cdp.wait_event("Page.fileChooserOpened", timeout=chooser_timeout)
    if not ev:
        raise RuntimeError("未收到 Page.fileChooserOpened，无法注入")
    log(f"    fileChooserOpened mode={ev.get('mode')} backendNodeId={ev.get('backendNodeId')}")

    cdp.call("DOM.setFileInputFiles", files=files, backendNodeId=ev["backendNodeId"])
    log(f"    DOM.setFileInputFiles OK（{len(files)} 个 / {total / 1024 / 1024:.1f} MB）")
    time.sleep(3)

    st = modal_state(cdp, MODAL_MARKER)
    staged = st["count"] if st else -1
    log(f"    弹窗条目数 = {staged}（本次注入 {len(files)} 个）")
    if st:
        log(f"    条目: {json.dumps(st['entries'][:12], ensure_ascii=False)}")

    if staged < 0:
        # 定位不到弹窗 = 校验没做成。仍尝试提交，但结果里标记为未校验。
        log("    [warn] 未能定位弹窗，未做暂存校验（结果里会标 UPLOAD_UNVERIFIED）")
        unverified = True
    else:
        unverified = False

    # 注入校验（dry-run 与正式提交都执行）：弹窗内 0 条目 = 注入未生效。
    # 必须在 dry_run 分支之前判断，否则试运行会绕过校验、把红灯当绿灯。
    if staged == 0:
        # 注入没生效。此时点「上传」弹窗会直接关闭，等待弹窗消失会被判成成功，
        # 实际一个视频都没传 —— 最危险的静默失败，必须拦在这里。
        log("    [error] 弹窗内 0 个条目，注入未生效，放弃提交")
        try:
            click_text(cdp, "取消")
        except Exception:
            pass
        raise RuntimeError(
            f"注入未生效：弹窗内 0 个条目（应注入 {len(files)} 个），已取消不提交")

    if staged > 0 and staged != len(files):
        log(f"    [warn] 条目数({staged})与注入数({len(files)})不一致，仍继续提交", )

    # ---- 提交前加标签
    # dry-run 也走真实交互（打开标签弹窗、勾选已有标签、点确定），但 allow_create=False
    # 不新建标签；随后整个上传弹窗会被取消，勾选结果一并丢弃，故不污染账户标签集。
    if labels:
        applied, created, missing = apply_labels(
            cdp, labels, allow_create=allow_create_labels)
        log(f"    标签：已应用 {sorted(applied)}"
            + (f"，新建 {sorted(created)}" if created else "")
            + (f"；未加上 {sorted(missing)}" if missing else ""))
        if missing:
            log("    [warn] 部分标签未加上（弹窗结构变化 / 标签超限 / 名称含特殊字符？"
                + ("（dry-run 不新建标签，正式上传时才会新建）" if dry_run else "") + "）")
        if created:
            log(f"    [info] 新建了标签 {sorted(created)}（该批上传成功后会留在账户标签集）")
        # 回读标签区：真正生效的标签会显示在「标签」栏里
        seen = read_effective_tags(cdp)
        if seen:
            log(f"    标签区回读：{seen[:150]}")

    if dry_run:
        try:
            click_text(cdp, "取消")
        except Exception as e:
            log(f"    取消失败: {e}")
        return {"staged": staged, "files": len(files), "bytes": total,
                "submitted": False, "unverified": unverified}

    click_text(cdp, "上传")
    log("    已点「上传」，等待上传完成 ...")
    return {"staged": staged, "files": len(files), "bytes": total,
            "submitted": True, "unverified": unverified}


def _probe_light(cdp):
    """上传期间的轻量探测：一次 JS 往返，只做短路式 querySelector。

    上传中唯一需要知道的就是「还在传吗」。原先每 3s 做一遍重活——遍历上百张卡片、
    读每张卡的 innerText、再读一次 `document.body.innerText`（整页重排）——会和上传
    抢渲染主线程，大批量时实测吞吐从 8.45 掉到 5.3 MB/s，怀疑与此有关。
    这里只查三样轻量信号，卡片一概不碰：

      busy     抽屉内是否存在可见的进度条/加载图标（querySelector 命中即 break）
      settle   抽屉内是否出现「完成/关闭/知道了」
      toast    「N个视频上传成功，M个视频上传失败」（只在候选 toast 容器里读文本）

    ⚠️ busy 必须限定在上传抽屉内查询。页面上常驻一个
    `ant-spin-nested-loading index-module__spin___xxx` 的表格工具条（1032×65、一直可见），
    它的 class 含 loading，全局查 `[class*="loading"]` 会一直判「还在传」→ 干等到超时。
    toast 故意不做整页兜底：容器里没有就返回空，由完成时的一次整页扫描补抓。
    """
    try:
        return cdp.js("""(() => {
            const vis = e => {
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const d = document.querySelector('.kcfe-ad-upload-video-content');
            let busy = false, settle = false;
            if (d) {
                const p = d.querySelectorAll(
                    '.ant-progress, [class*="progress"], [class*="Progress"],'
                    + '.anticon-loading, .ant-spin-spinning');
                for (let i = 0; i < p.length; i++) {
                    if (vis(p[i])) { busy = true; break; }
                }
                const bs = d.querySelectorAll('button, .ant-btn, [role=button]');
                for (let i = 0; i < bs.length; i++) {
                    const t = (bs[i].textContent || '').trim();
                    if ((t === '完成' || t === '关闭' || t === '知道了') && vis(bs[i])) {
                        settle = true; break;
                    }
                }
            }
            let toast = '';
            const ts = document.querySelectorAll(
                '.ant-message-notice, .ant-message, [class*="toast"], [class*="Toast"],'
                + '[class*="message-notice"], [class*="notification"]');
            for (let i = 0; i < ts.length; i++) {
                const t = ts[i].innerText || '';
                if (t.indexOf('个视频上传') >= 0) { toast = t; break; }
            }
            return {busy: busy, settle: settle, toast: toast, hasDrawer: !!d};
        })()""") or {}
    except Exception:
        return {}


def _drawer_state(cdp):
    """抽屉的粗状态：卡片总数 / 失败数。只在「没有进度」时才调用。

    全部失败时（0 成功 N 失败）平台的「N个视频上传成功，M个失败」toast 一闪就没，
    也不会出现「完成」按钮——靠「失败卡片已出现 + 状态两次不变」兜底。

    签名只用 (总数, 失败数)：文本签名对判断「有没有变化」没有额外信息，而读抽屉
    innerText 在百张卡片时很贵。失败判定先看卡片 class（便宜），再退回 innerText。
    """
    return cdp.js("""(() => {
        const d = document.querySelector('.kcfe-ad-upload-video-content');
        if (!d) return {total: 0, failed: 0, sig: ''};
        const cards = Array.from(d.querySelectorAll(
                '.kcfe-ad-upload-video-content-content-list-item'))
            .filter(c => c.querySelector('video, img'));   // 末尾的「选择视频」卡片没有缩略图，不算
        let failed = 0;
        for (const c of cards) {
            if (/fail|error/i.test(String(c.className || ''))
                || (c.innerText || '').indexOf('视频加载失败') >= 0) failed++;
        }
        return {total: cards.length, failed: failed,
                sig: cards.length + '|' + failed};
    })()""") or {"total": 0, "failed": 0, "sig": ""}


def recover_failed_names(failed_list, files):
    """把弹窗里失败素材的显示名（可能截断 / 也可能已是完整名）回匹配到本地全路径。

    failed_list: scan_failed_items 的返回，list[dict{name, reason}]。
    返回 list[dict{name, reason, path}]，path 为匹配到的本地全路径；匹配不上用显示名兜底。
    """
    bases = [(os.path.basename(f), os.path.splitext(os.path.basename(f))[0], f) for f in files]
    out = []
    for item in (failed_list or []):
        name = (item.get("name") or "").strip()
        reason = (item.get("reason") or "").strip()
        matched = None
        # 1) 直接全名 / 基名匹配（完整名悬停已拿到原名时）
        for base, base_noext, full in bases:
            if name == base or name == base_noext:
                matched = full
                break
        # 2) 截断名：按 '...' 切前缀/后缀回匹配
        if not matched and "..." in name:
            pre, suf = name.split("...", 1)
            pre, suf = pre.strip(), suf.strip()
            for base, base_noext, full in bases:
                if pre and base_noext.startswith(pre) and (not suf or base_noext.endswith(suf)):
                    matched = full
                    break
        out.append({"name": name, "reason": reason, "path": matched or name})
    return out


def settle_after_submit(cdp, timeout=900, poll_busy=6, poll_idle=2, expected=None):
    """提交后等上传完成（两段式轮询，2026-10-27 优化）。

    阶段 A（每轮都做，极轻）：`_probe_light` 只看「还在传吗」——进度条/加载图标。
        还在传就睡 poll_busy（默认 6s），完全不碰卡片、不读整页文本。
    阶段 B（只有确认不忙时才做）：数卡片、数失败、看结束按钮，间隔 poll_idle（默认 2s）。

    完成判据（任一成立）：
      1. 抓到「N个视频上传成功，M个视频上传失败」toast；
      2. 抽屉里出现「完成/关闭/知道了」；
      3. 抽屉自己关掉了（之前见过抽屉 + 连续两次探测不到）——成功时平台会自己收抽屉；
      4. 已出现失败卡片、且 (卡片数, 失败数) 连续两次不变。
         这是给「全部失败（0 成功）」准备的：那种情况 toast 一闪就没、也没有完成按钮。

    判定完成时再做一次整页文本扫描（仅此一次），把轮询期间可能漏掉的 toast 抓回来；
    随后抓失败素材（完整名 + 原因），关掉抽屉继续下一批。

    返回 (ok, result, failed_list, seconds)：
      ok              是否判为完成（False=超时）
      result          (成功数, 失败数) 或 None（toast 没抓到）
      failed_list     scan_failed_items 的返回，list[dict{name, reason}]
    """
    t0 = time.time()
    end = t0 + timeout
    result, st = None, {"total": 0, "failed": 0, "sig": ""}
    last_sig, stable, gone_hits = None, 0, 0
    seen_drawer = False
    done = False
    grace_left = 3          # 卡片数不足注入数时允许的宽限轮数

    while time.time() < end:
        p = _probe_light(cdp)
        m = RESULT_RE.search(p.get("toast") or "")
        if m:
            result = (int(m.group(1)), int(m.group(2)))
            done = True
        elif p.get("busy"):
            # 还在传：本轮到此为止，不去数卡片
            time.sleep(poll_busy)
            continue
        else:
            # 不忙了，做一次完整查询
            st = _drawer_state(cdp)
            if p.get("hasDrawer"):
                seen_drawer, gone_hits = True, 0
            elif seen_drawer:
                gone_hits += 1
            if p.get("settle"):
                done = True
            elif st["failed"] and st["sig"] == last_sig:
                stable += 1
                if stable >= 2:
                    done = True
            else:
                stable = 0
            if st["sig"]:
                last_sig = st["sig"]
            if not done and gone_hits >= 2:
                done = True          # 抽屉已经自己关掉了
            if (done and result is None and expected
                    and st["total"] and st["total"] < expected and grace_left > 0):
                # 卡片数还没到注入数：多半只是渲染没跟上，别急着结算
                grace_left -= 1
                done = False
        if done:
            break
        time.sleep(poll_idle)

    dt = time.time() - t0
    if done and result is None:
        # 轮询只查候选 toast 容器，这里补一次整页扫描（只此一次，不影响上传期性能）
        try:
            result = read_upload_result(cdp) or result
        except Exception:
            pass
    failed = scan_failed_items(cdp)
    if done:
        if result:
            ok_n, fail_n = result
        else:
            # toast 丢了：用抽屉卡片数反推（总数 - 失败数 = 成功数）
            fail_n = len(failed) or st["failed"]
            ok_n = max(0, st["total"] - fail_n) if st["total"] else "?"
        flog = "；".join(f"{x['name']}（{x['reason'] or '原因未捕获'}）" for x in failed) \
            if failed else ""
        log("    上传完成（用时 {:.0f}s）：成功 {} / 失败 {}".format(dt, ok_n, fail_n)
            + (f"，失败素材：{flog}" if flog else ""))
        dismiss_upload_modal(cdp)
        return True, (ok_n, fail_n), failed, dt

    flog = "；".join(f"{x['name']}（{x['reason'] or '原因未捕获'}）" for x in failed) \
        if failed else ""
    log("    弹窗超时未关闭（{:.0f}s），页面结果：{}".format(
        dt, f"成功 {result[0]} / 失败 {result[1]}" if result else "未抓到结果提示")
        + (f"；失败素材：{flog}" if flog else ""))
    return False, result, failed, dt


# ------------------------------------------------------------------ 单个账户

def process_account(cdp, acc, its, results, base, args, exts, recursive):
    """跑完一个账户，把结果写回 results[base:] 的占位行。

    异常一律向上抛，由调用方兜底标记 FAILED —— 这样已跑完的账户不受影响，
    汇总也一定会在 finally 里落盘。
    """
    cur = [base]        # 单元素 list，让内层 record() 能推进游标
    ctx_name = [""]     # 账户回读拿到的页面账户名，写进每行结果供人工核对
    ctx_note = [""]     # 账户层面统一的备注后缀（如 ACCOUNT_UNVERIFIED）

    # 该账户需要统一打的标签（来自匹配清单「标签」列，支持多标签/分隔符）
    account_labels = []
    for it in its:
        if len(it) > 4:
            account_labels.extend(split_labels(it[4]))
    account_labels = list(dict.fromkeys(account_labels))  # 去重保序
    if account_labels:
        log(f"  [标签] 本账户将统一打标签：{account_labels}")

    def record(folder, project, todo, skipped, status, staged, mb,
               note="", ok_n="", fail_n="", failed_files="", failed_reason="",
               account_name=None):
        note = str(note or "")
        if ctx_note[0]:
            note = (note + "；" if note else "") + ctx_note[0]
        results[cur[0]] = {"folder": folder, "account": acc, "project": project,
                           "account_name": ctx_name[0] if account_name is None
                           else account_name,
                           "status": status, "files": len(todo), "skipped": skipped,
                           "staged": staged, "mb": round(mb, 1),
                           "ok_n": ok_n, "fail_n": fail_n,
                           "failed_files": failed_files, "failed_reason": failed_reason,
                           "note": note}
        cur[0] += 1

    def clear_residue():
        if count_residue(cdp) > 0:
            log("    检测到残留弹窗节点，刷新页面 ...")
            cdp.call("Page.reload", ignoreCache=False)
            time.sleep(7)
            wait_lib_ready(cdp)
            time.sleep(2)
            # 刷新后账户可能弹回默认账户，必须重新确认再继续
            v, c = verify_account(cdp, acc, timeout=args.account_check_timeout,
                                  label="刷新后")
            if v == "MISMATCH":
                raise RuntimeError(
                    f"刷新后账户回读不一致：目标 {acc}，页面实际 {_observed_aids(c)}")
            if v == "OK" and c and c.get("name"):
                ctx_name[0] = c["name"]

    def run_batch(batch):
        """注入并提交一批，返回 (ok, staged, result, unverified, failed_files)。

        failed_files 为本批上传后「失败」的素材本地全路径列表（显示名回匹配得到）。
        """
        mb = sum(os.path.getsize(f) for f in batch) / 1048576
        log(f"    本批 {len(batch)} 个 / {mb:.1f} MB")
        clear_residue()
        if not args.no_account_check:
            # 最后一道保险：点「上传」之前再确认一次是不是目标账户
            guard_account(cdp, acc, label="提交前")
        r = upload_one(cdp, batch, dry_run=args.dry_run, settle_ms=args.settle_ms,
                       labels=account_labels, allow_create_labels=(not args.dry_run))
        if r["submitted"]:
            ok, result, failed_list, _dt = settle_after_submit(
                cdp, timeout=args.wait_timeout,
                poll_busy=args.poll_busy, poll_idle=args.poll_idle,
                expected=r["staged"] or len(batch))
            failed = recover_failed_names(failed_list, batch)
            return ok, r["staged"], result, r.get("unverified", False), failed
        return True, r["staged"], None, r.get("unverified", False), []

    # ---- 导航到该账户的视频库
    cdp.call("Page.navigate", url=LIB_URL.format(acc))
    time.sleep(6)
    if not wait_lib_ready(cdp):
        log("  [error] 视频库未就绪，跳过该账户")
        for it in its:
            record(it[0], it[3], [], 0, "ACCOUNT_NOT_READY", -1, 0,
                   "视频库未加载（新账户且库里 0 素材时也可能报这个）")
        return

    # ---- 账户回读：确认页面真的切到了这个账户
    # 账户ID 无效 / 无权限时，SPA 不会报错，只是继续显示上一个账户的视频库。
    # 不校验的话视频会静默传到别人库里，而结果照样记 UPLOADED。
    if args.no_account_check:
        log("    [warn] 已用 --no-account-check 跳过账户回读，风险自负")
    else:
        verdict, ctx = verify_account(cdp, acc, timeout=args.account_check_timeout)
        if ctx and ctx.get("name"):
            ctx_name[0] = ctx["name"]
        if verdict == "MISMATCH":
            log("  [error] 账户回读不一致 —— 一个文件都不传，直接跳过该账户")
            for it in its:
                record(it[0], it[3], [], 0, "ACCOUNT_MISMATCH", -1, 0,
                       f"页面账户ID {_observed_aids(ctx)} ≠ 目标 {acc}"
                       f"（多为账户ID 无效或无权限，SPA 停在别的账户上）")
            return
        if verdict == "UNKNOWN":
            ctx_note[0] = "ACCOUNT_UNVERIFIED"
            log("  [warn] 未能确认账户身份，继续但结果会标 ACCOUNT_UNVERIFIED，请人工核对")

    time.sleep(2)
    if args.dedupe:
        existing, pages_n = collect_library_names(cdp, args.max_pages)
        log(f"  库内已有素材 {len(existing)} 个（扫描 {pages_n} 页）")
    else:
        existing = set()
        log("  去重：默认关闭（直接上传，重复素材由平台侧拒绝或覆盖）")

    # ---- 收集各文件夹待传文件
    plans = []  # (folder, project, allf, todo)
    for it in its:
        folder, d, _, project, _label = it
        allf = list_videos(d, exts, recursive)
        if not allf:
            log(f"  - {folder}: 目录内无 {list(exts)} 文件，跳过")
            plans.append((folder, project, allf, []))
            continue
        todo = [f for f in allf
                if not (args.dedupe and os.path.basename(f) in existing)]
        skipped = len(allf) - len(todo)
        plans.append((folder, project, allf, todo))
        if todo:
            mb = sum(os.path.getsize(f) for f in todo) / 1048576
            log(f"  - {folder}: 待传 {len(todo)}/{len(allf)}（跳过重复 {skipped}），"
                f"{mb:.1f} MB")
        else:
            log(f"  - {folder}: {len(allf)} 个全部已在库中，跳过")
    log()

    if args.aggregate:
        # ---- 账户聚合：合并所有文件夹的待传文件，按 100/批分批
        merged = [f for _, _, _, todo in plans for f in todo]
        if not merged:
            for folder, project, allf, todo in plans:
                st = "EMPTY" if not allf else "SKIPPED"
                record(folder, project, todo, len(allf) - len(todo), st, 0, 0,
                       "无匹配文件" if st == "EMPTY" else "库内已存在")
            return
        total_mb = sum(os.path.getsize(f) for f in merged) / 1048576
        batches = [merged[i:i + 100] for i in range(0, len(merged), 100)]
        log(f"  [aggregate] 合并待传 {len(merged)} 个 / {total_mb:.1f} MB，"
            f"分 {len(batches)} 批（≤100 个/批）")
        all_ok = True
        sum_ok = sum_fail = None
        # 多批时暂存数要**累加**：只记最后一批会让 CSV 出现「传104个 / 暂存4」的误导值
        sum_staged, any_staged = 0, False
        unverified = False
        all_failed = []   # 本账户所有批次的失败素材（本地全路径）
        for bi, batch in enumerate(batches, 1):
            log(f"  [batch {bi}/{len(batches)}]")
            try:
                ok, staged, result, uv, failed = run_batch(batch)
                all_ok = all_ok and ok
                if isinstance(staged, int) and staged >= 0:
                    sum_staged += staged
                    any_staged = True
                unverified = unverified or uv
                all_failed.extend(failed)
                if result:
                    o, f = result
                    sum_ok = (sum_ok or 0) + o
                    sum_fail = (sum_fail or 0) + f
                if ok:
                    existing |= set(os.path.basename(f) for f in batch)
            except Exception as e:
                log(f"    [error] {e}")
                all_ok = False

        # dry-run 单独记成 DRYRUN，别混进 UPLOADED —— 否则试运行看着跟真传了一样
        final_st = ("DRYRUN" if all_ok else "PARTIAL/FAILED") if args.dry_run \
            else ("UPLOADED" if all_ok else "PARTIAL/FAILED")
        note = "" if all_ok else "聚合批次部分失败，见日志"
        if all_ok and unverified:
            note = (note + "；UPLOAD_UNVERIFIED").strip("；")
        # 失败里可能有回匹配不到任何待传文件的（名字残缺），这些也得报出来，
        # 否则会「有失败但表里看不到」。统一挂到第一个文件夹行上。
        all_todo = set()
        for _f, _p, _a, _td in plans:
            all_todo.update(_td)
        orphan_failed = [x for x in all_failed if x.get("path") not in all_todo]
        if orphan_failed:
            log(f"    [warn] {len(orphan_failed)} 条失败素材回匹配不到本地文件，"
                f"已挂到第一个文件夹行：{[x['path'] for x in orphan_failed][:3]}")

        # 本账户各批次的暂存条目总数（没有一批读到就是 -1）
        staged_rec = sum_staged if any_staged else -1

        for pi, (folder, project, allf, todo) in enumerate(plans):
            if not allf:
                record(folder, project, todo, 0, "EMPTY", 0, 0, "无匹配文件")
            elif not todo:
                record(folder, project, todo, len(allf), "SKIPPED", 0, 0, "库内已存在")
            else:
                mb = sum(os.path.getsize(f) for f in todo) / 1048576
                # 失败素材只摊给真正归属的文件夹：聚合时一批混了多个文件夹的文件，
                # 不筛的话每个文件夹行都会把整批失败素材重复列一遍（汇总表格翻倍）。
                mine = [x for x in all_failed if x.get("path") in set(todo)]
                if pi == 0:
                    mine = mine + orphan_failed
                if args.dry_run:
                    o_, f_ = "", ""
                elif all_ok:
                    f_ = len(mine)
                    o_ = max(0, len(todo) - f_)
                else:
                    o_ = "" if sum_ok is None else sum_ok
                    f_ = "" if sum_fail is None else sum_fail
                record(folder, project, todo, len(allf) - len(todo), final_st,
                       staged_rec, mb, note, ok_n=o_, fail_n=f_,
                       failed_files=";".join(x["path"] for x in mine),
                       failed_reason=";".join(x["reason"] for x in mine if x.get("reason")))
    else:
        # ---- 逐文件夹上传
        for folder, project, allf, todo in plans:
            skipped = len(allf) - len(todo)
            mb = sum(os.path.getsize(f) for f in todo) / 1048576
            if not allf:
                record(folder, project, todo, 0, "EMPTY", 0, 0, "无匹配文件")
                continue
            if not todo:
                record(folder, project, todo, skipped, "SKIPPED", 0, 0, "库内已存在")
                continue
            try:
                ok, staged, result, uv, failed = run_batch(todo)
                if args.dry_run:
                    st = "DRYRUN" if ok else "PARTIAL/FAILED"
                else:
                    st = "UPLOADED" if ok else "SUBMIT_TIMEOUT"
                note = "UPLOAD_UNVERIFIED" if (ok and uv) else ""
                o, f = result if result else ("", "")
                failed_files = ";".join(x["path"] for x in failed)
                failed_reason = ";".join(x["reason"] for x in failed if x.get("reason"))
                record(folder, project, todo, skipped, st, staged, mb, note,
                       ok_n=o, fail_n=f, failed_files=failed_files,
                       failed_reason=failed_reason)
                if ok:
                    existing |= set(os.path.basename(f) for f in todo)
            except Exception as e:
                log(f"    [error] {e}")
                record(folder, project, todo, skipped, "FAILED", -1, mb, str(e)[:120])


# ------------------------------------------------------------------ 汇总

def emit_summary(results, crash=None, out=None):
    """汇总并写结果 CSV。

    刻意放在 finally 里调用：中途任何异常都不会让已跑完的进度蒸发。
    out 为用户用 --out 指定的结果文件位置（目录或完整 .csv 路径）；
    为 None 时落到当前工作目录。结果文件**绝不**写进脚本所在目录（scripts/），
    以免污染 skill 安装包、或触发本机 Security Center 对安装目录的写入拦截。
    """
    log("\n================ 汇总 ================")
    if crash:
        log(f"[!] 任务未跑完就结束了：{crash}")
        log("    下面只含已完成的账户；状态为 RUNNING 的是中断时正在处理的那个。")

    ok = sum(1 for r in results if r["status"] in GOOD_STATUS)
    bad = [r for r in results
           if r["status"] not in GOOD_STATUS + NEUTRAL_STATUS]
    for r in results:
        flag = "OK " if r["status"] in GOOD_STATUS else "!! "
        cnt = ""
        if r.get("ok_n") not in ("", None) or r.get("fail_n") not in ("", None):
            cnt = f"成功{r.get('ok_n')}/失败{r.get('fail_n')} "
        nm = (r.get("account_name") or "").strip()
        ff = (r.get("failed_files") or "").strip()
        log(f"{flag}{r['account']}  {r['folder']:<34} {r['status']:<16} "
            f"传{r['files']}/跳{r['skipped']} 暂存{r['staged']} {r['mb']}MB "
            f"{cnt}{r['note']}"
            + (f"  页面账户名「{nm}」" if nm else "")
            + (f"\n        失败素材: {ff}" if ff else ""))
    log(f"\n成功 {ok} / 共 {len(results)} 个文件夹")
    if bad:
        log(f"需关注 {len(bad)} 个：" + ", ".join(f"{r['folder']}({r['status']})" for r in bad))
    unver = [r for r in results if "UNVERIFIED" in (r.get("note") or "")]
    if unver:
        log(f"[提示] {len(unver)} 个条目没做成暂存校验，请以库内实际数量为准")

    mism = [r for r in results if r["status"] == "ACCOUNT_MISMATCH"]
    if mism:
        log(f"[!!] {len(mism)} 个条目因账户回读不一致被拦下（未上传任何文件），"
            f"多半是账户ID 失效或当前登录号无权限，请核对清单里的账户ID")

    # 失败素材清单：输出 Markdown 表格（素材名称 + 失败原因），方便后期处理不合规文件
    failed_rows = [r for r in results if (r.get("failed_files") or "").strip()]
    if failed_rows:
        md_rows, seen_md = [], set()
        for r in failed_rows:
            names = [x for x in (r.get("failed_files") or "").split(";") if x.strip()]
            reasons = [x for x in (r.get("failed_reason") or "").split(";") if x.strip()]
            for i, nm in enumerate(names):
                rs = reasons[i] if i < len(reasons) else ""
                base = os.path.basename(nm.strip())      # 表格里只要文件名，全路径留在 CSV
                key = (r["account"], r["folder"], base, rs)
                if key in seen_md:
                    continue
                seen_md.add(key)
                md_rows.append((r["account"], r["folder"], base, rs))
                log(f"   {r['account']} / {r['folder']}: {base}"
                    + (f"  —— 原因：{rs}" if rs else ""))
        # 以 Markdown 表格形式输出（素材名称 + 失败原因）
        log("\n[失败素材 Markdown 表格]\n")
        log("| 账户ID | 文件夹 | 素材名称 | 失败原因 |")
        log("| --- | --- | --- | --- |")
        for acc, fol, nm, rs in md_rows:
            log(f"| {acc} | {fol} | {nm} | {rs} |")
        # 另写一份 .md 文件，便于复制
        md_dir = out or os.getcwd()
        if os.path.splitext(str(md_dir))[1].lower() == ".csv":
            md_dir = os.path.dirname(str(md_dir)) or os.getcwd()
        try:
            os.makedirs(md_dir, exist_ok=True)
            md_path = os.path.join(md_dir, time.strftime("失败素材_%Y%m%d_%H%M%S.md"))
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("# 上传失败素材清单\n\n")
                f.write("> 部分失败不影响同批其它素材上传；下表为上传失败的素材及其原因。\n\n")
                f.write("| 账户ID | 文件夹 | 素材名称 | 失败原因 |\n")
                f.write("| --- | --- | --- | --- |\n")
                for acc, fol, nm, rs in md_rows:
                    f.write(f"| {acc} | {fol} | {nm} | {rs} |\n")
            log(f"\n失败素材 Markdown 已写入: {md_path}")
        except OSError as e:
            log(f"[warn] 写失败素材 Markdown 失败：{e}")

    # 多个不同账户ID 却读到同一个页面账户名 = 切账户根本没生效，全部传到了同一个库
    by_name = {}
    for r in results:
        n = (r.get("account_name") or "").strip()
        if n:
            by_name.setdefault(n, set()).add(r["account"])
    dup = {n: a for n, a in by_name.items() if len(a) > 1}
    if dup:
        log("[!!] 疑似切账户未生效：以下页面账户名对应多个不同账户ID，务必人工核对：")
        for n, a in sorted(dup.items()):
            log(f"     「{n}」← 账户 {', '.join(sorted(a))}")

    # 写盘：结果文件落在「--out 指定的位置」（目录或完整 .csv 路径），
    # 未指定时落到「当前工作目录」。刻意不写进脚本所在目录（scripts/），
    # 以免污染 skill 安装包、或触发本机 Security Center 对安装目录的写入拦截。
    name = time.strftime("上传结果_%Y%m%d_%H%M%S.csv")
    if out and os.path.splitext(out)[1].lower() == ".csv":
        candidates = [out]                            # 用户给了完整文件名
    elif out:
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            log(f"[warn] 无法创建目录 {out}：{e}")
        candidates = [os.path.join(out, name)]        # 用户给了目录
    else:
        candidates = [os.path.join(os.getcwd(), name)]  # 默认当前工作目录
    for out_path in candidates:
        try:
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(results)
            log(f"结果已写入: {out_path}")
            break
        except OSError as e:
            log(f"[warn] 写盘失败（{out_path}）：{e}")
    else:
        log("[error] 结果未落盘——下面是原始结果，可直接复制：")
        log(",".join(CSV_FIELDS))
        for r in results:
            log(",".join(str(r.get(k, "")) for k in CSV_FIELDS))

    if crash:
        return 1
    return 0 if not bad else 2


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(
        description="按映射清单批量上传视频到快手视频库",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--map", required=True, help="匹配清单 CSV（含 文件夹/账户ID 列）")
    ap.add_argument("--root", default=_cfg("videoRoot", "D:/视频"),
                    help="文件夹所在根目录（默认取 config.json 的 videoRoot）")
    ap.add_argument("--port", type=int, default=int(_cfg("chromePort", 9222)))
    ap.add_argument("--tab-id", help="复用指定标签页（仅用于第一个账户）；"
                                   "其余账户各自开独立标签页，跑完保留供查看")
    ap.add_argument("--only-account", help="只处理这些账户ID，逗号分隔")
    ap.add_argument("--only-folder", help="只处理这些文件夹，逗号分隔")
    ap.add_argument("--skip-folder", help="跳过这些文件夹，逗号分隔")
    ap.add_argument("--ext", nargs="*", default=[".mp4"])
    ap.add_argument("--out", default=None,
                   help="结果 CSV 写出位置：目录或完整 .csv 路径。"
                        "默认落到当前工作目录，绝不写进 skill 安装目录")
    ap.add_argument("--no-recursive", action="store_true",
                    help="只扫文件夹顶层、不进子目录（默认递归，与 match_accounts.py 口径一致）")
    ap.add_argument("--dedupe", action="store_true",
                    help="上传前扫描库内已有素材名、跳过同名文件（需翻页，慢；默认关闭）")
    ap.add_argument("--max-pages", type=int, default=40,
                    help="--dedupe 时最多翻多少页（默认 40）")
    ap.add_argument("--aggregate", action="store_true",
                    help="按账户聚合：该账户下所有文件夹的待传文件合并，一次注入一批上传"
                         "（平台单批上限 100 个，超出自动分批）")
    ap.add_argument("--dry-run", action="store_true", help="注入后取消，不真正上传")
    ap.add_argument("--settle-ms", type=int, default=3000)
    ap.add_argument("--wait-timeout", type=float, default=900,
                    help="提交后等待上传完成的秒数（部分失败时弹窗不自动关闭，到时主动关）")
    ap.add_argument("--poll-busy", type=float, default=6,
                    help="上传中（有进度条）的探测间隔秒数，默认 6；上传中没有需要频繁查的东西")
    ap.add_argument("--poll-idle", type=float, default=2,
                    help="疑似传完（无进度条）时的探测间隔秒数，默认 2")
    ap.add_argument("--no-account-check", action="store_true",
                    help="关掉账户回读校验（不推荐：账户ID 无效时视频会传到别的账户且不报错）")
    ap.add_argument("--account-check-timeout", type=float, default=15,
                    help="切换账户后等待页面账户ID 对上来的秒数（默认 15）")
    args = ap.parse_args()

    def split(s):
        return set(x.strip() for x in s.split(",") if x.strip()) if s else None

    items = load_map(args.map, args.root,
                     only_accounts=split(args.only_account),
                     skip_folders=split(args.skip_folder),
                     only_folders=split(args.only_folder))
    if not items:
        log("没有待处理项")
        return 1

    # ext 允许写成 "mp4" 或 ".mp4"，统一规范化
    exts = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in args.ext)
    recursive = not args.no_recursive

    groups = group_by_account(items)
    total_files = 0
    for acc, its in groups.items():
        for it in its:
            total_files += len(list_videos(it[1], exts, recursive))
    log(f"[plan] {len(groups)} 个账户 / {len(items)} 个文件夹 / 本地共 {total_files} 个视频"
        f"（扫描模式：{'递归子目录' if recursive else '仅顶层'}）"
        f"{'（dry-run，不提交）' if args.dry_run else ''}")
    for acc, its in groups.items():
        log(f"   - 账户 {acc} ({its[0][3]}): " + ", ".join(i[0] for i in its))
    log()

    # 连 Chrome 调试端口（只需端口可达；每个账户会自己开独立标签页）
    try:
        pages = list_pages(args.port)
    except Exception as e:
        log(f"[fatal] 连不上 Chrome 调试端口 127.0.0.1:{args.port}：{e}")
        log("       先确认 Chrome 是带 --remote-debugging-port=9222 启动的，")
        log("       并跑 curl http://127.0.0.1:9222/json/version 验证。")
        return 1
    cands = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    if not cands:
        log("[fatal] 调试端口里没有可用的标签页，无法开新标签页")
        return 1
    log(f"[plan] 将为每个账户开独立标签页（共 {len(groups)} 个，跑完保留供查看）\n")

    results = []
    crash = None
    try:
        for acc_no, (acc, its) in enumerate(groups.items(), 1):
            log(f"===== [{acc_no}/{len(groups)}] 账户 {acc}（{its[0][3]}） =====")

            # 先给该账户每个文件夹落一条 RUNNING 占位行：中途崩了也能看出卡在哪，
            # 而不是整批记录跟着异常一起蒸发。
            base = len(results)
            for it in its:
                results.append({"folder": it[0], "account": acc, "project": it[3],
                                "account_name": "", "status": "RUNNING", "files": 0,
                                "skipped": 0, "staged": -1, "mb": 0,
                                "ok_n": "", "fail_n": "", "failed_files": "",
                                "note": "执行中（未跑完即中断）"})
            cdp = None
            try:
                if args.tab_id and acc_no == 1:
                    # --tab-id 只用于第一个账户；其余账户仍各自开新标签页
                    tab_ws = next((p["webSocketDebuggerUrl"]
                                   for p in cands if p["id"] == args.tab_id), None)
                    if not tab_ws:
                        raise RuntimeError(f"找不到 tab id={args.tab_id}")
                    log("  [tab] 复用指定标签页（首个账户）")
                else:
                    tab_ws = create_tab(args.port, LIB_URL.format(acc))
                    log(f"  [tab] 已开独立标签页：{LIB_URL.format(acc)}")
                cdp = CDP(tab_ws)
                cdp.call("DOM.enable")
                cdp.call("Runtime.enable")
                cdp.call("Page.enable")
                process_account(cdp, acc, its, results, base, args, exts, recursive)
            except Exception as e:
                log(f"  [error] 账户 {acc} 处理异常：{type(e).__name__}: {e}")
                # 只把还停在 RUNNING 的占位行标成 FAILED，已跑完的账户不动
                for i in range(base, len(results)):
                    if results[i]["status"] == "RUNNING":
                        results[i].update(
                            {"status": "FAILED",
                             "note": f"{type(e).__name__}: {str(e)[:100]}"})
            finally:
                # 关闭 CDP 连接即可（标签页保留在浏览器里供后期查看），不关浏览器标签
                try:
                    if cdp is not None:
                        cdp.close()
                except Exception:
                    pass

    except KeyboardInterrupt:
        crash = "用户中断 (Ctrl+C)"
        log("\n[中断] 收到 Ctrl+C，下面的汇总只含已跑完的部分")
    except Exception as e:
        crash = f"{type(e).__name__}: {e}"
        log(f"\n[fatal] 未捕获异常：{crash}")
    finally:
        # 汇总放这里：不管上面怎么退出的，进度都要落盘
        return emit_summary(results, crash, args.out)


if __name__ == "__main__":
    sys.exit(main())
