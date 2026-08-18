import json
import re
import subprocess
import unittest
from pathlib import Path


class TestPageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("static/test.html").read_text(encoding="utf-8")
        helper_path = Path("static/test-page-capabilities.js")
        cls.helper = helper_path.read_text(encoding="utf-8") if helper_path.exists() else ""

    def _function_source(self, name):
        match = re.search(
            rf"(?:async\s+)?function\s+{re.escape(name)}\([^)]*\)\s*\{{.*?^\}}",
            self.html,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, f"test page has no {name}()")
        return match.group(0)

    def _run_node(self, script):
        result = subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_page_auto_issues_memory_only_admin_capability(self):
        self.assertIn("/api/admin/test-capability", self.html)
        self.assertIn("X-Flow2API-Test-Capability", self.html)
        self.assertNotIn("localStorage", self.html)
        self.assertNotIn("sessionStorage", self.html)
        self.assertRegex(self.html, r"let\s+testCapability\s*=\s*['\"]{2}")

    def test_default_requests_use_same_origin_test_endpoints(self):
        self.assertIn("/api/test/models", self.html)
        self.assertIn("/api/test/chat/completions", self.html)
        self.assertRegex(self.html, r"credentials\s*:\s*['\"]include['\"]")

    def test_manual_api_key_controls_and_diagnostics_are_preserved(self):
        self.assertRegex(self.html, r"<details[^>]*id=['\"]advancedAuthSettings['\"]")
        self.assertIn('id="apiKey"', self.html)
        self.assertIn('id="baseUrl"', self.html)
        self.assertIn('id="diagnosticTokenId"', self.html)
        self.assertIn("/api/admin/test-accounts", self.html)
        self.assertIn("/api/test/model-availability?diagnostic_token_id=", self.html)
        self.assertIn("diagnostic_token_id", self.html)

    def test_page_has_parameterized_image_and_video_controls_with_labels(self):
        self.assertIn('role="tablist"', self.html)
        self.assertIn('data-media-mode="image"', self.html)
        self.assertIn('data-media-mode="video"', self.html)
        for control_id in (
            "capabilitySelect",
            "imageAspectRatio",
            "imageResolution",
            "videoAspectRatio",
            "videoDuration",
        ):
            self.assertIn(f'id="{control_id}"', self.html)
            self.assertRegex(
                self.html,
                rf"<label[^>]*for=['\"]{control_id}['\"]",
                f"{control_id} must have a keyboard/screen-reader label",
            )
        self.assertIn(
            "原生清晰度（外部客户端请选择 720P）",
            self.html,
        )
        self.assertNotIn("Omni Flash 可选 8 秒或 10 秒", self.html)
        self.assertIn("/static/test-page-capabilities.js", self.html)

    def test_formal_menu_has_no_legacy_or_long_list_copy(self):
        for forbidden in (
            "待验证",
            "其他模型",
            "推荐模型",
            "Gemini 2.5",
            "Imagen preview",
            "Veo 2",
            "【优先测试】",
            "【未验证/可能有等级要求】",
        ):
            self.assertNotIn(forbidden, self.html)
        self.assertNotRegex(
            self.html,
            r"<option[^>]*>[^<]*(?:Extend|续写)[^<]*</option>",
        )
        self.assertNotIn("const FALLBACK_MODELS", self.html)
        self.assertNotIn("buildModelDisplayGroups", self.html)
        self.assertNotIn("renderSidebar", self.html)

    def test_pure_resolver_maps_image_and_video_parameters_and_hides_hidden_combinations(self):
        self.assertTrue(self.helper, "static/test-page-capabilities.js must exist")
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const image = {
  capability_id: "nano-banana-2",
  validation_status: "validated",
  compatibility_map: [
    {parameters:{aspect_ratio:"4:3",resolution:"2K"}, model_id:"gemini-3.1-flash-image-four-three-2k", validation_status:"validated"},
    {parameters:{aspect_ratio:"9:16",resolution:"4K"}, model_id:"hidden-image-id", validation_status:"hidden"}
  ]
};
const video = {
  capability_id: "veo-3.1-fast",
  validation_status: "validated",
  compatibility_map: [
    {parameters:{aspect_ratio:"9:16",duration_seconds:"8"}, model_id:"veo_3_1_t2v_fast_portrait_8s", validation_status:"validated"}
  ],
  actions: [{
    id:"extend",
    validation_status:"validated",
    model_map:{"16:9":"veo_3_1_extend","9:16":"veo_3_1_extend_portrait"}
  }]
};
process.stdout.write(JSON.stringify({
  image: api.resolveCapabilityModelId(image, {aspect_ratio:"4:3",resolution:"2K"}),
  hidden: api.resolveCapabilityModelId(image, {aspect_ratio:"9:16",resolution:"4K"}),
  video: api.resolveCapabilityModelId(video, {aspect_ratio:"9:16",duration_seconds:"8"}),
  extend: api.resolveExtendModelId(video, "9:16")
}));
"""
        )
        self.assertEqual(
            {
                "image": "gemini-3.1-flash-image-four-three-2k",
                "hidden": "",
                "video": "veo_3_1_t2v_fast_portrait_8s",
                "extend": "veo_3_1_extend_portrait",
            },
            payload,
        )

    def test_admin_only_hidden_diagnostics_list_only_real_hidden_mappings(self):
        self.assertTrue(self.helper, "static/test-page-capabilities.js must exist")
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const catalog = [
  {
    capability_id:"omni-flash",
    display_name:"Omni Flash",
    model_type:"video",
    validation_status:"validated",
    compatibility_map:[
      {parameters:{aspect_ratio:"16:9",duration_seconds:"8"},model_id:"omni",validation_status:"validated"},
      {parameters:{aspect_ratio:"16:9",duration_seconds:"10"},model_id:"omni_10s",validation_status:"hidden"},
      {parameters:{aspect_ratio:"9:16",duration_seconds:"10"},model_id:"omni_portrait_10s",validation_status:"hidden"},
      {parameters:{aspect_ratio:"1:1",duration_seconds:"10"},model_id:"",validation_status:"hidden"}
    ]
  },
  {
    capability_id:"veo-3.1-fast",
    display_name:"Veo 3.1 Fast",
    model_type:"video",
    validation_status:"validated",
    compatibility_map:[
      {parameters:{aspect_ratio:"16:9",duration_seconds:"8"},model_id:"veo_fast_8s",validation_status:"validated"}
    ]
  }
];
process.stdout.write(JSON.stringify(
  api.listHiddenDiagnosticMappings(catalog).map(item => ({
    label:item.label,
    modelId:item.modelId,
    parameters:item.parameters
  }))
));
"""
        )
        self.assertEqual(
            [
                {
                    "label": "Omni Flash · 16:9 · 10 秒",
                    "modelId": "omni_10s",
                    "parameters": {
                        "aspect_ratio": "16:9",
                        "duration_seconds": "10",
                    },
                },
                {
                    "label": "Omni Flash · 9:16 · 10 秒",
                    "modelId": "omni_portrait_10s",
                    "parameters": {
                        "aspect_ratio": "9:16",
                        "duration_seconds": "10",
                    },
                },
            ],
            payload,
        )

    def test_unverified_diagnostic_control_is_collapsed_and_reuses_generation(self):
        self.assertRegex(
            self.html,
            r"<details(?=[^>]*id=['\"]unverifiedDiagnosticSettings['\"])(?=[^>]*class=['\"][^'\"]*hidden)[^>]*>",
        )
        details_tag = re.search(
            r"<details[^>]*id=['\"]unverifiedDiagnosticSettings['\"][^>]*>",
            self.html,
        ).group(0)
        self.assertNotRegex(details_tag, r"\sopen(?:\s|=|>)")
        self.assertIn('id="unverifiedDiagnosticSelect"', self.html)
        self.assertIn('id="btnUseUnverifiedDiagnostic"', self.html)
        self.assertRegex(
            self.html,
            r"<label[^>]*for=['\"]unverifiedDiagnosticSelect['\"]",
        )

        render = self._function_source("renderUnverifiedDiagnosticControls")
        activate = self._function_source("activateUnverifiedDiagnostic")
        generate = self._function_source("generate")
        self.assertIn("!apiKey && testCapability", render)
        self.assertIn("listHiddenDiagnosticMappings", render)
        self.assertIn("option.value = String(index)", render)
        self.assertNotIn("option.textContent = diagnostic.modelId", render)
        self.assertIn("STATE.selectedModel = diagnostic.modelId", activate)
        self.assertIn("STATE.modelConfig = getModelMeta", activate)
        self.assertNotIn("fetch(", activate)
        self.assertIn("STATE.unverifiedDiagnosticActive", generate)
        self.assertIn("/api/test/chat/completions", generate)
        self.assertNotIn("模型: ${STATE.selectedModel}", generate)
        self.assertIn("publicCapabilityLabel", generate)

    def test_duration_selection_prefers_current_value_then_capability_default(self):
        self.assertTrue(self.helper, "static/test-page-capabilities.js must exist")
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const omni = [{value:"8"}, {value:"10"}];
const veo = [{value:"8"}];
process.stdout.write(JSON.stringify({
  omniDefault: api.selectPreferredOptionValue(omni, "", "10"),
  omniKeepsEight: api.selectPreferredOptionValue(omni, "8", "10"),
  veoFallsBack: api.selectPreferredOptionValue(veo, "10", "8")
}));
"""
        )
        self.assertEqual(
            {
                "omniDefault": "10",
                "omniKeepsEight": "8",
                "veoFallsBack": "8",
            },
            payload,
        )

    def test_capability_usage_helper_exposes_method_guide_and_exact_image_requirement(self):
        self.assertTrue(self.helper, "static/test-page-capabilities.js must exist")
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const capability = {
  model_type:"video",
  generation_mode:"first_last_frame_to_video",
  generation_modes:[{id:"first_last_frame_to_video",label:"首尾帧生视频"}],
  image_semantics:"恰好 2 张图片：第 1 张首帧，第 2 张尾帧",
  usage_guide:"按首帧、尾帧顺序上传恰好 2 张图片并选择 8 秒。",
  min_images:2,
  max_images:2
};
process.stdout.write(JSON.stringify({
  usage: api.getCapabilityUsageMeta(capability),
  one: api.validateCapabilityImageCount(capability, 1),
  two: api.validateCapabilityImageCount(capability, 2),
  three: api.validateCapabilityImageCount(capability, 3)
}));
"""
        )
        self.assertEqual(
            {
                "generationMode": "first_last_frame_to_video",
                "generationModeLabel": "首尾帧生视频",
                "imageSemantics": "恰好 2 张图片：第 1 张首帧，第 2 张尾帧",
                "usageGuide": "按首帧、尾帧顺序上传恰好 2 张图片并选择 8 秒。",
                "minImages": 2,
                "maxImages": 2,
                "requiresImages": True,
                "nativeResolutionNote": "原生清晰度（外部客户端请选择 720P）",
            },
            payload["usage"],
        )
        self.assertEqual(
            {"valid": False, "message": "需要上传 2 张图片后才能生成"},
            payload["one"],
        )
        self.assertEqual(
            {"valid": True, "message": "图片数量符合当前能力要求"},
            payload["two"],
        )
        self.assertEqual(
            {"valid": False, "message": "当前能力最多接受 2 张图片"},
            payload["three"],
        )

    def test_video_usage_rendering_and_image_count_gate_are_catalog_driven(self):
        self.assertIn('id="videoCapabilityGuide"', self.html)
        update = self._function_source("updateResolvedModel")
        refresh = self._function_source("refreshGenerateButton")
        previews = self._function_source("renderImagePreviews")
        self.assertIn("getCapabilityUsageMeta", update)
        self.assertIn("videoCapabilityGuide", update)
        self.assertIn("nativeResolutionNote", update)
        self.assertIn("validateCapabilityImageCount", refresh)
        self.assertIn("imageValidation.valid", refresh)
        self.assertIn("imageValidation.message", refresh)
        self.assertIn("refreshGenerateButton()", previews)
        self.assertNotIn("veo_3_1", self.html + self.helper)
        self.assertNotIn("omni_portrait", self.html + self.helper)
        self.assertNotIn("const VIDEO_MODELS", self.html + self.helper)
        self.assertNotIn("const VIDEO_CAPABILITY_MAP", self.html + self.helper)

    def test_validated_catalog_leaves_hidden_diagnostic_list_empty(self):
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const catalog = [{
  capability_id:"omni-flash",
  display_name:"Omni Flash",
  model_type:"video",
  validation_status:"validated",
  compatibility_map:[
    {parameters:{aspect_ratio:"16:9",duration_seconds:"8"},model_id:"omni",validation_status:"validated"},
    {parameters:{aspect_ratio:"9:16",duration_seconds:"8"},model_id:"omni_portrait",validation_status:"validated"},
    {parameters:{aspect_ratio:"16:9",duration_seconds:"10"},model_id:"omni_10s",validation_status:"validated"},
    {parameters:{aspect_ratio:"9:16",duration_seconds:"10"},model_id:"omni_portrait_10s",validation_status:"validated"}
  ]
}];
process.stdout.write(JSON.stringify(api.listHiddenDiagnosticMappings(catalog)));
"""
        )
        self.assertEqual([], payload)

    def test_formal_video_duration_is_rendered_from_visible_capability_options(self):
        duration_select = re.search(
            r"<select[^>]*id=['\"]videoDuration['\"][^>]*>.*?</select>",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(duration_select)
        self.assertNotIn("disabled", duration_select.group(0))
        self.assertNotRegex(duration_select.group(0), r"value=['\"](?:4|6)['\"]")

        apply_selection = self._function_source("applyCapabilitySelection")
        update = self._function_source("updateResolvedModel")
        self.assertRegex(
            apply_selection,
            r'listVisibleOptions\(\s*capability,\s*"duration_seconds"',
        )
        self.assertIn("selectPreferredOptionValue", apply_selection)
        self.assertIn("defaults.duration_seconds", apply_selection)
        self.assertIn('document.getElementById("videoDuration").value', update)
        self.assertNotIn('duration_seconds: "8"', update)
        self.assertIn(
            'addEventListener("change", handleVideoDurationChange)',
            self.html,
        )
        self.assertIn(
            'addEventListener("change", handleVideoAspectChange)',
            self.html,
        )

    def test_hidden_image_combination_is_removed_when_parameters_change(self):
        self.assertRegex(self.html, r"function\s+handleImageAspectChange\(")
        self.assertRegex(self.html, r"function\s+handleImageResolutionChange\(")
        aspect_change = self._function_source("handleImageAspectChange")
        resolution_change = self._function_source("handleImageResolutionChange")
        self.assertIn("listVisibleOptions", aspect_change)
        self.assertIn("listVisibleOptions", resolution_change)
        self.assertIn(
            'addEventListener("change", handleImageAspectChange)',
            self.html,
        )
        self.assertIn(
            'addEventListener("change", handleImageResolutionChange)',
            self.html,
        )

    def test_pure_visibility_contract_lists_only_supported_capabilities(self):
        self.assertTrue(self.helper, "static/test-page-capabilities.js must exist")
        payload = self._run_node(
            """
