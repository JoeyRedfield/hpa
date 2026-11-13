# 06: 纠正 → 固化 → 建议改变

**What to build:** 健身 tab 的纠正框写一句话，落库为 scope=fitness 的纠正；`hpa growth run` 识别 scope，用健身的固化提示产出两类提案——追加规则到 `rules/fitness-advice.md`，或结构化改写训练画像四键之一（不重复）。同一份数据，固化前后建议真的不同；邮件纠正路径完全不变。端到端测试覆盖 记录 → 建议 → 纠正 → 固化 → 再建议。

**Blocked by:** 02, 04

**Status:** done

- [x] 纠正提交后待固化数 +1；corrections 带 scope=fitness、message_id 为空。
- [x] FakeLLM 返回 rule 提案：规则文件追加一条，带「来源：纠正 #N」。
- [x] FakeLLM 返回 profile 提案（key=weekly_days, value=2）：画像对应行改成 2，文件里没有重复键行。
- [x] 非法目标（如 src 下的文件）或 profile 目标配 rule kind 被拒绝，文件不动。
- [x] 固化上下文含画像与 4 周摘要，不含备注原文。
- [x] 同一份数据，固化前建议说「加重量 / 按 3 天」，固化后不默认加重量、按 2 天算。
- [x] 邮件纠正的既有测试全绿，行为不变。
- [x] 一条端到端测试走完 记录 → 建议 → 纠正 → growth run → 再建议。
