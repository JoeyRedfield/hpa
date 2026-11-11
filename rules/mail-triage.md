# 邮件分诊规则（人工可改）

这个文件是**给人看的规则**，同时被程序当配置读：`bin/hpa mail triage` 会先跑它，
命中的邮件直接定型，**不进 LLM**——省 token，也让行为可预测、可回归。

## 怎么改

- 只改下面 ` ```rules ` 代码块里的行；块外随便写。
- 一行一条：`<动作> if <字段> <操作> "<值>"`，多个条件用 ` and ` 连接，全部满足才命中。
- 字段：`from` / `to` / `subject` / `body` / `all`（四者拼起来）。
- 操作：`contains` / `startswith` / `endswith` / `equals`；前面加 `not ` 取反。
- 动作：
  - `ignore`：不用管（自动通知、退订、机器人）
  - `needs_reply`：需要我回
  - `fyi`：知道即可，可能生成待办
- 从上到下匹配，**先命中先用**；都没命中就留待模型判断。
- 改完跑 `bin/hpa rules check` 验证语法；测试在 `tests/test_rules.py`。

```rules
ignore if from contains "noreply"
ignore if from contains "no-reply"
ignore if from contains "notification@"
ignore if from contains "notifications@"
ignore if subject contains "退订"
ignore if subject contains "unsubscribe"
needs_reply if subject startswith "Re:"
needs_reply if subject startswith "回复:"
needs_reply if subject startswith "Re："
fyi if subject contains "周报"
fyi if subject contains "月报"
fyi if subject contains "通知"
```

## 已知边界

- 规则只看纯文本字段，不懂语义；模糊邮件交给模型。
- 不要在这里写「删除邮件」这类动作：对外/破坏性动作必须过确认闸门（见 spec §3）。
