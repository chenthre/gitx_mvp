# Agent Git Workspace MVP 实现指导手册

> **阅读说明**：本文档是本项目最初的完整设计手册。正文第 4/5/9/17 章推荐的
> "FUSE 视图 + bubblewrap 沙箱"方案在 v0.2 中验证后被推翻——已发布的 v0.3
> 改为 **git-native** 架构（`git worktree` + sparse-checkout + chmod +
> pre-commit hook，零挂载零沙箱），见仓库 README。
> 设计动机（§1-§3）、体验原则（§6/§18）、测试矩阵（§14）与待验证的实验问题
> （§15/§16）仍然有效，保留供参考。

---

> 目标：验证一个简单但重要的想法——**Agent 操作的文件系统不是 Git checkout 之后的“自由工作目录”，而是一个受控的 Git 工作视图；Agent 对允许修改的文件所做的修改，实时落到真实 Git worktree 中，因此版本管理天然存在，不需要事后同步或重新投影。**

---

## 1. 背景：我们真正想解决什么

讨论一开始很容易走向“大而全”的设计：新的 VCS、Agent-native merge、ACL、Merkle DAG、runtime sandbox、secret broker、多 Agent 调度等。

MVP 不解决这些。

我们只解决两个紧密相关的核心问题：

1. **文件权限**：用户希望方便地限制 Agent 能看到什么、能修改什么。
2. **版本管理**：Agent 的修改必须天然处于 Git 的版本控制之下，能够 diff、checkpoint、rollback，且不需要在任务结束后再做一次“从 Agent workspace 同步回仓库”的动作。

典型痛点：

```text
repo/
├── src/                 Agent 可以修改
├── tests/               Agent 可以修改
├── generated/           Agent 运行时需要，但不希望修改
├── package-lock.json    Agent 运行时需要，但不希望修改
├── .env                 Agent 不应该看到
└── secrets/             Agent 不应该看到
```

简单地删除 `generated/` 或 `package-lock.json` 会破坏运行环境；简单地告诉 Agent“不要改”又没有真正约束。

因此需要的是一个**受控工作视图**，而不是“删除文件”。

---

## 2. 第一性原理

### 2.1 Git 继续做 Git 擅长的事情

不要重新实现 Git。

Git 已经非常擅长：

- 记录历史；
- diff；
- branch；
- merge / rebase；
- rollback；
- object storage；
- 与现有工具链兼容。

MVP 只在 Git 外面包一层。

### 2.2 Workspace 不是一份独立副本

传统模型：

```text
Git Repository
    ↓ checkout
普通目录
    ↓ Agent 修改
普通目录变脏
    ↓ diff / commit
Git Repository
```

MVP 的模型：

```text
Git Repository
    ↓ git worktree
真实 Git 工作目录（backing worktree）
    ↓ 受控文件系统视图
Agent Workspace
```

Agent 修改一个允许写的文件时：

```text
Agent write("src/a.ts")
        ↓
受控文件系统校验
        ↓
直接写入 backing git worktree/src/a.ts
        ↓
git diff 立即可以看到变化
```

**没有“任务结束后同步回去”这一步。**

### 2.3 文件存在性与可写性是两个问题

MVP 只定义三种路径状态：

```text
RW      可见、可读、可修改
RO      可见、可读、不可修改
HIDDEN  对 Agent 不可见
```

这三种状态已经覆盖最核心需求。

例如：

```text
src/**              RW
 tests/**            RW
 generated/**        RO
 package-lock.json   RO
 .env                HIDDEN
 secrets/**          HIDDEN
```

其中 `RO` 很关键：

> “程序运行需要”并不意味着“Agent 必须拥有修改权限”。

---

## 3. MVP 核心模型

可以把一个 Agent Session 定义为：

```text
Session = GitWorktree + PathPolicy + ControlledView
```

具体结构：

