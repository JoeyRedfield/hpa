# hpa · 会成长的个人助手

> 一份 intent 文档。短。只说我最初想做什么、能到什么效果、最重要的那几个设计——不是使用手册。

## 我想做一个什么样的东西

一个 7×24 跑在我 Mac 上、不需要我盯着对话窗口的助手：它读我的邮件和 Obsidian 笔记库，把值得处理的事变成**待办和草稿**，推到手机让我一句话确认；我每纠正它一次，它就**多一条规则**，下次不再犯同样的错。

它服务的不是"演示一个 Agent"，而是我每天真实在做的事：看邮件 → 判断哪封要回 → 翻资料 → 写回复 → 跟进。

## 效果（做完应该能看到这些）

- 新邮件到达 5 分钟内，变成「今日待办 + 回复草稿」出现在手机页面上
- 草稿可以在手机上调措辞，**点确认才发送**；发出去的是我改过的版本
- 反复轮询邮箱不会重复处理同一封、更不会重复发送
- 我纠正过的偏好会落成规则文件，**新会话**里对同类邮件生效（例：不要把我发的会议邀请当成需要回复的邮件）
- 同一套上下文能跨工具做事：邮件里提到的项目 → 检索笔记库 → 引用原文 → 写进草稿
- 所有改动都在 git 里可追溯，包括"我为什么这么设计"

## 最重要的三个设计

**1. 笔记库是唯一真相。** 助手只读 `~/Documents/notes`，不复制资料、不搞两套数据。它自己的规则、记忆、状态单独放在本项目里。

**2. 确定性与智能分离。** 轮询、去重、状态机、发送、确认全部是脚本，可测试、可重放；只有"这封邮件在说什么、该不该回、怎么回"交给 LLM。这样 token 花在该花的地方，出错也能定位。

**3. 一切对外动作过确认闸门。** 发邮件、提交表单这类不可撤销动作，必须由我在手机上明确点确认；敏感字段（密码、身份证号）永不出本机、永不进 prompt。

## 与作业原文的偏差

Lab 1 原文举例要求接南大 `smail` 邮箱和 `ehall` 办事大厅，我不是南大学生、没有这些账号。按其真正的意图——**实现一个会成长的个人助手**——用我真实在用的 QQ 邮箱和真实事务替代，验收标准等价（持续获取、去重、草稿确认、提交前确认）。

## 现在到哪了

- ✅ intent（本文件）
- ✅ spec：[.scratch/growing-assistant/spec.md](.scratch/growing-assistant/spec.md)
- ✅ M1 个人数据库：`hpa index` 增量索引 680 篇（P0 651 / P1 28 / P2 1，约 2.6s）；`hpa ask` 中文检索并给 `文件:行号` 定位；P1/P2 在 SQL 层被 `purpose=prompt` 挡在模型之外。设计见 [ADR 0001](docs/adr/0001-sqlite-fts5-trigram.md)。
- ✅ M2 邮件接入：`hpa mail sync` 用 `UIDVALIDITY+UID` 游标增量拉取，Message-ID 去重（三次同步不增），UIDVALIDITY 变化时重扫不重复；`rules/mail-triage.md` 是人工可改的规则 DSL（12 条），`hpa mail triage` 规则优先、命中即定型。
- ✅ M3a 流水线（未发送）：`hpa process` = 模型分诊 → 抽取待办 → 检索 P0 笔记 → 起草带 `[引用]` 的回复，草稿状态 `proposed` 进队列；全程幂等，`--dry-run` 只走一遍不写库。LLM 走 DeepSeek（OpenAI 兼容），provider 已抽象。
- ✅ M3b 确认闸门：`hpa web` 起手机端一页（PWA，绑定 `127.0.0.1:8787`，手机经 Tailscale 访问）；草稿可在手机上改（改一次 = 新版本，须重新确认）、驳回、记录纠正；**只有点「确认发送」才走 SMTP**。发送状态机 `proposed → sending → sent`，失败退回 `proposed` 并显示原因；崩溃在 `sending` 不自动重发，由人核对后收尾。
- ✅ M4 成长闭环：手机上记一条纠正 → `hpa growth run` 让模型把它改写成 `rules/mail-triage.md` 的一条规则、`rules/reply-style.md` 的写作要求、或 `memory/preferences.md` 的一条记忆；新规则先过 DSL 语法校验（写坏就回滚），可判定的同时生成 `tests/cases/*.md` 回归样例，`hpa growth check` 随时验证。改动只写本仓库、带「来源：纠正 #N」注释，用 `git diff` 审完再提交——**新会话重新加载规则即生效**。
- ✅ M5 组合与归档：`hpa daemon` 每 5 分钟一轮「同步 → 规则分诊 → 模型流水线」，单轮失败只记日志不杀循环，模型/邮箱凭据缺失时自动降级；配 `HPA_PUSH_URL` 可在有新事时 POST 一条消息到手机（不配也能在手机页看到，页面 20s 自刷新）。`hpa export` 把已发送的邮件连同原文、待办、最终稿、引用、纠正记录导出成带 frontmatter 的 Markdown 到 `outbox/`，由我手动搬进笔记库（保持 vault 只读）。至此走通一条完整流程：**邮件进来 → 待办+草稿 → 手机确认发送 → 导出归档**。

