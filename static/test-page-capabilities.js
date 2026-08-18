(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.Flow2ApiCapabilityUi = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SELECTABLE_STATES = new Set(["validated", "membership_required"]);

  function isSelectableStatus(value) {
    return SELECTABLE_STATES.has(String(value || ""));
  }

  function listVisibleCapabilities(catalog, mediaType) {
    return (Array.isArray(catalog) ? catalog : [])
      .filter(entry => (
        entry
        && entry.model_type === mediaType
        && isSelectableStatus(entry.validation_status)
      ))
      .slice()
      .sort((left, right) => (
        Number(left.catalog_order ?? Number.MAX_SAFE_INTEGER)
        - Number(right.catalog_order ?? Number.MAX_SAFE_INTEGER)
      ));
  }

  function mappingMatches(mapping, parameters) {
    const expected = mapping && mapping.parameters;
    if (!expected || !isSelectableStatus(mapping.validation_status)) return false;
    return Object.keys(expected).every(key => (
      String(expected[key]) === String((parameters || {})[key] ?? "")
    ));
  }

  function resolveCapabilityModelId(capability, parameters) {
    if (!capability || !isSelectableStatus(capability.validation_status)) return "";
    const mapping = (capability.compatibility_map || [])
      .find(candidate => mappingMatches(candidate, parameters));
    return mapping ? String(mapping.model_id || "") : "";
  }

  function selectPreferredOptionValue(options, currentValue, defaultValue) {
    const values = (Array.isArray(options) ? options : [])
      .map(option => String(
        option && option.value !== undefined && option.value !== null
          ? option.value
          : ""
      ));
    const current = String(currentValue ?? "");
    const defaultOption = String(defaultValue ?? "");
    if (values.includes(current)) return current;
    if (values.includes(defaultOption)) return defaultOption;
    return String(values[0] || "");
  }

  function listVisibleOptions(capability, optionName, currentParameters) {
    const options = capability && capability.options && capability.options[optionName];
    if (!Array.isArray(options)) return [];
    return options.filter(option => (
      (capability.compatibility_map || []).some(mapping => {
        if (!isSelectableStatus(mapping.validation_status)) return false;
        const parameters = mapping.parameters || {};
        if (String(parameters[optionName] ?? "") !== String(option.value ?? "")) return false;
        return Object.keys(currentParameters || {}).every(key => (
          key === optionName
          || currentParameters[key] === undefined
          || currentParameters[key] === null
          || currentParameters[key] === ""
          || String(parameters[key] ?? "") === String(currentParameters[key])
        ));
      })
    ));
  }

  function getCapabilityUsageMeta(capability) {
    const entry = capability || {};
    const generationMode = String(entry.generation_mode || "");
    const generationModes = Array.isArray(entry.generation_modes)
      ? entry.generation_modes
      : [];
    const selectedMode = generationModes.find(item => (
      String(item && item.id || "") === generationMode
    )) || generationModes[0] || {};
    const minImages = Math.max(0, Number(entry.min_images || 0));
    const maxImages = Math.max(minImages, Number(entry.max_images || 0));
    return {
      generationMode: generationMode || String(selectedMode.id || ""),
      generationModeLabel: String(selectedMode.label || generationMode || ""),
      imageSemantics: String(entry.image_semantics || ""),
      usageGuide: String(entry.usage_guide || entry.description || ""),
      minImages,
      maxImages,
      requiresImages: minImages > 0,
      nativeResolutionNote: "原生清晰度（外部客户端请选择 720P）",
    };
  }

  function validateCapabilityImageCount(capability, imageCount) {
    const usage = getCapabilityUsageMeta(capability);
    const count = Math.max(0, Number(imageCount || 0));
    if (count < usage.minImages) {
      const message = usage.minImages === usage.maxImages
        ? `需要上传 ${usage.minImages} 张图片后才能生成`
        : `需要至少上传 ${usage.minImages} 张图片后才能生成`;
      return { valid: false, message };
    }
    if (count > usage.maxImages) {
      return {
        valid: false,
        message: `当前能力最多接受 ${usage.maxImages} 张图片`,
      };
    }
    return { valid: true, message: "图片数量符合当前能力要求" };
  }

  function listHiddenDiagnosticMappings(catalog) {
    const diagnostics = [];
    (Array.isArray(catalog) ? catalog : []).forEach(capability => {
      if (!capability || !String(capability.capability_id || "").trim()) return;
      (capability.compatibility_map || []).forEach(mapping => {
        const modelId = String(mapping && mapping.model_id || "").trim();
        if (!modelId || mapping.validation_status !== "hidden") return;
        const parameters = { ...(mapping.parameters || {}) };
        const labelParts = [String(capability.display_name || "").trim()];
        if (parameters.aspect_ratio) {
          labelParts.push(String(parameters.aspect_ratio));
        }
        if (parameters.duration_seconds) {
          labelParts.push(`${parameters.duration_seconds} 秒`);
        } else if (parameters.resolution) {
          labelParts.push(String(parameters.resolution));
        }
        diagnostics.push({
          capabilityId: String(capability.capability_id),
          modelType: String(capability.model_type || ""),
          modelId,
          parameters,
          label: labelParts.filter(Boolean).join(" · "),
        });
      });
    });
    return diagnostics;
  }

  function resolveExtendModelId(capability, aspectRatio) {
    if (!capability) return "";
    const action = (capability.actions || []).find(candidate => (
      candidate.id === "extend" && isSelectableStatus(candidate.validation_status)
    ));
    if (!action || !action.model_map) return "";
    return String(action.model_map[String(aspectRatio || "")] || "");
  }

  return Object.freeze({
    isSelectableStatus,
    listVisibleCapabilities,
    listVisibleOptions,
    selectPreferredOptionValue,
    getCapabilityUsageMeta,
    validateCapabilityImageCount,
    listHiddenDiagnosticMappings,
    resolveCapabilityModelId,
    resolveExtendModelId,
  });
});