const api = require("./static/test-page-capabilities.js");
const catalog = [
  {capability_id:"visible", model_type:"image", validation_status:"validated"},
  {capability_id:"tier", model_type:"image", validation_status:"membership_required"},
  {capability_id:"hidden", model_type:"image", validation_status:"hidden"},
  {capability_id:"video", model_type:"video", validation_status:"validated"}
];
process.stdout.write(JSON.stringify({
  image: api.listVisibleCapabilities(catalog, "image").map(item => item.capability_id),
  video: api.listVisibleCapabilities(catalog, "video").map(item => item.capability_id)
}));
"""
        )
        self.assertEqual({"image": ["visible", "tier"], "video": ["video"]}, payload)

    def test_selection_resolves_to_compatible_id_before_generation(self):
        self.assertRegex(self.html, r"function\s+updateResolvedModel\(")
        update = self._function_source("updateResolvedModel")
        self.assertIn("resolveCapabilityModelId", update)
        self.assertIn("STATE.selectedModel", update)

        generate = self._function_source("generate")
        self.assertRegex(generate, r"const\s+body\s*=\s*\{\s*model:\s*STATE\.selectedModel")
        self.assertIn("stream: true", generate)

    def test_generation_uses_sse_until_done(self):
        body = self._function_source("generate")
        self.assertIn("getReader()", body)
        self.assertIn("TextDecoder", body)
        self.assertIn("[DONE]", body)
        self.assertNotIn("await resp.json()", body)

    def test_successful_video_keeps_private_extend_id_and_uses_aspect_action(self):
        get_safe_media_url = self._function_source("getSafeMediaUrl")
        use_last_video = self._function_source("useLastVideoForExtend")
        render_result = self._function_source("renderResult")
        payload = self._run_node(
            f"""
