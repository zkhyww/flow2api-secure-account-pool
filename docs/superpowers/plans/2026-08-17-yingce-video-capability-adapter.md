# Yingce / Jutian Video Capability Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让影策/巨天通过现有 `/v1/videos` 明确选择 Omni Flash 与 Veo 3.1 的文生、首帧、首尾帧和参考图生成方式，并把客户端的 720P 安全映射为上游原生输出。

**Architecture:** 保留现有账号池、`GenerationHandler`、浏览器、并发和任务轮询链路，只扩充 `public_model_catalog.py` 的公开视频能力条目，并让 `yingce_adapter.py` 完全由该目录解析模型、生成方式、图片数量和原生清晰度。内置测试页继续消费同一目录，不维护第二份模型表。

**Tech Stack:** Python 3.11、FastAPI、unittest/pytest、原生 JavaScript 测试页。

## Global Constraints

- 只写 `src/core/public_model_catalog.py`、`src/api/yingce_adapter.py`、`static/test.html`、`static/test-page-capabilities.js`、对应测试和本功能文档。
- 绝不修改当前 dirty 的登录持久化文件、账号池、数据库、浏览器、验证码、并发、`FlowClient`、`GenerationHandler` 或 Framefield Studio。
- 继续保留已有旧模型 ID 的可调用兼容；公开视频目录只展示最新版、可说明且有现有底层路径的能力。
- 不恢复浏览器插件依赖。
- `720p`、`720P`、空值和 `native` 都表示上游原生输出；不得触发放大。
- 1080P、4K、2160P、480P 等未纳入本轮真实验收的组合必须 fail-closed，不得静默降档。
- Omni 10 秒参考图不开放；Omni 8 秒参考图开放 1–3 张。
- Veo Quality 不开放 Ingredients/References；Veo Fast References 开放 1–3 张。
- 错误响应不得包含提示词、账号、Token、Cookie、API Key、上游 URL、原始响应或内部异常。
- 生产代码必须先有按预期失败的测试，再做最小实现。

---

### Task 1: Public video capabilities describe generation methods truthfully

**Files:**
- Modify: `src/core/public_model_catalog.py`
- Modify: `tests/test_canonical_model_catalog.py`

**Interfaces:**
- Produces: public capability entries with `generation_mode`, `generation_modes`, `supports_images`, `min_images`, `max_images`, `image_semantics`, `usage_guide`, `unsupported_notes`, native `resolution` option, and exact `compatibility_map`.
- Preserves: current image capabilities and existing text-to-video capability IDs.

- [ ] **Step 1: Write failing catalog tests**

  Add table-driven assertions for these exact public video selections:

  | Capability ID | Display purpose | Images | Duration | Landscape model | Portrait model |
  |---|---|---:|---:|---|---|
  | `omni-flash` | 文生视频 | 0 | 8/10 | `omni` / `omni_10s` | `omni_portrait` / `omni_portrait_10s` |
  | `omni-flash-references` | 参考图生视频 | 1–3 | 8 | `omni` | `omni_portrait` |
  | `veo-3.1-lite` | 文生视频 | 0 | 8 | `veo_3_1_t2v_lite_landscape_8s` | `veo_3_1_t2v_lite_portrait_8s` |
  | `veo-3.1-lite-first-frame` | 首帧生视频 | 1 | 8 | `veo_3_1_i2v_lite_landscape_8s` | `veo_3_1_i2v_lite_portrait_8s` |
  | `veo-3.1-lite-first-last` | 首尾帧生视频 | 2 | 8 | `veo_3_1_interpolation_lite_landscape_8s` | `veo_3_1_interpolation_lite_portrait_8s` |
  | `veo-3.1-fast` | 文生视频 | 0 | 8 | `veo_3_1_t2v_fast_landscape_8s` | `veo_3_1_t2v_fast_portrait_8s` |
  | `veo-3.1-fast-first-frame` | 首帧生视频 | 1 | 8 | `veo_3_1_i2v_s_fast_landscape_8s_fl` | `veo_3_1_i2v_s_fast_portrait_8s_fl` |
  | `veo-3.1-fast-first-last` | 首尾帧生视频 | 2 | 8 | `veo_3_1_i2v_s_fast_landscape_8s_fl` | `veo_3_1_i2v_s_fast_portrait_8s_fl` |
  | `veo-3.1-fast-references` | 参考图生视频 | 1–3 | 8 | `veo_3_1_r2v_fast_landscape` | `veo_3_1_r2v_fast_portrait` |
  | `veo-3.1-quality` | 文生视频 | 0 | 8 | `veo_3_1_t2v_landscape_8s` | `veo_3_1_t2v_portrait_8s` |
  | `veo-3.1-quality-first-frame` | 首帧生视频 | 1 | 8 | `veo_3_1_i2v_s_landscape_8s` | `veo_3_1_i2v_s_portrait_8s` |
  | `veo-3.1-quality-first-last` | 首尾帧生视频 | 2 | 8 | `veo_3_1_i2v_s_landscape_8s` | `veo_3_1_i2v_s_portrait_8s` |

  Assert that every entry includes 16:9/9:16, `resolution=native`, an unambiguous Chinese usage guide, the correct generation method, and no Quality References entry.

