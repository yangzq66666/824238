# 上传技术方法：原生 CDP（推荐） vs bsk（禁用）

> 本文件是 2026-10-23 实测的结论沉淀，决定了本技能的上传实现路线。

## 0. 结论先行

| 路线 | 单批大小上限 | 是否采用 |
|---|---|---|
| **bsk CLI**（browser-skill 的 `bsk upload --mode input`） | **512 MiB（536870912 字节）硬上限** | ❌ **禁止用于上传** |
| **原生 CDP 直连 Chrome 9222**（`scripts/cdp_upload.py`） | 无自身限制，受平台规则约束（100 个/批、单个 <500MB） | ✅ **唯一采用** |

## 1. 为什么禁用 bsk 上传

`Page.setInterceptFileChooserDialog` / `DOM.setFileInputFiles` 本身不限制文件大小——Chrome
只接收**本地路径**，由浏览器自己从磁盘读。限制是 **bsk 自己加的**：它把文件字节搬进
daemon 的「会话暂存通道」，而该通道上限硬编码为 536870912 字节，且**无任何配置项可放开**
（已确认：`daemon`/`upload` 无对应参数，`BSK_*` 环境变量里也没有相关开关）。

实测（探针页 `file://`，干净新会话，会话内只做了一次 navigate，零额外负载）：

| 注入量 | 结果 |
|---|---|
| 9 个文件 / 989.9 MiB | ❌ `session transfer staging exceeds limit 536870912` |
| 5 个文件 / 598.7 MiB | ❌ 同上 |
| 4 个文件 / 494.6 MiB | ✅ 成功（回读 `input.files` 确认为 4 files） |

三点补充：

1. **失败后暂存会释放**——同一个失败过的会话紧接着注入 494.6 MiB 仍成功，不存在"污染"。
2. 同一会话里 `bsk observe` 的感知探针/截图数据也走这条通道，会挤占预算。所以早期出现过
   "重负载会话传 411MB 失败、干净会话传同样的 411MB 成功"——那是 oberve 占用叠加的结果，
   但底层仍是这个 512 MiB 天花板。
3. bsk 路线还要依赖扩展的「允许访问文件网址」权限，多一层环境耦合。

→ **结论：只要走 bsk，就永远传不了超过 512 MiB 的批次。必须绕开它，直连 CDP。**

## 2. 唯一采用的机制：chooser 拦截四步

快手视频库是 React 页面，**直接对 input 调 `DOM.setFileInputFiles(nodeId=...)` 无效**
（返回 OK，但文件不生效；普通页面有效，已用探针页对照验证，与 input 是否 `display:none` 无关）。
必须用文件选择器拦截流程：

```
1) Page.setInterceptFileChooserDialog { enabled: true }
2) Input.dispatchMouseEvent  → 对拖拽区派发「可信鼠标点击」
3) 收 Page.fileChooserOpened 事件 → 取 backendNodeId
4) DOM.setFileInputFiles { files, backendNodeId }        ← 目标方法
5) 页面自己点「上传」提交
```

**第 2 步是成败关键**：JS 的 `input.click()` 没有 user activation，Chrome 不会打开文件选择器
（实测等 15 秒无事件）。必须用 CDP 的 `Input.dispatchMouseEvent` 派发
`mouseMoved → mousePressed → mouseReleased`，这样才带 user activation。

## 3. 页面元素（已回填，实测有效）

| 用途 | 定位 |
|---|---|
| 打开上传弹窗 | 文本 `上传视频` 按钮 |
| 触发文件选择器（**不是 button**） | CSS `.kcfe-ad-file-selector`（拖拽区，「选择视频」只是其中文本） |
| 弹窗标识文本 | `视频上传要求`（用于回读暂存状态） |
| 提交 / 取消 | `上传` / `取消` |
| 素材名区别于弹窗外的列表 | 弹窗外、`取消` 按钮所在的最小共同祖先容器 |
| 分页器 | `.ant-pagination`，页码元素 `.ant-pagination-item-N`（10 条/页） |
| 打开标签气泡（**不是文字「添加标签」那个 DIV**） | `.ant-btn` 且 `textContent.trim()==='添加标签'` |
| 标签气泡面板 | `[class*="popoverContainer"]`（ant-popover，portal 渲染），内有 `input[placeholder*="限"]` |
| 单个标签行 | `[class*="popoverContainerItem"]`，其中 `label.ant-checkbox-wrapper`，**标签文字在 label 外面** |
| 新建标签的提交区 | `[class*="submit"]`（为空时带 `index-module__disable___*`） |
| 失败卡片 | `.kcfe-ad-upload-video-content-content-list-item` 且含 `<video>` + 叶子`原因` |
| 失败素材完整名 / 原因 | 悬停名称 / 「原因」链接后的 `.ant-tooltip-inner` 文本 |

平台限制：**一次最多 100 个，单个文件 <500MB**。

## 3.1 三个只能靠实测才知道的结构细节（2026-10-27）

1. **「添加标签」有两个同名元素**：标题 `DIV`（点了没反应）和 `.ant-btn`（真正开气泡）。
   按文字点必错，必须限定 `.ant-btn`。
2. **勾选框的文字不在 `<label>` 里**：结构是
   `<label class="ant-checkbox-wrapper"><span class="ant-checkbox"><input type=checkbox>…</span></label>心心相印`。
   所以「按 label.innerText 找标签」永远匹配不到；要用**整行 textContent** 匹配，
   点击目标是 label/checkbox 本体（点文字无效）。
3. **`title` 属性不可信**：失败卡片里带 `title` 的元素值也是**截断**的
   （实测 `title="年10月27日-项目A"`，真名在 tooltip 里是 `2026年10月27日-项目A-口播-1`）。
   完整名只能悬停取 tooltip；`title` 最多当兜底。

