# gitx — 给 Agent 的受控 Git 工作视图（MVP）

> [!WARNING]
> **AI 生成项目**。本仓库由 AI 编程助手生成，可能存在设计缺陷或问题；
> 使用前请自行审查，生产环境请谨慎。欢迎提 issue 和贡献。

> **English**: [README.md](README.md)

> **一句话**：在 git worktree 上用 **git 自己的机制**实现 `RW / RO / HIDDEN`
> 三种文件锁；Agent 的合法修改实时处于 git 版本控制之下，diff /
> checkpoint / rollback 全部是原生 git，**没有任何挂载、沙箱或第二份状态**。

## 架构（git-native，零挂载）

```
你的仓库 (正常 checkout，不被打扰)
└── .gitx/worktrees/<id>/        ← git worktree，就是 Agent 的 cwd
     │
     ├── HIDDEN  git sparse-checkout   文件根本不落盘：ls/cat/stat 全部 ENOENT
     ├── RO      chmod 444/555          写入得到 Permission denied；嵌套目录连删除/改名都挡
     ├── guard   pre-commit hook        锁定文件的改动永远进不了 commit（含 agent 自己跑 git）
     └── 其余    RW                     正常工作，实时出现在 git status / git diff
```

| 锁 | 机制（全部是现有轮子） | Agent 看到的 |
|---|---|---|
| `rw` | 无干预 | 正常读写，实时进 git |
| `ro` | chmod（文件 444，目录 555） | `Permission denied`（同 EROFS 的 UX）；读/构建正常 |
| `hidden` | `git sparse-checkout`（非 cone 模式，gitignore 式 pattern） | 文件不存在（`ls` 看不到、`cat` ENOENT） |

版本管理 100% git：`status` = git status、`checkpoint` = git add+commit、
`rollback` = git reset+clean、`finish` 后用普通 `git merge` 收编分支。

## 快速开始

依赖（Linux）：`git` ≥ 2.25（sparse-checkout）、Python ≥ 3.8。**不需要
fusepy / bwrap / root**。`pip3 install -e .`（editable 需 setuptools ≥ 64，
否则用零安装 wrapper `bin/gitx`）。

```bash
cd your-repo
gitx init                          # 生成 .gitx.toml（默认 default = "rw"）

gitx lock package-lock.json        # RO
gitx lock 'generated/**'           # RO 整棵子树
gitx hide .env                     # HIDDEN
gitx hide 'secrets/**'

gitx ls                            # 查看每个文件的锁状态
gitx session create                # git worktree add -b agent/<id>
gitx run <id> -- pi                # ← 就这么简单（见下）
```

## 运行真实 Agent CLI

```bash
gitx run <id> -- pi                # 或 claude、codex、aider、任意命令/脚本
gitx run <id> -- pi -p "fix the failing test"
```

Agent 的 cwd 是 session worktree，**其余一切（HOME、PATH、配置、认证）
都是你自己的环境**——因为没有任何挂载/沙箱，所以也没有"把环境重建一遍"
的坑。`gitx run` 做的事只有：确保锁就位（幂等，还会修复 agent 对锁定
文件的 chmod 篡改）→ 以 worktree 为 cwd exec。

Agent 也可以直接用 git（status/diff/log/commit）——它的 commit 同样
过 pre-commit 守卫。

## Session 生命周期（全部是普通 git）

```bash
gitx session create [--base <rev>] [--name <name>]
gitx run <id> -- pi "fix the failing test"

gitx status <id>               # git status + 锁违规警告
gitx diff <id>                 # git diff HEAD
gitx checkpoint <id> -m "msg"  # git add -A && git commit（违规则拒绝）
gitx rollback <id>             # git reset --hard HEAD && git clean -fd（先解锁再重锁）
gitx rollback <id> --base      # 回到 session 起点
gitx finish <id>               # git worktree remove；分支 agent/<id> 保留

git diff main...agent/<id> && git merge agent/<id>
```

## 查看锁状态：`gitx ls`

用户侧视图（host 上运行），HIDDEN 文件也列出——你能看到 Agent 看不到什么：

```
$ gitx ls                        # 或 gitx ls -v / -R src / --session t1
hidden .env        <- rule #2: hidden .env
hidden .git/       <- built-in (always hidden)
ro     generated/  <- rule #1: ro generated/**
rw     src/        <- default
```

`gitx policy [--path P]` 显示规则本身并解析单个路径。

## 权限语义（pattern 规则）

规则写在 `.gitx.toml`（建议提交），gitignore 风格、**last matching rule
wins**；`gitx unlock X` = 追加一条 `mode="rw"` 覆盖规则。

