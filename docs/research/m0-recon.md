# M0 技术侦察：smail 邮箱 / ehall / 本机环境

> 时间：2026-11-11 · 方式：网络文档 + 本机只读命令 · 状态：**部分未证实**（标注见各处）
> 目的：在写任何代码前，确认两项最可能推翻架构的外部接入可行性。

## 结论速览

| 问题 | 结论 | 对架构的影响 |
| ---- | ---- | ------------ |
| smail 能否程序化收发邮件 | **能**：腾讯企业邮，IMAP 993 / SMTP 465(SSL) | 邮件走 `imaplib`/`smtplib`，**零第三方依赖**，不需要浏览器 |
| ehall 能否自动化 | **能但脆**：CAS 单点登录 + Vue SPA + 图形/拼图验证码 + 半年一次短信验证 | 必须 **Playwright + 复用人工登录态**，不做纯 requests 登录、不自己解滑块 |
| 本机环境 | Python 3.14.6 (Homebrew) + uv 0.7.20 + Node 24 + Chrome 已装；Playwright 未装 | `uv` 管依赖；Playwright 用本机 Chrome（`channel="chrome"`）省下载 |

## 1. 学生邮箱 smail.nju.edu.cn

- `smail.nju.edu.cn` 无 A 记录；网页端 `mail.smail.nju.edu.cn` → 14.17.27.155。学生邮箱托管在**腾讯企业邮**。
- 服务器：`imap.exmail.qq.com:993`、`smtp.exmail.qq.com:465`、`pop.exmail.qq.com:995`；三端口 TCP 实测可达。账号须填**完整邮箱地址**。
- 前置条件：网页版「设置 → 客户端设置」需开启 IMAP/POP/SMTP；若已启用微信扫码/安全登录，须使用**客户端专用密码（授权码）**，在网页端获取。
- 官方页：[学生邮箱说明](https://itsc.nju.edu.cn/1a/8f/c21586a334479/pagem.htm)、[授权码办法](https://itsc.nju.edu.cn/1a/8e/c21586a334478/page.htm)（页面为 2015 年制）。
- **未证实**：2026-02 认证升级后学生侧是否仍全量开放；腾讯会自动关闭长期不用的客户端账号。
- 缓解：常驻轮询（每 5 分钟）天然规避"长期不用"；M0 第一步就用授权码实机验证一次。

## 2. ehall.nju.edu.cn

- 登录：`authserver.nju.edu.cn/authserver/login`（Apereo CAS），HTML 内含 `execution`/`lt`/`_eventId`，成功后发 `CASTGC`。未登录访问 `ehall.nju.edu.cn/appShow?appId=...` 会 302 到 `/login?service=...`。
- 页面：`ehall.nju.edu.cn/ywtb-portal/index.html` 是**金智"一网通办"前端 SPA**（`require.min.js` + `chunk-*.js`），HTML 无正文，**服务端渲染不可用**。
- 业务后端：`ehallapp.nju.edu.cn` 有 JSON 接口（如 `cjcx` 成绩、`wdkb` 课表，`appShow`/`changeAppRole` 建会话）——参考 [soundadam/nju-openapi](https://github.com/soundadam/nju-openapi)（已抓 OpenAPI yaml）。
- 验证码：`/authserver/getCaptcha.htl` 返回 image/jpeg，已升级为**拼图滑块**；另有半年一次短信验证 + 扫码登录。所以纯 `requests` 登录不稳定。
- 开源参考：[Do1e/NJUlogin](https://github.com/Do1e/NJUlogin)（PyPI，requests 登录）、[treehey/NJU-Login-Pro](https://github.com/treehey/NJU-Login-Pro)（本地 CNN 解滑块）、MarcWebber/FUCK-NJUEHALL（选课，非办事大厅，已失效）。**无官方 API 文档（未证实）。**

## 3. 本机环境

- `python3` 3.14.6（/opt/homebrew）、`uv` 0.7.20（`uvx` 可用）、Node v24.19.0、Homebrew 7.0.11。
- `/Applications` 有 **Google Chrome**；`python3 -c "import playwright"` 失败（未安装）。

## 对架构的三个决定

1. **邮件适配器不引入浏览器**：`imaplib` + `smtplib` + 授权码，后台轮询 5 分钟，幂等靠 IMAP `UIDVALIDITY + UID` 游标。
2. **ehall 适配器用 Playwright 复用登录态**：人工登录一次 → 存 `storage_state.json`（已 gitignore）→ 后续 headless 复用；登录态过期再回到人工。**不实现滑块破解**（既脆弱又不必要）。业务数据优先走 `ehallapp` JSON 接口，表单提交走页面操作 + 确认闸门。
3. **selenium 风格的重试要克制**：ehall 页面结构随时可能变，适配器要小、要能被单独替换——这是 Lab 2 加需求时不至于塌方的关键。

## 待验证事项（M0 清单）

- [ ] 用授权码 IMAP 登录成功并拉到收件箱 UID 列表
- [ ] SMTP 发一封测试邮件到自己的邮箱
- [ ] Playwright 打开 authserver 完成一次人工登录，导出 `storage_state.json`
- [ ] 用该登录态访问 ehall 一个只读页面（如成绩/课表）并抓到 DOM
- [ ] 确认 `ehallapp.nju.edu.cn` 的 JSON 接口是否需要额外 header/token