另外：读 tooltip **不能取「最后一个可见浮层」**——页面常驻公告（“新增视频分析功能…知道了”）
和调研弹窗会串进来。只认「水平距悬停点 <600px、垂直 <260px、按面积取最小」的浮层。

## 4. 坑清单（每一条都踩过）

1. **WS 握手 403**：Chrome 未用 `--remote-allow-origins=*` 启动，默认带 `Origin` 头会被拒。
   建连时用 `websocket.create_connection(..., suppress_origin=True)`，不必重启浏览器。
2. **刷新后 `DOM.requestNode` 返回 nodeId=0**：`DOM.getDocument` 指向旧文档，必须重新取。
3. **残留节点**：反复开关弹窗会在 DOM 里累积旧 input（实测最多 16 个），
   `querySelector` 会选错。批量时每批前检查触发元素数量，>0 就 reload。
4. **弹窗内文件名被截断**成 `2026年10月2...(1)`，所以校验**只能比条目数**，不能匹配全名。
   条目数 = `...-list-item` 卡片数 − 「选择视频」占位卡；新版弹窗已无「已上传素材（N/100）」计数器。
   ⚠️ 别对统计结果写 `.slice()`，2026-10-27 因此把 104/87 个误报成 30。
5. **文本点击要取「文本最短」的命中元素**，否则会点到包着整页文字的外层容器 div。
6. **可信点击前必须 `scrollIntoView` 并校验坐标在视口内**。曾出现中心坐标 (1992, 204)
   落在视口外、点击无效却表现为"收不到 fileChooserOpened"的静默失败。现在的校验会
   显式报「不在视口内」。
7. Windows 下路径正斜杠 / 反斜杠 Chrome 都能识别，不是问题。

## 5. 上传耗时参考

约 **6–8 MB/s**（实测：751.3 MB → 106s；367.8 MB → 81s；770.5 MB → 约 130s；
1471.2 MB / 50 个 → 184s = 8.0 MB/s，2026-10-27 两段式轮询版）。
平台侧校验很快失败的情况：310.9 MB / 5 个 → 33s，113.1 MB / 2 个 → 18s。

**判定完成不要等 `.kcfe-ad-file-selector` 消失**——部分失败时抽屉不会自己关，
全部失败时连结果 toast 都会一闪就没。现在的判据（任一成立）：
结果 toast、出现「完成/关闭/知道了」按钮、抽屉自己关掉（曾见过 + 连续两次探测不到）、
抽屉状态稳定（有失败卡片 + 无进度条 + (卡片数, 失败数) 连续两次不变）。
默认超时 900s。

### 5.1 两段式轮询（2026-10-27 优化，实测有效）

**问题**：原先每 3s 做一遍重活——遍历上百张卡片、读每张卡 `innerText`、
再读一次 `document.body.innerText`（整页强制重排）。大批量时和上传抢渲染主线程，
100 个 / 3.4GB 的批次只有约 5.5 MB/s，而 3 个小文件能跑到 8.45 MB/s。

**改法**：`settle_after_submit` 分两段。
- 阶段 A（每轮都做，1ms）：`_probe_light` 只查「抽屉内有没有可见进度条」+ 结束按钮 + toast 候选容器。
  命中进度条就 `sleep(poll_busy=6s)`，卡片一概不碰。
- 阶段 B（确认不忙才做，`poll_idle=2s`）：数卡片、数失败、比 `(总数, 失败数)` 签名。

实测：50 个 / 1471.2 MB → 184s = **8.0 MB/s**（旧版同量级约 5.5）。小批未见变慢。

配套的两个坑：
1. **busy 必须限定在抽屉内查**。页面上常驻一个
   `ant-spin-nested-loading index-module__spin___xxx` 的表格工具条（1032×65、一直可见），
   class 含 loading；全局查 `[class*="loading"]` 会永远判「还在传」→ 干等到 timeout。
2. **卡片数不足注入数时不要急着结算**：判完成后若 `卡片数 < 期望数`，再宽限 3 轮
   （渲染慢的批次会先只出现一部分卡片）。

### 5.2 长任务必须后台跑

一次 100+ 个的批次要 8–10 分钟，前台 shell 有 600s 上限，会被 SIGTERM 掐掉——
而且 Python 输出走管道是全缓冲，**进程被杀时缓冲区一起丢，终端一个字都没有**，
看起来像"啥也没干"，实际上传已经完成、只是结果 CSV 没落盘。
大批量一律后台跑 + 输出重定向到日志文件，再读日志。


## 6. 为什么不做「多账户并发上传」（2026-10-27 实测结论，已定案）

**结论：保持串行。并发不做，也不再评估。**

对等样本实验（同一个 184.6 MB 文件，软广 3 个 vs 复制到本机混，两次跑）：

| 模式 | 合计吞吐 |
|---|---|
| 串行单账户 | 8.45 MB/s（22s 传完） |
| 2 账户并发 · 第 1 次 | 3.30 MB/s（约 39%） |
| 2 账户并发 · 第 2 次 | 4.39 MB/s（约 52%） |

两次并发都**没有提速，反而腰斩**。原因推断是上传带宽被平台侧按会话/账号公平调度，
多开会互抢，而不是叠加。

附带的工程代价也很实在：多个标签页同时上传时，**失败卡片的抓取会错乱**
（悬停 tooltip 读到别的页的公告横幅 / 别的批次的卡片），
而"失败素材名 + 原因"是这一步的核心交付物，不能为了速度把它弄脏。

因此单账户内超过 100 个自动分批（100 + 余数）串行跑，账户与账户之间也严格串行。
`batch_upload.py` 不接受任何并发参数。