- [ ] **Step 2: Run RED**

  Run:
  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_canonical_model_catalog.py -q`

  Expected: failures because image/video modes are currently hidden behind four text-only entries and metadata is absent.

- [ ] **Step 3: Implement the minimal catalog model**

  Generalize `_video_capability` to accept explicit `generation_mode`, image bounds, model maps, usage copy and unsupported notes. Build only the twelve entries listed above. Preserve all image entries and legacy callable aliases.

- [ ] **Step 4: Run GREEN**

  Run the same catalog test command and require exit code 0.

### Task 2: The compatibility endpoint resolves native resolution and exact image mode

**Files:**
- Modify: `src/api/yingce_adapter.py`
- Modify: `tests/test_yingce_adapter_contract.py`

**Interfaces:**
- Consumes: Task 1 public catalog.
- Produces: `_resolve_video_model(model, size, seconds, reference_count)` returning only a model whose capability image bounds match the uploaded file count.
- Produces: `_apply_video_resolution` accepting blank/native/720p without changing the model.

- [ ] **Step 1: Write failing request-contract tests**

  Add one focused test per behavior:

  - `720P`, `720p`, `native` and blank keep the resolved model unchanged and reach the existing handler.
  - `omni-flash-references` with 1 and 3 images maps to 8-second `omni`/`omni_portrait`; 0 or 4 images fails before task creation.
  - `omni-flash` with images and `omni-flash-references` at 10 seconds fail without dropping images or falling back to text generation.
  - Lite first-frame accepts exactly one image; Lite first-last accepts exactly two.
  - Fast first-frame accepts exactly one, Fast first-last exactly two, Fast References 1–3.
  - Quality first-frame/first-last accept 1/2 respectively; no Quality References alias is resolvable.
  - 1080P/4K/2160P/480P return `unsupported_video_parameters` and produce no task, reservation or generation call.
  - Invalid image count returns `unsupported_video_parameters` with safe `allowed` metadata and without reflecting prompt or file content.

- [ ] **Step 2: Run RED**

  Run:
  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_yingce_adapter_contract.py -q`

  Expected: failures for 720P and all new generation-mode capability IDs.

- [ ] **Step 3: Implement catalog-driven request resolution**

  Read and size-limit `input_reference[]` before final model resolution, pass only the image count to the resolver, validate the selected capability's min/max bounds, then pass the existing byte list unchanged to `GenerationHandler`. Do not infer Veo References from a generic Veo entry; the caller must select the explicit References capability.

- [ ] **Step 4: Stabilize errors**

  Return `unsupported_video_parameters` for invalid resolution, duration, aspect ratio or image count. Include only the selected public capability name and allowed duration/aspect/native-resolution/image-count values.

- [ ] **Step 5: Run GREEN**

  Run the same adapter contract command and require exit code 0.

### Task 3: Built-in test page makes the usage obvious

**Files:**
- Modify: `static/test.html`
- Modify: `static/test-page-capabilities.js`
- Modify: `tests/test_test_page_contract.py`
- Modify: `tests/test_admin_test_capability.py`

**Interfaces:**
- Consumes: the same public catalog returned by `/api/test/models`.
- Produces: capability name, generation method, image purpose/count, duration, aspect ratio, native-resolution note and validation state visible before submit.

- [ ] **Step 1: Write failing UI contract tests**

  Assert that the helper exposes each capability's generation method and usage guide, that image upload is required for min-images > 0, and that incompatible image counts disable submission with a Chinese explanation. Assert that no hard-coded second video model table is added to HTML/JS.

- [ ] **Step 2: Run RED**

  Run:
  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_test_page_contract.py tests/test_admin_test_capability.py -q`

- [ ] **Step 3: Implement the minimum UI rendering**

  Keep the existing model/aspect/duration layout. Add a compact read-only line for generation method and usage guide; drive upload required/optional state from `min_images`/`max_images`; show `原生清晰度（外部客户端请选择 720P）`. Do not add a client-side model map.

- [ ] **Step 4: Run GREEN**

  Run the same UI contract command and require exit code 0.

### Task 4: Regression, documentation and handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/USER_GUIDE_ZH.md`
- Modify: `docs/FORK_DIFFERENCES_ZH.md`
- Modify: `docs/MILESTONE_REPORT.md`

**Interfaces:**
- Documents: exact model choices and how to choose them in Flow2API, Yingce and Jutian.

- [ ] **Step 1: Update usage tables**

  Replace the old claim that Omni is text-only. List the twelve clear capability IDs, generation methods, exact image counts, duration and the instruction that external clients select 720P for native output. State that Omni 10-second references and Quality References are not exposed.

- [ ] **Step 2: Run focused regression**

  Run:
  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_canonical_model_catalog.py tests/test_yingce_adapter_contract.py tests/test_test_page_contract.py tests/test_admin_test_capability.py -q`

- [ ] **Step 3: Run full gates**

  Run full pytest with the project venv, `python -m compileall src`, `git diff --check`, and the repository's existing credential scanner. Require zero failures; preserve any unrelated dirty login-session files.

- [ ] **Step 4: Independent real QA**

  After automated GREEN, start the existing local service and use the existing four logged-in accounts without reading credentials. Submit and poll one task for: Omni text, Omni References, Veo text, Veo first-frame, Veo first-last and Veo Fast References. For each, require terminal completed state and playable media; record only sanitized status/model-mode/duration/aspect evidence.

- [ ] **Step 5: Stop on real RED**

  If a real generation path fails, retain its RED evidence, classify whether the failure is request mapping, account availability, reCAPTCHA or upstream support, and make only the smallest TDD-backed correction. Do not loop real generations blindly and do not mark unverified combinations available.