```text
Canonical Git Repository
        │
        │ git worktree add
        ▼
Session Backing Worktree
        │
        │ controlled filesystem view
        ▼
Agent Workspace
```

Agent 只接触最下面一层。

外层 CLI / orchestrator 可以访问 backing worktree，并使用普通 Git 命令完成版本管理。

### 3.1 关键 invariant

MVP 必须保证以下行为：

```text
RW:
  read   ✓
  write  ✓
  create ✓
  delete ✓
  rename ✓

RO:
  read   ✓
  write  ✗
  create ✗
  delete ✗
  rename ✗

HIDDEN:
  list   ✗
  stat   ✗
  read   ✗
  write  ✗
```

同时：

```text
所有 RW 修改 -> 立即落入真实 Git worktree
```

因此：

```text
git diff
```

永远是 Agent 当前真实修改的权威来源。

---

## 4. 一个重要的设计决定：不要让 Agent 直接操作 backing worktree

如果只是在 backing worktree 上 chmod：

```bash
chmod 444 package-lock.json
```

是不够的。

Agent 仍可能尝试：

```bash
rm package-lock.json
mv package-lock.json old
cp new-file package-lock.json
chmod +w package-lock.json
```

更严重的是，如果我们为了隐藏 `.env` 只是不展示它，但 Agent 仍然能够找到真实 backing path，那么它仍可以绕过视图直接读取。

因此安全边界必须是：

```text
Agent 只能访问 ControlledView
Agent 不应该能访问 backing worktree 的真实路径
```

这也是为什么 MVP 推荐：

- 一个很薄的文件系统视图层；
- 再用进程隔离把 backing worktree 从 Agent 的 namespace 中拿掉。

---

## 5. MVP 技术选型：尽量使用现有轮子

第一版推荐只支持 Linux。

### 5.1 Git

直接使用系统 Git：

```text
git worktree
git diff
git status
git add
git commit
git reset
git clean
```

不重新实现任何 Git object / branch / merge 逻辑。

### 5.2 Git Worktree

每个 Agent Session 创建一个真实 linked worktree：

```bash
git worktree add \
  -b agent/session-123 \
  .agentgit/worktrees/session-123 \
  HEAD
```

优点：

- main checkout 保持干净；
- 每个 Agent 有自己的 branch；
- 普通 Git diff / commit / rollback 全部直接可用；
- 最后仍然可以使用普通 Git merge / rebase。

### 5.3 受控文件系统视图

推荐 MVP 使用 FUSE 做一个很薄的 passthrough filesystem。

可选实现轮子：

```text
Python: pyfuse3 / fusepy
Rust:   fuser
```

它不存储文件内容，只做两件事：

1. 根据路径规则决定 `RW / RO / HIDDEN`；
2. 对允许的操作直接转发给 backing worktree。

也就是说：

```text
open/read/write/readdir/rename/unlink
             │
             ▼
        Path Policy
             │
       ┌─────┼─────┐
       ▼     ▼     ▼
      RW    RO   HIDDEN
       │     │
       └─────┴────→ backing worktree
```

**不要在 FUSE 层实现版本管理。**

版本管理永远由 Git 负责。

### 5.4 进程隔离

Linux MVP 推荐直接使用 `bubblewrap`（bwrap）。

目的不是做一个完整 sandbox，而只解决一个问题：

> Agent 只能看到 FUSE view，不能绕过它访问 backing worktree。

概念上：

```text
Host
├── repo/.agentgit/worktrees/session-123   ← Agent 不可见
└── /mnt/agentgit/session-123              ← Agent 看见

bubblewrap namespace
└── /workspace -> /mnt/agentgit/session-123
```

系统运行依赖例如 `/usr`、`/bin`、`/lib` 可以只读挂载进去。

第一版不要顺便实现网络权限、CPU 限制、secret broker 等能力。

---

## 6. 用户体验：把它设计成“给文件上锁”

不要让用户理解 ACL、mount namespace 或 FUSE。

用户只需要理解：

