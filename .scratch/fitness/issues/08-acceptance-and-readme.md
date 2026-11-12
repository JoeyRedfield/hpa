# 08: 端到端验收与 README

**What to build:** 按 Q15 从零跑完整验收：seed → 建议 → 纠正 → growth → 建议改变 → clear；补 README 的健身效果与使用说明；手工确认 30 秒记录一次。

**Blocked by:** 03, 05, 06, 07

**Status:** ready-for-agent

- [ ] 干净数据目录下 demo seed，手机切健身 tab，点建议：≤3 条，含深蹲 2.0t 停滞与每周 3 天频率。
- [ ] 纠正一句话后 growth run，git diff 同时出现规则与画像改动。
- [ ] 再点建议：不再默认加重量，频率按 2 天。
- [ ] demo seed --clear 后真实记录不受影响。
- [ ] 从打开记录表单到保存一次训练，手工计时 ≤30 秒（含 1 个动作 5 组）。
- [ ] README「效果 / 现在到哪了」更新，含启动与演示命令。
- [ ] 全量测试通过（pytest）。
