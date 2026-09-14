# 更新日志

记录本项目的每次重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
项目目前**没有打版本标签**，所以条目用**日期**做标题（将来引入 tag 后可平行换成版本号）。

条目分类：

- **新增** Added：新功能、新端点、新脚本
- **变更** Changed：行为变化、重构、目录与配置调整
- **修复** Fixed：缺陷修复
- **安全** Security：凭据、权限、泄露相关

---

## [未发布]

下次发布前，把改动累积在这一节；发布时改写成当天的日期标题。

### 修复

- **网关假死自愈（监听器看门狗）**：Windows 上 asyncio 的 Proactor 事件循环遇到并发
  `accept` 出错时，会关闭监听 socket 且不再重新 accept（`proactor_events.py:863-870`）。
  结果是**进程活着、服务状态仍是 RUNNING、但再也接不到任何连接**。SCM 看不到进程退出，
  失败恢复策略因此不会触发——实测该状态下网关挂了 11 分钟、期间零请求：
  日志有 `Accept failed on a socket` / `OSError: [WinError 64]`，之后服务完全静默。
  现增加看门狗线程：每 `ADMIN_WATCHDOG_INTERVAL`（默认 20s）对 `GET /gw/health`
  发起真实 TCP 探活，连续 `ADMIN_WATCHDOG_FAILURES`（默认 3）次失败即记录诊断并
  主动退出进程，交由服务管理器在 5s 内拉起一个全新监听器
- **失败恢复策略其实从未生效**：`service_admin.py install` 会打印「5 秒自动重启」，
  但 SCM 侧实际为空（实测 `Actions = ()`、`ResetPeriod = 0`），所以进程真崩溃也不会
  被拉起。现在服务每次启动自检该配置并补写（服务以 LocalSystem 运行，有权限改自己），
  `service_admin.py status` 也改为如实显示配置状态，不再假定成功
- **真实积分回写不再「每请求一线程」**：`_fetch_real_credits` 原先对每个估算请求起一个
  `threading.Thread` 并在其中 `sleep(60)`，等于「每请求占一个线程 60 秒」。一旦上游开始
  不回传 `credit` 字段，QPS 上到两位数就是几百个并发线程。改为有界队列（2048）+ 单
  worker：worker 按最早到期时间休眠后逐条回写，队列满则丢弃并告警——估算值本身已落库，
  回写只是把估算修正为真实值，丢一条不影响记账
- **`service_admin.py status` 改用真实 HTTP 探活**：原先只做 TCP connect，假死状态下
  会误判为健康；现在会明确报出「无响应 —— 疑似假死」
- **后台页面依赖全部本地化**：`/admin` 原先从 `cdn.tailwindcss.com` 与 `cdn.jsdelivr.net`
  拉 Tailwind 与 FontAwesome。一旦机器无外网出口、或系统代理（如 `127.0.0.1:10808`）未开，
  两个请求双双 `ERR_PROXY_CONNECTION_FAILED`，页面退化成无样式裸 HTML——登录框掉进页头、
  统计卡片竖排、Tab 与表格全部走形。现改为引用仓库内 `admin/static/vendor/` 的
  `tailwind.min.js`（Play CDN 3.0.0 构建）与 FontAwesome 6.5.2（CSS + woff2），
  后台在内网 / 离线 / 代理异常环境下均正常渲染，也不再向第三方发出请求

---

## [2026-09-12]

### 新增

- **猫猫旅行巡检 + 对话活跃上报**：两个新的定时任务（`cat_travel` / `activity_report`），
  契约对齐 Go 版实现——无猫自动领养（+300 积分）、按 `travel/status` 派出与领奖、
  每号上报 N 条活跃事件点亮连登并解锁领养门槛
- **账号列表「活跃」列**：展示成长中心连登天数（复用余额刷新的同一会话，无额外请求开销）
- **旅行中明细回程 ETA**：按上游 `arrive_at` / `server_now` 毫秒口径估算，字段缺失就不显示
- **OAuth 一键加号**；客户端身份按凭据 `domain` 推断（workbuddy / codebuddy 两套 UA）
- **前端引号注入修复**

### 变更