```text
可写
只读锁
隐藏锁
```

暂定 CLI 名称：`agentgit`。

### 6.1 初始化

```bash
agentgit init
```

生成：

```text
.agentgit.toml
```

默认策略建议第一版使用：

```toml
version = 1

default = "rw"
```

原因：MVP 首先验证“给现有工作流加锁”是否自然，而不是强迫用户一开始维护完整 allowlist。

以后可以增加：

```toml
default = "ro"
```

作为更严格模式。

### 6.2 给文件加只读锁

```bash
agentgit lock package-lock.json
agentgit lock generated/**
```

配置结果：

```toml
[[rule]]
path = "package-lock.json"
mode = "ro"

[[rule]]
path = "generated/**"
mode = "ro"
```

### 6.3 隐藏文件

```bash
agentgit hide .env
agentgit hide secrets/**
```

```toml
[[rule]]
path = ".env"
mode = "hidden"

[[rule]]
path = "secrets/**"
mode = "hidden"
```

### 6.4 解锁

```bash
agentgit unlock src/**
```

显式覆盖之前的规则：

```toml
[[rule]]
path = "src/**"
mode = "rw"
```

### 6.5 查看最终规则

```bash
agentgit policy
```

输出：

```text
RW      src/**
RW      tests/**
RO      package-lock.json
RO      generated/**
HIDDEN  .env
HIDDEN  secrets/**
```

规则匹配 MVP 可以直接采用：

> **last matching rule wins**

类似 `.gitignore` 的认知模型，简单且容易实现。

---

## 7. Session 工作流

### 7.1 创建 Session

```bash
agentgit session create --base HEAD
```

内部执行：

```bash
git worktree add \
  -b agent/session-123 \
  .agentgit/worktrees/session-123 \
  HEAD
```

记录：

```text
session id
base commit
branch name
backing worktree path
mount path
policy snapshot
```

建议 metadata：

```json
{
  "id": "session-123",
  "base": "abc123",
  "branch": "agent/session-123",
  "worktree": ".agentgit/worktrees/session-123",
  "mount": "/mnt/agentgit/session-123"
}
```

### 7.2 启动 Agent

```bash
agentgit run session-123 -- <agent-command>
```

内部：

```text
1. mount FUSE controlled view
2. 创建隔离 namespace
3. 将 view 暴露为 /workspace
4. chdir /workspace
5. exec agent command
```

Agent 看到：

```text
/workspace
├── src/                 RW
├── tests/               RW
├── generated/           RO
├── package-lock.json    RO
└── ...
```

`.env` / `secrets/` 不出现在 `readdir` 中。

### 7.3 Agent 修改时发生什么

假设 Agent：

```text
write src/api.ts
```

调用链：

```text
Agent
  ↓ write("/workspace/src/api.ts")
FUSE
  ↓ policy(src/api.ts) == RW
backing worktree
  ↓
真实文件被修改
```

此时在 host 直接执行：

```bash
git -C .agentgit/worktrees/session-123 diff
```

已经能看到修改。

没有额外同步步骤。

---

## 8. 版本管理：全部复用 Git

### 8.1 Status

```bash
agentgit status session-123
```

内部：

```bash
git -C <worktree> status --short
```

### 8.2 Diff

```bash
agentgit diff session-123
```

内部：

```bash
git -C <worktree> diff
```

### 8.3 Checkpoint

```bash
agentgit checkpoint session-123 -m "agent checkpoint"
```

内部：

```bash
git -C <worktree> add -A
git -C <worktree> commit -m "agent checkpoint"
```

Checkpoint 就是普通 Git commit。

不要创造第二套 snapshot 格式。

### 8.4 回滚未提交修改

```bash
agentgit rollback session-123
```

第一版语义：回到当前 branch 最后一个 commit。

内部：

```bash
git -C <worktree> reset --hard HEAD
git -C <worktree> clean -fd
```

