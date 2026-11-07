# 配送员 · 工作流参考 (references/workflow.md)

本文件记录 磁力金牛(快手) 素材库批量上传的可复用操作知识。SKILL.md 只讲流程，细节与坑点都在这里。

> **2026-10-23 重要变更**：上传通道已从 **bsk 切换为原生 CDP 直连 Chrome 调试端口**。
> 原因与技术细节见 `references/upload_method.md`（含 bsk 512 MiB 上限的实测数据）。
> 本文件中所有 bsk 上传命令均已废弃，仅作历史留档；**第 4 节以前的结论以新版为准**。

## 1. 前置条件

1. **Chrome 带调试端口运行**（唯一上传通道）：用**原 profile** 启动以保留登录态，
   本机已用桌面的 `Chrome-Debug-Port.bat`：

   ```
   chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\Users\<你>\AppData\Local\Google\Chrome\User Data"
   ```

   验证：`curl http://127.0.0.1:9222/json/version` 应返回浏览器信息。
2. **登录态**：该 Chrome 必须已登录「磁力金牛服务商平台」(`jinfu.e.kuaishou.com`)，
   且能访问目标广告主账户（服务商/代理账号下有多个广告主账户）。

### 1.1 两个域名必须分清（2026-10-24 用户确认 + 实测）

| 域名 | 是什么 | 典型 URL |
|---|---|---|
| `jinfu.e.kuaishou.com` | **服务商/代理商后台**（管广告主账户） | `https://jinfu.e.kuaishou.com/account/list` ← **用户指定的正确入口** |
| `niu.e.kuaishou.com` | **广告投放后台**（真正上传视频的地方） | `https://niu.e.kuaishou.com/material/supervideo?__accountId__=<ID>` |

- 从 jinfu 账户列表点某账户的「访问」→ **新开标签页**跳到
  `https://niu.e.kuaishou.com/home?__accountId__=<ID>&homeType=super`。
- ⚠️ **`/material/supervideo` 在 jinfu 域名下是 404**，它只存在于 niu 域名。
- ⚠️ **不要在 jinfu 标签页里 navigate 到 niu 的 URL**（跨域，视频库加载不出来，
  表现为 `wait_lib_ready` 超时 → 报「视频库未就绪」）。
  正确做法：**选中一个 niu 标签页**，然后在它内部改 `__accountId__` 切账户（实测可切）。
- 若浏览器里没有 niu 标签页：先去 `jinfu.e.kuaishou.com/account/list` 点任一账户的「访问」
  打开一个，之后这个标签页就能一直复用。脚本已按此规则选标签页（优先 niu 域）。

> ~~bsk CLI / BrowserSkill 扩展 / 「Allow access to file URLs」权限~~ —— 均**不再需要**。
> 原依赖该权限绕过的 `Not allowed` 报错，在直连 CDP 路线下不存在。

## 2. 核心技巧：视频库按 URL 隔离账户

每个广告主账户的视频库是独立 URL，靠查询参数区分，**上下文不串号**：

```
https://niu.e.kuaishou.com/material/supervideo?__accountId__=<账户ID>
```

- 直接导航到该 URL 即进入该账户视频库，**无需**展开「素材」下拉；但**必须是在 niu 域名
  的标签页内**导航（见 1.1 节，从 jinfu 标签页跨域过去会加载不出来）。
- 若从「账户列表 → 访问 → 素材 → 视频库」走，「素材」浮层会遮挡「上传视频」按钮（需先按 Esc 关浮层）。
- 不知道账户 ID 时：在服务商后台广告主账户列表用**账户ID**字段（不是名称字段）搜索，
  结果行「访问」链接里带 ID。

## 3. 上传弹窗结构（元素定位）

| 用途 | 定位 | 备注 |
|---|---|---|
| 打开上传弹窗 | 文本 `上传视频` 按钮 | 文本点击须取「文本最短」的命中元素 |
| **触发文件选择器** | `.kcfe-ad-file-selector` | **不是 button**，是拖拽区；「选择视频」只是其中文案 |
| 弹窗标识文本 | `视频上传要求` | 用于回读暂存状态 |
| 提交 / 取消 | `上传` / `取消` | dry-run 用取消丢弃，不入库 |
| 列表分页器 | `.ant-pagination`、`.ant-pagination-item-N` | 10 条/页 |
| 打开标签气泡 | `.ant-btn` 且文字为 `添加标签` | 页面另有一个同名标题 `DIV`，点它无效 |
| 标签气泡面板 / 标签行 | `[class*="popoverContainer"]` / `[class*="popoverContainerItem"]` | 文字在 `label` **外面**，用整行 textContent 匹配 |
| 新建标签提交区 | `[class*="submit"]` | 空内容时带 `disable` 类，点不动 |
| 失败卡片 | `…content-list-item` 且含 `<video>` + `原因` | 完整名只在悬停 tooltip 里，`title` 是截断的 |

