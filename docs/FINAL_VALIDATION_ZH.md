# Flow2API 本地安全账号池最终验收记录

更新时间：2026-08-14

本文只记录公开的阶段、状态、数量、耗时类别和是否存在媒体，不记录账号、Cookie、Token、API Key、浏览器 Profile、完整 URL、提示词、媒体内容或响应正文。

## 当前结论

- 自动门禁：425 项测试通过，另有 190 个子测试通过。
- 隔离交付环境：从新建虚拟环境安装 `requirements.txt` 与 `requirements-test.txt` 后，同样 425 项通过。
- 干净交付扫描：禁止运行态路径 0，秘密格式命中 0。
- Windows 开机自启：真实系统任务处于启用状态。
- 服务重启：健康检查 200，端口只有一个监听实例。
- 登录恢复：服务重启后 5 个账号保持可用，无需插件配对或重新登录 Google。
- 启动资源：服务启动时项目浏览器进程为 0，不会预先打开全部账号浏览器。
- 旧孤儿浏览器：修复前为 21 个项目进程、3 个旧根；受控重启后为 0。
- 真实图片：完成并返回媒体。
- 真实 10 秒视频：完成并返回媒体。
- 真实三槽并行图片：3/3 完成，3/3 返回媒体。
- 空闲回收：任务后项目浏览器一度为 39 个进程，随后归零；持续观察到 601 秒仍为 0。
- 回收后再唤醒：再次提交图片可自动启动浏览器，完成并返回媒体。

## 真实生命周期验收

```text
服务启动
  -> 项目浏览器为 0
  -> 有任务时按需启动
  -> 图片、视频和三槽并行均返回媒体
  -> 空闲后项目浏览器归零
  -> 601 秒观察期内保持为零
  -> 新任务到来后再次自动唤醒并成功返回媒体
```

这证明当前实现不是“登录时长期打开很多浏览器”，而是保留账号登录状态，在任务需要时启动，任务结束后自动回收，下次任务再恢复。

## 自动质量门禁

- 全量：425 passed，190 subtests passed。
- 编译：`python -m compileall -q src tests scripts main.py` 通过。
- 增量差异：开发仓库 `git diff --check` 通过，只有既有 LF/CRLF 转换提示。
- 影策兼容、安全和资源生命周期合同通过。
- 浏览器上限、单号优先并发、按需启动、重启恢复、分页和 0/1/200/500 个合成账号规模合同通过。
- 模型菜单只展示当前公开能力；Omni Flash 提供 8 秒和 10 秒，Veo 3.1 菜单保持已验证的 8 秒能力。

## 干净交付边界

最终目录不包含：

- `.git` 历史（在最终目录重新初始化干净历史）；
- `venv`、pytest 缓存、Python 缓存；
- `data`、`tmp`、数据库、WAL/journal；
- 浏览器 Profile 或运行状态；
- `.env`、本机 `setting.toml`、日志和 HAR；
- 真实无人值守运行的临时输出。

交付扫描器只输出计数。最终扫描结果：

```text
forbidden_path_count=0
secret_pattern_count=0
```

上游源码中原本存在的公开 Flow 客户端标识只通过“精确相对路径 + 精确不可逆指纹”豁免；相同形状出现在其他路径或发生变化仍会报警。测试目录也没有被整体排除。

## 使用方式

1. 首次安装运行依赖：`venv\Scripts\python.exe -m pip install -r requirements.txt`。
2. 需要运行完整验收时，再安装：`venv\Scripts\python.exe -m pip install -r requirements-test.txt`。
3. 双击 `start-flow2api.cmd` 或 `start-flow2api.pyw` 启动；重复点击不会启动第二个监听实例。
4. 管理页添加 Google 账号时只需正常登录一次；Google 后续要求验证码或风控确认时仍需人工完成。
5. 影策接入方法见 `docs/USER_GUIDE_ZH.md`，与原版差异见 `docs/FORK_DIFFERENCES_ZH.md`。

## 尚在继续的验收

- 低频无人值守稳定性正在继续执行；已完成的单轮结果不冒充数小时终态。
- Zeabur、Linux 云端登录入口和生产部署不属于本次 Windows 本地交付范围。

## 2026-08-14 最终补充验收

- 最新全量自动门禁：427 passed，另有 190 个 subtests passed；唯一 warning 为既有的 Windows Proactor 兼容提示，不是失败。
- 最新编译门禁：`python -m compileall -q src tests scripts main.py` 通过。
- 最新增量差异门禁：`git diff --check` 通过，仅有既有 LF/CRLF 提示。
- Yingce 图片兼容端点真实 HTTP 验收：create HTTP 200，status=completed，has_media=true。
- Yingce 10 秒视频兼容端点真实 HTTP 验收：create/poll/content HTTP 均为 200，status=completed，has_media=true，media_bytes>0。
- 上述图片和视频都经过正常 HTTP 鉴权并复用现有 `GenerationHandler -> Flow -> 媒体解析` 主链；没有绕过生产端点或新增第二套生成实现。
- 真实验收使用仅存在于受控验证进程内存中的临时值；没有读取、修改或输出当前 API Key，也没有把该值写入仓库、报告或压缩包。
- 验收结束后服务已恢复正常配置：health=200，listener_count=1；任务结束后的项目浏览器进程为 0。
- 已完成的一轮约 4 小时低频无人值守记录为 16 次计划、15 次成功且 15 次返回媒体、1 次 transport_error，浏览器最终归零；但结束时 service_alive=false，因此这份旧证据不冒充全绿终态。新的全绿终态验收继续由控制器执行。