因为这个 worktree 是 Session 独占的，所以这个操作边界很清晰。

### 8.5 回到 Session 起点

```bash
agentgit rollback session-123 --base
```

内部：

```bash
git -C <worktree> reset --hard <base-commit>
git -C <worktree> clean -fd
```

### 8.6 完成 Session

```bash
agentgit finish session-123
```

结果只是保留：

```text
agent/session-123
```

后续用户继续使用普通 Git：

```bash
git diff main...agent/session-123
git merge agent/session-123
```

MVP 不实现新的 merge。

---

## 9. FUSE 层应该有多薄

这是整个实现最重要的控制点。

### 9.1 FUSE 不应该做的事情

不要在里面实现：

- Git commit；
- Git branch；
- merge；
- history；
- content-addressed storage；
- Agent provenance；
- database；
- 自动 checkpoint。

### 9.2 FUSE 只做 policy enforcement + passthrough

伪代码：

```python
def open(path, flags):
    mode = policy.resolve(path)

    if mode == HIDDEN:
        raise ENOENT

    if wants_write(flags) and mode != RW:
        raise EROFS

    return backing.open(path, flags)
```

```python
def readdir(path):
    children = backing.readdir(path)

    return [
        child
        for child in children
        if policy.resolve(join(path, child)) != HIDDEN
    ]
```

```python
def unlink(path):
    if policy.resolve(path) != RW:
        raise EROFS

    backing.unlink(path)
```

```python
def rename(src, dst):
    if policy.resolve(src) != RW:
        raise EROFS

    if policy.resolve(dst) != RW:
        raise EROFS

    backing.rename(src, dst)
```

所有 mutation API 都必须统一过同一个 policy resolver。

---

## 10. 必须特别处理的文件系统边界

这些不是“高级功能”，而是 MVP 正确性的最低要求。

### 10.1 `.git`

默认不要把 `.git` 暴露给 Agent。

原因：

- linked worktree 的 `.git` 会指向外部 metadata；
- 直接暴露 Git metadata 会扩大绕过面；
- MVP 已经由 host wrapper 负责 Git 操作。

第一版可以直接内置：

```text
.git => HIDDEN
.agentgit => HIDDEN
```

Agent 如果需要 diff/status，先通过 orchestrator 使用 `agentgit diff/status`。

以后再考虑暴露一个受限 Git proxy。

### 10.2 Symlink

Symlink 是最容易造成绕过的地方。

例如：

```text
visible-link -> ../secrets/token
```

如果只对字符串路径 `visible-link` 做 policy 检查，就可能绕过 `HIDDEN secrets/**`。

MVP 必须有明确规则：

```text
symlink 最终解析目标仍然必须处于 repository root 内，
并且目标路径本身必须通过相同 policy。
```

如果实现成本过高，第一版宁可：

> 对指向 repo root 外部的 symlink 默认拒绝访问。

不要默默放行。

### 10.3 Rename

下面这种操作同时涉及两个权限点：

```text
rename(src, dst)
```

必须保证：

```text
src == RW
AND
dst == RW
```

否则拒绝。

### 10.4 Directory mutation

只读目录不能通过父目录操作间接被删除或替换。

例如：

```text
RO: config/
RW: *
```

不能因为父目录是 RW，就允许：

```bash
rm -rf config
```

Policy 判断必须作用于实际被修改的路径，而不只是父目录。

---

## 11. 一个必须接受的 MVP 限制

有一种需求第一版不要假装已经解决：

> “Agent 本人不能修改这个文件，但 Agent 启动的程序运行时必须能够修改这个文件。”

例如：

```text
runtime.db
```

要求：

```text
Agent shell      不可写
Program runtime  可写
```

如果两者运行在同一个身份、同一个 namespace，并且 Agent 可以执行任意程序，那么仅靠路径权限无法可靠区分两者。

这个问题需要：

```text
process separation / broker / separate runtime sandbox
```

不属于当前 MVP。