> 旧版用「选择视频」按钮 + bsk 挂载的路线已废弃。**FYI**：`.kcfe-ad-file-selector`
> 必须用 `Input.dispatchMouseEvent` 派发**可信点击**才能触发文件选择器，`input.click()`
> 没有 user activation，Chrome 不响应。

## 3.1 标签与失败清单（2026-10-27）

- **每账户一个独立标签页**：`Target.createTarget`（浏览器级 WS，页面级会话不暴露 Target 域；
  HTTP `/json/new` 在本机 Chrome 返回 405 也不能用）。跑完不关，留给人看。
- **统一打标签**：清单「标签」列 → 气泡里勾选；没有的先新建（键入 → 等 disable 消失 →
  点提交区 → **再点一次勾选**）→ 确定 → 回读标签区校验。
- **失败素材**：单个失败不影响同批。完整名靠悬停 tooltip（卡片 `title` 也是截断的），
  原因靠悬停「原因」链接；读 tooltip 时按「贴着悬停点、面积最小」挑，否则会读到公告横幅。
  结果按文件归属摊到各自文件夹，输出 Markdown 表格 + `失败素材_<时间戳>.md`。

## 4. 上传命令要点（现行）

```bash
# 正式：按账户聚合，一次注入一次提交
python3 scripts/batch_upload.py --map "D:/视频/配送员_匹配清单.csv" --root "D:/视频" \
    --only-account "12345678,23456789" --aggregate

# 试运行：注入后取消，不入库（改动后先用这个验证）
python3 scripts/batch_upload.py ... --dry-run
```

- 聚合 `--aggregate`：该账户下所有文件夹的待传文件合并，**超过 100 个才自动分批**（平台上限）。
- 去重 `--dedupe`：**默认关闭**（翻页扫全库很慢，10 条/页、素材多的账户 18 页 ≈ 40s）。
  保留该能力待用户决定更省时的实现（拦截列表接口 / 只采最新几页）。
- 上传中弹窗**新版已不显示**「已上传素材 (N/100)」计数器，只有整体 `已上传N%` 和每张卡的百分比；
  条目数改为数 `...-list-item` 卡片（减去「选择视频」占位卡）。
  完成后页面出现 `N个视频上传成功，M个视频上传失败`。
  这个 toast 是瞬时的，**全部失败时几乎没有**，此时脚本按「有失败卡片 + 无进度条 + 文本稳定」
  判定完成，并用抽屉卡片数反推成功/失败数。

## 5. 已知坑

1. **WS 握手 403**：Chrome 未用 `--remote-allow-origins=*` 启动时，带 `Origin` 头会被拒。
   建连用 `suppress_origin=True`（已内置）。
2. **残留节点**：反复开关弹窗会在 DOM 累积旧 input（实测最多 16 个），`querySelector` 会选错。
   批量时每批前检查拖拽区数量，>0 就 reload。
3. **坐标落到视口外**：可信点击前必须 `scrollIntoView` **并校验坐标在视口内**，
   否则表现为"点不到、收不到 fileChooserOpened"的静默失败（已内置校验并显式报错）。
4. **刷新后 `DOM.requestNode` 返回 0**：需重新 `DOM.getDocument`。
5. **弹窗内文件名被截断**为 `2026年10月2...(1)`，校验**只能比条目数**；
   条目数 = `...-list-item` 卡片数 − 「选择视频」占位卡（不要用 `.slice()` 参与计数，
   曾因此把 104/87 个误报成 30）。
6. **bsk 的 512 MiB 上限**（历史，已绕开）：`session transfer staging exceeds limit 536870912`
   是 bsk 会话暂存通道的硬编码上限，无配置项可放开。这不是 CDP 也不是 Chrome 的限制。
   旧脚本因此被迫把账户内视频再切成 ~400MB 块逐块提交——**该分块逻辑已不再需要**。
7. **重复视频**：平台提示「不能上传内容重复的相同视频」，实测同名不同日期文件仍可上传（0 失败）。
8. 用户习惯：某项目传完会手动删掉 `D:/视频` 下对应文件夹，脚本会 `[skip] 目录不存在`，属正常。

## 6. 上传耗时

约 **6 MB/s**（实测 751.3 MB → 106s、367.8 MB → 81s、770.5 MB → 约 130s）。
判定完成：提交后轮询 `.kcfe-ad-file-selector` 消失，默认超时 900s。
