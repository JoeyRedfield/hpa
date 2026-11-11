# 实机跑通记录（2026-11-11）

凭据放 `~/.config/hpa/secrets.env`（不进 git）：QQ 邮箱 IMAP 授权码 + DeepSeek key + web token。
下面是第一次接真实数据时的实际输出（邮箱内容已脱敏到主题级别）。

## 1. 个人数据库

```console
$ ./bin/hpa index
扫描 ~/Documents/notes
新增 680 / 更新 0 / 未变 0 / 删除 0（2.64s）
索引共 680 篇（P0:651 P1:28 P2:1）

$ ./bin/hpa ask 验收标准
命中 3 篇（purpose=prompt，只给 P0）：
1. 项目/…  → 项目/…md:42
...
```

再跑一次增量：`新增 0 / 未变 680`，说明索引跟得上资料变化。
`hpa ask` 返回 `文件:行号`，草稿里的引用直接落成 `[1] 项目/....md:11`。

## 2. 邮件持续关联

```console
$ ./bin/hpa mail sync
邮箱 INBOX：拉取 50 / 新增 50 / 重复 0
游标推进到 UID 4167（UIDVALIDITY 1650852696）

$ ./bin/hpa mail sync        # 再跑一次：0 新增
邮箱 INBOX：拉取 0 / 新增 0 / 重复 0
```

首次只取最近 50 封，之后按 `UIDVALIDITY+UID` 增量；同一封邮件靠 Message-ID 去重。

## 3. 规则优先 + 模型兜底

```console
$ ./bin/hpa mail triage
扫描 50 封：已定型 19 / 未命中 31
按规则：fyi:3 ignore:16

$ ./bin/hpa process -n 20
完成：分诊 20 / 抽取 8 / 起草 1 / 新待办 6
   · <tencent_…> → needs_reply
   · <tencent_…> 抽取 6 条待办
   · <tencent_…> 起草 → Re: 某项目接口文档
草稿已进确认队列：等手机端确认后才会发送（M3b 提供页面）。
```

草稿真的引用了笔记库里的项目文档（P0），两条引用分别是
`项目/项目开发/…:11` 和 `项目/项目开发/…:40`；6 条待办是从邮件正文抽出来的待确认项。

## 4. 手机端确认闸门

```console
$ ./bin/hpa web        # 127.0.0.1:8787
$ curl -s '…/api/state?token=…'
{"status":{"counts":{"messages":50,"todos_open":6,"drafts_pending":1,"sent":0}, …}}
```

页面：状态条 → 草稿卡（可改 / 驳回 / 记录纠正 / 确认发送）→ 待办 → 最近纠正；
`?token=` 首次进入后写 Cookie，之后免 token。手机经 `scripts/phone-access.sh`（tailscale serve）访问。

## 5. 成长循环（真模型预演）

```console
$ ./bin/hpa growth run --dry-run
预演：待处理 1 / 成功 1 / 跳过 0
   · #1（预演）rules/reply-style.md: 回复客户时不要用「…」这类客套开场，直接给结论和待确认项。
```

真模型选对了目标文件（写作风格而不是分诊规则），且 `--dry-run` 不落盘。
正式跑 `growth run` 会追加带 `来源：纠正 #N` 注释的文本 + `tests/cases/*.md` 样例，
然后 `growth check` 回归、`git diff` 人审。

## 6. 归档

```console
$ ./bin/hpa export --message-id '<tencent_…>'
导出 1 篇到 outbox/（移动端不写笔记库，请手动搬进 vault）：
   · outbox/2026-10-30-某项目接口文档-6c7946.md
```

文件带 frontmatter（message_id / 发件人 / 分诊）、原文、待办、最终稿、引用、纠正记录；
`outbox/` 在 `.gitignore` 里，由用户手动搬进笔记库。

## 尚未由本次会话执行的动作（需要人）

- 点「确认发送」真实发一封（闸门逻辑已有测试；真实 SMTP 只登录验证过，未发信）；
- 把某条真实纠正 `growth run` 后 `git diff` 审阅提交；
- `./scripts/install-daemon.sh install` 让 `hpa daemon` 开机常驻；
- `./scripts/phone-access.sh` 在手机上打开并「添加到主屏幕」。