当前 MVP 只保证：

```text
运行需要读取，但不应该修改   -> RO 可以解决
运行完全不需要看到           -> HIDDEN 可以解决
Agent 可以修改                -> RW
```

这已经足够验证核心假设。

---

## 12. 推荐的代码结构

尽量保持项目很小。

```text
agentgit/
├── cli/
│   ├── init
│   ├── lock
│   ├── hide
│   ├── unlock
│   ├── session
│   ├── run
│   ├── status
│   ├── diff
│   ├── checkpoint
│   └── rollback
│
├── git/
│   ├── worktree
│   └── commands
│
├── policy/
│   ├── parser
│   └── resolver
│
├── fs/
│   └── fuse_passthrough
│
├── sandbox/
│   └── bubblewrap
│
└── session/
    └── metadata
```

原则：

```text
Git 模块只包装 git CLI
FS 模块只做 path policy
Sandbox 模块只保证 Agent 不能绕过 FS view
Session 模块只拼装上述组件
```

不要让这些模块互相承担职责。

---

## 13. 开发顺序

### Phase 0：先验证实时投影

先不做 HIDDEN，不做 sandbox。

目标：

```text
FUSE view -> backing Git worktree
```

完成：

- passthrough read；
- passthrough write；
- 写后 `git diff` 立即可见。

成功标准：

```text
Agent/View 修改文件
≈
直接修改 Git worktree
```

### Phase 1：只读锁

增加：

```text
RW / RO
```

覆盖：

- write；
- truncate；
- create；
- unlink；
- rename；
- mkdir / rmdir；
- chmod（可直接拒绝）。

成功标准：

```text
RO 文件仍可用于 build/test，
但任何 mutation 都失败。
```

### Phase 2：隐藏锁

增加：

```text
HIDDEN
```

要求：

- 不出现在 `readdir`；
- `stat/open` 返回 `ENOENT`；
- rename / link / symlink 不可绕过。

### Phase 3：隔离 backing path

接入 bubblewrap。

成功标准：

```text
Agent 只能看到 /workspace
无法通过绝对路径找到 backing worktree
```

### Phase 4：Git Session

接入：

```text
git worktree
status
diff
checkpoint
rollback
finish
```

到这里就应该停下来开始真实使用。

**不要立刻开发 Phase 5。**

先让真实 Agent 跑真实任务，记录失败模式。

---

## 14. MVP 测试矩阵

### 14.1 RW

```text
读取已有文件                PASS
修改已有文件                PASS
创建文件                    PASS
删除文件                    PASS
重命名文件                  PASS
mkdir                       PASS
git diff 立即看到变化        PASS
rollback 恢复                PASS
```

### 14.2 RO

```text
读取                        PASS
stat                        PASS
执行/引用                   PASS
write                       FAIL
truncate                    FAIL
unlink                      FAIL
rename away                 FAIL
rename over                 FAIL
chmod                       FAIL
```

### 14.3 HIDDEN

```text
readdir 看不到              PASS
stat                        ENOENT
open                        ENOENT
read                        ENOENT
write                       ENOENT / reject
通过 symlink 访问           FAIL
通过 backing path 访问      FAIL
```

### 14.4 Git

```text
Agent 写入后 git diff       PASS
checkpoint                  PASS
checkpoint 后继续修改        PASS
rollback 到 HEAD             PASS
rollback 到 base             PASS
main checkout 不被污染       PASS
普通 git merge branch        PASS
```

---

## 15. MVP 成功与否，不看功能数量

这个实验真正要回答的不是：

> “我们能不能造一个更强的 Git？”

而是下面几个非常具体的问题。

### 问题 1

用户是否真的愿意维护类似：

```text
lock package-lock.json
hide .env
lock generated/**
```

这样的规则？

还是规则维护本身很烦？

### 问题 2

RO 是否真的“不影响 Agent 工作”？

还是很多构建工具会偷偷改 lockfile / generated file / cache，导致大量失败？