const elements = {{
  outputResult: {{
    children: [],
    style: {{}},
    textContent: "",
    appendChild(child) {{ this.children.push(child); }},
    set innerHTML(_value) {{ this.children = []; }}
  }},
  videoSourceGroup: {{ style: {{}} }},
  videoSourceStatus: {{ textContent: "" }},
  btnGenerate: {{ disabled:false, textContent:"" }}
}};
function makeElement(tagName) {{
  return {{
    tagName,
    children: [],
    style: {{}},
    textContent: "",
    appendChild(child) {{ this.children.push(child); }}
  }};
}}
const document = {{
  getElementById(id) {{ return elements[id] || null; }},
  createElement: makeElement
}};
const window = {{ location: {{ origin: "http://localhost:8000" }} }};
const capability = {{
  capability_id:"veo-3.1-fast",
  model_type:"video",
  actions:[{{
    id:"extend",
    validation_status:"validated",
    model_map:{{"16:9":"veo_3_1_extend","9:16":"veo_3_1_extend_portrait"}}
  }}]
}};
const ALL_CAPABILITIES = {{"veo-3.1-fast": capability}};
function resolveExtendModelId(entry, aspectRatio) {{
  const action = (entry.actions || []).find(item => item.id === "extend");
  return action ? action.model_map[aspectRatio] || "" : "";
}}
const STATE = {{
  selectedModel:"veo_3_1_t2v_fast_portrait_8s",
  selectedCapabilityId:"veo-3.1-fast",
  parameters:{{aspect_ratio:"9:16",duration_seconds:"8"}},
  modelConfig:{{type:"video",requires_video_id:false}},
  generating:false,
  extendSourceMediaId:"",
  lastSuccessfulVideo:null
}};
function canGenerateSelectedModel(meta, sourceId) {{
  return Boolean(meta) && (!meta.requires_video_id || Boolean(sourceId));
}}
function getModelMeta(entry, requiresVideoId) {{
  return {{type:entry.model_type, requires_video_id:Boolean(requiresVideoId)}};
}}
function refreshGenerateButton() {{}}
{get_safe_media_url}
{use_last_video}
{render_result}
const privateId = "private-media-generation-id";
renderResult("<video src='http://localhost:8000/tmp/result.mp4' data-media-id='" + privateId + "' controls></video>");
const button = elements.outputResult.children.find(child => child.tagName === "button");
const video = elements.outputResult.children.find(child => child.tagName === "video");
const visibleBefore = elements.outputResult.children.map(child => child.textContent || "").join(" ");
const clicked = button && button.onclick();
const visibleAfter = visibleBefore + " " + elements.videoSourceStatus.textContent;
process.stdout.write(JSON.stringify({{
  buttonText: button && button.textContent,
  clicked,
  selectedModel: STATE.selectedModel,
  retainedPrivately: STATE.extendSourceMediaId === privateId,
  idRenderedAsText: visibleAfter.includes(privateId),
  idCopiedToVideoDom: Boolean(video && video.dataset && Object.values(video.dataset).includes(privateId))
}}));
"""
        )
        self.assertEqual(
            {
                "buttonText": "续写此视频",
                "clicked": True,
                "selectedModel": "veo_3_1_extend_portrait",
                "retainedPrivately": True,
                "idRenderedAsText": False,
                "idCopiedToVideoDom": False,
            },
            payload,
        )
        self.assertNotIn('id="mediaGenerationId"', self.html)

    def test_result_preview_uploads_errors_and_capability_retry_are_preserved(self):
        self.assertRegex(self.html, r"function\s+getSafeMediaUrl\(value\)")
        self.assertIn('"flow-content.google"', self.html)
        self.assertIn('video.src = safeUrl', self.html)
        self.assertIn('img.src = safeUrl', self.html)
        self.assertIn('[media-url-hidden]', self.html)
        self.assertIn('id="imageFileInput"', self.html)
        self.assertRegex(self.html, r"function\s+handleFiles\(")
        self.assertIn("publicErrorClasses", self.html)

        retry = self._function_source("fetchWithTestCapabilityRetry")
        script = f"""
let testCapability = "stale";
let issueCalls = 0;
const requestHeaders = [];
async function issueTestCapability() {{
  issueCalls += 1;
  testCapability = "fresh";
  return true;
}}
async function fetch(_url, options) {{
  requestHeaders.push(options.headers["X-Flow2API-Test-Capability"]);
  return {{ status: requestHeaders.length === 1 ? 401 : 200 }};
}}
{retry}
(async () => {{
  const response = await fetchWithTestCapabilityRetry("/api/test/models", {{method:"GET"}});
  process.stdout.write(JSON.stringify({{status:response.status, issueCalls, requestHeaders}}));
}})().catch(error => {{ process.stderr.write(error.stack); process.exit(1); }});
"""
        self.assertEqual(
            {"status": 200, "issueCalls": 1, "requestHeaders": ["stale", "fresh"]},
            self._run_node(script),
        )

    def test_model_usage_copy_membership_caveat_and_responsive_layout_exist(self):
        self.assertIn('id="modelUsage"', self.html)
        self.assertIn("4K 组合需要高级会员", self.html)
        self.assertRegex(self.html, r"@media\s*\(max-width:\s*768px\)")
        self.assertIn("flex-wrap", self.html)


if __name__ == "__main__":
    unittest.main()
