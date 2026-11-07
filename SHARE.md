# 配送员 · 分享与同步更新指南

> 这个 skill 是**本地创建**的（`_skillhub_meta.json` 里 `source: local`），没发到技能市场。
> 分发靠「打包 → 发 zip」，更新靠「重新打包 → 重发」；也可以走 Git 让对方自助拉。

## 〇、版本仓库（已建立 · 2026-11-07）

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/yangzq66666/824238`（**Private**） |
| git 地址 | `https://github.com/yangzq66666/824238.git` |
| 本地目录 | `~/.workbuddy/skills/peisongyuan` |
| 分支 | `main`，已跟踪 `origin/main` |

> ⚠️ 用户名是 **`yangzq66666`（5 个 6）**，多打一个 6 会报 `Repository not found`，
> 而 GitHub 对私有仓库一律用 404 伪装，很容易误判成「认证没过」。地址以 `git remote -v` 为准。

以后改完功能：`git add -A && git commit -m "..." && git push` 即可，无需重新配置。
**仓库是 Private，朋友看不到**；要让他们直接从 GitHub 下载，去 Settings 改成 Public（已脱敏，可公开），
否则照旧发 zip。

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

## 四、私有仓库怎么给朋友用

**私有仓库不是分发渠道**——它只管你自己的版本历史，别人默认看不到，也不需要看到。
让朋友访问仓库的门槛很高（GitHub 账号 + 国内直连常 443 超时 + 会操作 Git），
对剪辑师朋友不推荐。**照旧发 zip 最省事。**

只给懂技术的朋友：GitHub 仓库 → Settings → Collaborators → 加对方账号，对方接受邀请后
可以用下面这段提示词自助拉更新：

```
帮我校验并更新本地已装的 peisongyuan skill。核心要求：绝对不要动我的 config.json。
源码仓库：https://github.com/yangzq66666/824238
步骤：
1. 确认本地已装在 ~/.workbuddy/skills/peisongyuan/
2. 先把我的 config.json 备份成 config.json.bak
3. 拉取仓库最新代码，覆盖 scripts/、SKILL.md、README.md、references/ 等通用文件
4. 用 config.json.bak 恢复 config.json
5. 跑 setup_check.py，并对比 SKILL.md 的 version 告诉我升到哪一版、改了啥
```

## 五、公开前的脱敏检查（已完成）

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