- 不含 `/` 的模式匹配任意深度（`.env` 也匹配 `sub/.env`）；前导 `/` 锚定仓库根。
- `*` 不跨目录、`?` 单字符、`**` 跨目录段；一条规则匹配路径及其全部子孙。
- `.git`、`.gitx` 内置 HIDDEN 不可覆盖。

**sparse 模式的一个已知限制**：把某目录 hide 后再 unlock 它的子树不生效
（gitignore 的目录裁剪语义）；请按目录粒度操作：`hide secrets` →
`unlock secrets` → `hide secrets/private`。

## 威胁模型（诚实声明）

没有进程/文件系统边界，锁保护的是 **workspace 内的正常文件操作**——
这是"结构化约束"，不是"硬隔离"：

- **保证**：HIDDEN 文件不落盘（普通工具读不到）；RO 写入失败（嵌套目录
  连 unlink/rename 都失败）；锁定文件的改动进不了任何 commit
  （`gitx checkpoint` 和 hook 双重拦截）；一切违规在 `gitx status` 可见；
  rollback 总能恢复。
- **unix 泄露面（同 uid 下无法避免）**：agent 可以 `chmod u+w` 改 RO
  文件、可以 unlink/rename **顶层** RO 文件（父目录必须保持可写）——但
  这些都 git 可见、被守卫拦截、可 rollback；嵌套 RO 目录完全保护。
  每次会话/运行开始时 gitx 会修复被篡改的权限。
- **不保证**：agent 刻意走出 worktree（`cat ../.env` 读主 checkout、读
  你 home 的任何东西）或翻 git 历史（`git show HEAD:.env`）——这与今天
  直接在终端跑 agent 的风险等级相同。秘密本就不该进 git 仓库；需要硬
  边界时是 sandbox 问题，不是 git 问题（v0.2 的 FUSE+bwrap 方案验证后
  已被本版本取代）。
- HIDDEN 文件的名字在 `git ls-files` 中仍可见（内容需要刻意 `git show`）；
  agent 在 hidden 路径**新建**的文件是它自己的文件（不是你的秘密），git
  会忽略该路径。

## 代码结构（刻意很小）

```
gitx/
├── cli.py      # 命令分发 + run/guard/status/checkpoint/rollback 编排
├── policy.py   # RW/RO/HIDDEN + gitignore 风格 pattern（纯逻辑，可单测）
├── config.py   # .gitx.toml 读写（3.11+ 用 tomllib，否则内置极简解析）
├── gitcmd.py   # git CLI 包装
├── locks.py    # 锁的三种 git 机制落地：sparse / chmod / hook
└── session.py  # git worktree + 会话元数据 + flock
```

## 测试

```bash
python3 -m unittest tests.test_policy   # 策略匹配单元测试
bash tests/e2e.sh                       # 57 项矩阵：RW/RO/HIDDEN 不变量、
                                        #   unix 泄露面的可见性+守卫+恢复、
                                        #   git 生命周期、主 checkout 隔离
bash examples/demo-agent.sh             # 端到端演示：修 bug→被锁→适应→合并
bash examples/run-pi.sh <repo> -- -p "…"  # 真实 pi（需要 pi + auth）
```

## 已知限制 / 下一步

设计手册（含被验证过的 v0.2 FUSE 方案、实验目的与失败模式思考）：
[`docs/design-guide.md`](docs/design-guide.md)（§15/§16 是实验待验证的问题）。
值得收的数据：哪些锁真的有用？Agent 对 `Permission denied` 的反应？
HIDDEN 是否影响完成率？锁的粒度（目录 vs 文件）是否需要更细？

## 作者的话

作者**不建议**把这个仓库或工具当作实用的日常方案。它首先是一个**思想实验**，围绕一个问题展开：

> *Git 生来是帮助人类管理文件的工具——在人机协作的时代，它该如何进化？*

它想探查的问题:agent 会把遇到的一切内容不假思索地**照单全收**，而其中有些东西对它的上下文是有害的。作为人类，我们往往知道哪些文件与当前任务无关（不必读），哪些文件不该被改动。我们希望在**把工作区交给 agent 之前**把这些边界划好。

这件事看上去和 `git worktree` 已经做的事情非常相似——于是问题变成了：*git 能不能天然地承担起这份责任？*

如果说这个仓库还有什么价值，那就是给那些想打造下一代 agent-friendly 的 git 类工具的人当个参照：在这次尝试之前，这个想法还没有公开的实现（就作者所知）。如果未来真有工具从这个想法里长出来，作者会很高兴知道。

亲历这次尝试之后，它的局限也清楚了——若要完整满足需求（硬隔离），主流的 bubblewrap 类沙箱似乎是更实际、也更好用的路。

> 愿思想碰撞出的火花可以照亮一点点前行的迷雾。

## License

[MIT](LICENSE)
