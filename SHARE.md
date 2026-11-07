# 配送员 · 分享与同步更新指南

> 这个 skill 是**本地创建**的（`_skillhub_meta.json` 里 `source: local`），没发到技能市场。
> 分发靠「打包 → 发 zip」，更新靠「重新打包 → 重发」；也可以走 Git 让对方自助拉。

## 〇、版本仓库（已建立 · 2026-11-07）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/yangzq66666/824238`（**Public**，2026-11-07 起） |
| git 地址 | `https://github.com/yangzq66666/824238.git` |
| 朋友下载 | 仓库页 → 绿色 **Code → Download ZIP**（无需账号） |
| 本地目录 | `~/.workbuddy/skills/peisongyuan` |
| 分支 | `main`，已跟踪 `origin/main` |

> ⚠️ 用户名是 **`yangzq66666`（5 个 6）**，多打一个 6 会报 `Repository not found`，
> 而 GitHub 对私有仓库一律用 404 伪装，很容易误判成「认证没过」。地址以 `git remote -v` 为准。

以后改完功能：`git add -A && git commit -m "..." && git push` 即可，无需重新配置。
仓库已改 **Public**，实测匿名 `git ls-remote` 免登录成功，朋友不需要 GitHub 账号或 token。
具体怎么装进他们的 WorkBuddy，见下面第四节。

## 一、打包（1 条命令）

`git archive` 只打包**已提交**的文件。而 `config.json`（本机路径）、备份、缓存、运行产物
都被 `.gitignore` 排除、从没进过仓库，所以导出的 zip **天然干净**，不用手工挑文件。

```sh
cd ~/.workbuddy/skills/peisongyuan
git archive --format=zip --prefix=peisongyuan/ \
  -o "../peisongyuan_v1.0.0.zip" HEAD
```

> `git archive` **不需要远程仓库**，本地 `git init` + 一次 commit 就能用。
> 完全可以不联网、不用 GitHub、不用 token，照样拿到干净的分发包。

## 二、朋友怎么装（零门槛）

- **首次装**：WorkBuddy 左侧「专家 · 技能 · 连接器 → 技能 → 添加技能 → 上传技能」把 zip 拖进去
- **或手动**：解压到 `~/.workbuddy/skills/peisongyuan/`，**重启 WorkBuddy** 才生效

装完必做三件事：
1. 复制 `config.example.json` → `config.json`，改成自己机器的 `videoRoot` / `xlsxPath` / `chromePort`
2. 跑 `python scripts/setup_check.py`（缺依赖加 `--fix`）
3. 按 `README.md` 用调试端口 9222 启动 Chrome，并登录磁力金牛投放后台

## 三、更新到最新版

| 你这边 | 朋友那边 |
|---|---|
| 改代码 → 改 `SKILL.md` frontmatter 的 `version` → `git commit` → 重新出包 → 发微信 | 解压覆盖到原 skill 目录（**不要删目录重建**），保住自己的 `config.json`，重启 WorkBuddy |

> 覆盖是安全的：分发包里**故意不含 `config.json`**，只有 `config.example.json`。
> 删目录重建反而会丢配置，得重新填一遍路径。

## 四、朋友怎么把它拉进 WorkBuddy（仓库已公开）

匿名可读（实测免登录 `git ls-remote` 成功），朋友**不需要 GitHub 账号、不需要 token**。
三条路按省事程度排：

### 方式 A：发 zip（最稳，推荐给不折腾的朋友）

把 `peisongyuan_v1.0.0.zip` 直接发微信 → 对方在 WorkBuddy 左侧
「专家 · 技能 · 连接器 → 技能 → 添加技能 → 上传技能」把 zip 拖进去，系统自动配置。
这个包顶层就是 `peisongyuan/`，**不用解压、不要重新打包**。

### 方式 B：朋友自己从 GitHub 下

仓库页 → 绿色 **Code → Download ZIP** → 把下载的**原始 zip** 拖进「上传技能」。