## 对应 Lab1 的五项能力

| 作业要求 | 这里的等价实现 | 看哪里 |
| --- | --- | --- |
| 个人数据库：索引、检索、随资料更新、定位原文 | 只读索引 Obsidian vault，`hpa index` 增量（mtime+size），`hpa ask` 返回 `文件:行号` | `src/hpa/kb.py`、ADR 0001 |
| 持续关联邮箱：新邮件/历史/草稿，重复不重复处理 | QQ IMAP `UIDVALIDITY+UID` 游标 + Message-ID 去重 + 发送两阶段状态机 | `src/hpa/mail.py`、`src/hpa/send.py` |
| 办理事务：查询 + 至少一种材料/表单准备 + 提交前确认 | 工作事务流：邮件 → 待办与截止时间 → 检索资料 → 起草 → 手机确认后发送；不可撤销动作（发送/改稿/驳回）全部过确认闸门 | `src/hpa/pipeline.py`、`hpa web` |
| 手机端联动：发起/进度/改草稿/确认，不依赖桌面窗口 | 本机 PWA（Tailscale 内网），`hpa daemon` 后台跑，页面 20s 自刷新 | `src/hpa/web/`、`scripts/phone-access.sh` |
| 能力组合：共享上下文、跨工具、一条愿意反复用的流程 | 一条完整链路：邮件 → 待办 → 检索笔记库 → 引用原文起草 → 确认发送 → 导出归档 | `tests/test_export_daemon.py`（端到端） |
| 成功流程与纠正沉淀，Git 管理，新会话复用 | `hpa growth`：纠正 → 规则/记忆 + 回归样例；改动只写仓库、可 `git diff` 审 | `src/hpa/growth.py`、ADR 0002 |

## 跑起来（当前形态）

```bash
./bin/hpa index                      # 索引笔记库（增量，可反复跑）
./bin/hpa mail sync                  # 拉新邮件（需在 secrets.env 配 QQ 授权码，见下）
./bin/hpa mail triage                # 规则优先分诊
./bin/hpa process                    # 模型分诊 → 抽取待办 → 检索 → 起草
./bin/hpa web                        # 手机端：待办 / 草稿确认 / 纠正
./bin/hpa growth list                # 看有哪些纠正还没固化
./bin/hpa growth run                 # 纠正 → 规则/记忆 + 回归样例（写进 git）
./bin/hpa growth check               # 跑回归样例，确认老规则没被写坏
./bin/hpa export                     # 已发送邮件 → outbox/*.md（手动搬进笔记库）
./bin/hpa daemon                     # 常驻：每 5 分钟同步+分诊+起草（--once 单轮调试）
```

凭据放 `~/.config/hpa/secrets.env`（不进 git）：

```ini
HPA_MAIL_USER=you@qq.com
HPA_MAIL_PASSWORD=QQ邮箱IMAP授权码
HPA_LLM_API_KEY=sk-...
HPA_WEB_TOKEN=自己定一个长随机串
```

手机访问：`http://<Mac 的 Tailscale IP>:8787/?token=<HPA_WEB_TOKEN>`（首次带 token，之后写 Cookie）。

### 让它常开 + 手机访问

```bash
./scripts/install-daemon.sh install   # launchd 常驻：开机自启、挂了自动拉起
./scripts/install-daemon.sh status    # 看是否在跑
./scripts/phone-access.sh 8787        # tailscale serve，并打印带 token 的手机 URL
```

`phone-access.sh` 用 `tailscale serve` 把本机 8787 暴露到 tailnet（只对你自己的设备可见），
打印形如 `https://your-machine.your-tailnet.ts.net/?token=...` 的地址，手机浏览器打开后
「添加到主屏幕」即为 PWA。token 在 `~/.config/hpa/secrets.env` 的 `HPA_WEB_TOKEN`，
没配则用自动生成的 `state/web-token`。