- **代理重试骨架收敛**：五个 `/v1/*` 端点（chat / responses / anthropic，各分流式与非流式）
  原本各持一份重复的重试循环，统一收敛为 `_proxy_loop`；协议差异经
  `make_consumer` / `emit_client_error` / `emit_exhausted` 三个回调注入。
  附带行为统一：非流式也走共享连接池并记录延迟、异常分支的错误分类更准确
- **`backend.py` 拆包**：271 行单文件 → `admin/backend/`（http / session / checkin / growth），
  对外 API 100% 兼容
- **调度器任务实现移出**：`admin/scheduler.py` 413 → 139 行，任务实现移到
  `admin/tasks/`（一个任务一个文件），调度框架只管轮询与分发
- **项目目录归拢**：新增 `core/`（内核模块）、`docs/`（部署文档）、`deploy/`（Docker）、
  `examples/`（客户端接入示例）、`scripts/`（本机脚本）；`main.py` 与
  `service_admin.py` 作为入口留在根目录
- **仓库脱离 fork 关系**：本仓库原为 `xiaofan6ya/workbuddy2api` 的 fork，
  现重建为**独立仓库**（提交历史完整保留，URL 不变）。GitHub 上不再显示
  「领先/落后上游」的对比条与「同步复刻」按钮
- **测试固化**：`tests/` 下 20 项（代理重试骨架 11 + 猫猫旅行状态机 9）

### 修复

- **session 死亡改三振机制**：连续命中死亡标记 3 次才禁用账号，前 2 次只做 10 分钟冷却；
  纯网络抖动不再被误判为 session 死亡（改为 `server` 类，5 次才冷却且永不禁用）

### 安全

- **`.env` 移出版本库**：它此前已被提交并推送到公开仓库，内含后台密码与
  converter API Key。文件已从 git 移除（本地保留），但**历史提交里的旧凭据无法靠删除消除，
  这两个值必须轮换**

---

## [2026-09-11]

### 新增

- **`/v1/messages`（Anthropic 协议）**：带 API Key 配额与用量记账，让 Claude Code 一类
  只会说 Anthropic 协议的客户端直接吃号池
- **Windows 服务宿主 `service_admin.py`** 与安装 / 卸载脚本：以系统服务方式跑
  `admin.server:app`，由 SCM 管理生命周期
- **部署文档与镜像**：`DEPLOY_SEALOS.md`、`DEPLOY_WINDOWS.md`，以及 admin 独立部署镜像
  `Dockerfile.admin`（单端口单进程）

### 修复

- 内嵌 `/gw` 凭据初始化；启动回显补齐端点清单
- 启动地址显示与设备 Token 获取优化
- 模型配置页支持按倍率排序（PR #3）

### 文档

- README 补充 `/v1/responses` 与 `/v1/messages` 网关路由及 Anthropic 接入说明

---

## [2026-09-03]

### 修复

- 可用积分统计改用**当前周期剩余额度**（原来用的是账号层级总剩余，体验版用完后仍显示有额度）
- `turing_helper.js` 改为 SDK 目录自动发现，不再写死 `D://workbuddy`，
  修复 TuringShieldSDK 在不同机器上的加载失败

---

## [2026-09-02]

- 初始提交：workbuddy2api 网关与 admin 管理后台
- 添加项目截图（`images/`）并修正 `.gitignore`（不再误忽略图片资源）

---

## 怎么维护这份日志

1. **什么时候写**：每次合并一个有意义的功能 / 修复 / 重构，就往顶部 `[未发布]` 里加一行。
   不要等到发版时再回忆——那时候最容易漏。
2. **写给谁看**：用户和未来的自己。写"变了什么、为什么变"，不要只贴 commit 标题。
   例如不写「改 proxy」，而写「五个端点的重试循环收敛为一份，新增端点不用再复制重试逻辑」。
3. **发布时**：把 `[未发布]` 改名成当天日期，再新建一节空的 `[未发布]`。
4. **不确定归哪类**：影响用户可感知行为 → 变更；修坏了的东西 → 修复；全新的东西 → 新增；
   涉及凭据 / 权限 / 泄露 → 安全。
5. **辅助生成**：`git log --format="%ad | %s" --date=short` 可以列出日期与提交标题，
   拿来对照着补条目，比翻 GitHub 快。