### 问题 3

Agent 遇到 `EROFS` 后，能否自然调整方案？

还是会反复尝试修改同一个锁定文件？

### 问题 4

HIDDEN 是否会让 Agent 因缺少上下文而明显降低完成率？

### 问题 5

“Agent 修改实时进入 Git worktree”是否比：

```text
临时 workspace -> 最后 diff/apply
```

更简单、更可靠？

### 问题 6

实际使用中最需要的 rollback 粒度是什么？

```text
整个 Session
checkpoint
单文件
单次 tool call
```

不要提前猜答案。

先收集真实任务的数据。

---

## 16. 第一版明确不做什么

为了保护实验质量，以下功能全部暂缓：

```text
新的版本控制协议
新的 merge 算法
CRDT
semantic merge
multi-agent scheduler
remote server
web UI
权限数据库
RBAC
network sandbox
secret broker
Agent provenance
自动 checkpoint
按 tool call 版本化
跨平台支持
Windows
macOS 完整支持
```

这些以后都可能有价值，但现在会让我们失去核心问题。

---

## 17. 一句话架构

最终 MVP 可以压缩成：

```text
                Git Repository
                     │
              git worktree
                     │
                     ▼
             Backing Worktree
                     │
              FUSE Policy View
          ┌──────────┼──────────┐
          │          │          │
          ▼          ▼          ▼
          RW         RO       HIDDEN
          │          │
          └──────┬───┘
                 ▼
          /workspace for Agent
                 │
             bubblewrap
                 │
                 ▼
               Agent
```

版本管理始终在外层：

```text
status      -> git status
diff        -> git diff
checkpoint  -> git commit
rollback    -> git reset / clean
finish      -> ordinary git branch
```

文件权限始终在视图层：

```text
read/write path
      ↓
policy resolver
      ↓
RW / RO / HIDDEN
```

两者通过同一个 backing worktree 自然连接。

---

## 18. 最重要的产品原则

### 原则一：不要创造第二份状态

权威状态只有两个：

```text
Git history
Git worktree 当前 delta
```

不要再创建自己的 file snapshot database。

### 原则二：锁发生在写入之前

不是 Agent 改完以后检查：

```text
“你改了不该改的文件。”
```

而是在 mutation 发生时立即拒绝。

### 原则三：运行需要 ≠ 可修改

`RO` 是核心能力，不要把“不能改”等同于“删除”。

### 原则四：用户只需要理解“锁”

底层可以是：

```text
Git
FUSE
bubblewrap
mount namespace
```

但用户界面应该只是：

```text
lock
hide
unlock
```

### 原则五：先实验，再完善理论

当前想法未必是最终答案。

MVP 的价值不是证明设计正确，而是尽快暴露：

```text
哪些锁真的有用？
哪些锁破坏 Agent 工作流？
Agent 如何响应只读失败？
隐藏上下文会不会伤害效果？
Git worktree 是否真的是合适的版本边界？
```

当这些问题有真实数据后，再决定是否需要：

```text
更细的 capability
runtime-only 文件
per-process permission
更细粒度 checkpoint
多 Agent merge
甚至新的 VCS abstraction
```

---

# 最终 MVP 定义

如果只能保留一句话：

> **在真实 Git worktree 上提供一个可配置的 RW / RO / HIDDEN 文件系统视图，让 Agent 的合法修改实时落入 Git 工作区，并继续用原生 Git 完成 diff、checkpoint 和 rollback。**

如果只能保留四个命令：

```bash
agentgit lock <path>
agentgit hide <path>
agentgit run -- <agent>
agentgit rollback
```

如果只能验证一个假设：

> **“给 Agent 的工作目录增加简单、强制、可理解的文件锁，同时保持修改实时处于 Git 管理之下”，是否比今天的 unrestricted checkout 更适合作为 Agent 编程的默认工作方式？**

先把这个问题跑通。