> ⚠️ GitHub 自动生成的 zip 顶层是 `824238-main/`（不是 `peisongyuan/`）。
> **不要解压后重新压缩**——多套一层目录会报「找不到 SKILL.md」。
> 若装完技能名显示成 `824238-main` 或提示找不到入口，改用方式 A，或走方式 C 让 Agent 处理。

### 方式 C：把仓库地址交给 WorkBuddy 自己装（后续更新最省事）

朋友**新建一段对话**，把下面整段贴进去即可（这段就是他需要做的全部操作）：

```
帮我把下面这个公开仓库的 Skill 安装到 WorkBuddy。核心要求：绝对不要覆盖我已有的 config.json。
仓库：https://github.com/yangzq66666/824238
分支：main
目标目录：~/.workbuddy/skills/peisongyuan/

步骤：
1. 若目标目录已存在且有 .git，改为 git pull 更新；否则先把它原有的 config.json 备份到别处
2. 匿名下载仓库 main 分支的全部文件，平铺放进目标目录（不要多套一层 824238-main/）
3. 确认 SKILL.md 在目标目录根部，frontmatter 里 name 是 peisongyuan
4. 若目录里没有 config.json：复制 config.example.json 为 config.json，并提醒我按 README 填自己的 videoRoot / xlsxPath / chromePort
5. 若第 1 步备份过 config.json，原样恢复回去
6. 跑 python scripts/setup_check.py 自检
7. 告诉我装到了哪、SKILL.md 的 version 是多少，并提醒我重启 WorkBuddy 才生效

若 GitHub 直连超时，别反复重试，直接告诉我改用手传 zip。不要改动仓库里的任何文件。
```

这样装出来的目录带 `.git`，**以后你 push 新版，朋友只要说一句**
「帮我更新 peisongyuan skill 到最新版，别动我的 config.json」，WorkBuddy 会自动 `git pull`，
不用重新下载 zip——这才是真正的「更新同步」。

### 装完必做（三种方式都一样）

1. 复制 `config.example.json` → `config.json`，填自己的 `videoRoot` / `xlsxPath` / `chromePort`
   （分发包与仓库里**故意不含 config.json**，任何更新都覆盖不到你的配置）
2. 跑 `python scripts/setup_check.py`
3. **重启 WorkBuddy** 刷新技能索引，否则技能列表还显示旧版
4. 对话里说「把视频按账户上传」或「配送员」即可触发

### 更新怎么同步

| 安装方式 | 你 push 新版后朋友要做的 |
|---|---|
| 方式 C（git 目录） | 一句「更新 peisongyuan skill」，自动 `git pull` |
| 方式 A / B（zip 导入） | 收新 zip 覆盖解压到原目录，**别删目录重建**（会丢 config.json） |

> 国内直连 GitHub 偶发 443 超时；方式 B/C 卡住时，退回方式 A 发 zip 兜底，只试一次别反复重试。

## 五、公开仓库脱敏检查（已通过）

| 位置 | 处理 |
|---|---|
| `config.json` 里的 `C:/Users/Administrator/Desktop/账户ID.xlsx` 等本机路径 | 已被 `.gitignore` 排除，不进包 |
| `scripts/cdp_upload.py` 示例里的真实人名 | 已换成中性占位 |
| `README.md` / `SKILL.md` 里的账户 ID | 均为「仅供参考」的示例值（12345678 等），非真实 ID |
| `references/` 里的 536870912 | 512 MiB 字节数，技术常量 |

**这个 skill 不含 staffId、API Key 或任何密钥**，脱敏后可直接公开。

## 六、每次改完功能，必做三件事
1. **bump 版本号**：`SKILL.md` frontmatter 的 `version`
2. **记变更**：在下面「变更记录」补一行
3. **发布**：`git commit` → `git push` → 重新出包发朋友（zip 与仓库保持同一版）

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0.0 | 2026-11-07 | 首次整理为可分发版：补 `.gitignore`、`SHARE.md`，示例人名脱敏，纳入 Git 版本管理 |
| 1.0.0 文档 | 2026-11-07 | 仓库改 Public；SHARE.md 补「朋友怎么拉进 WorkBuddy」三种方式、安装提示词与更新同步流程（代码未变） |
