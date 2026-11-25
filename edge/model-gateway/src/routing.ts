// 路由与错误映射：入口两个兼容面 → 网关的 provider 模型 id。
// 设计依据：P0 实测——网关要求 <provider>/<model>；anthropic 路径必须带 anthropic-version。
export type Surface = "openai" | "anthropic";

export function surfaceForPath(path: string): Surface | null {
  if (path === "/v1/chat/completions" || path === "/v1/models") return "openai";
  if (path === "/anthropic/v1/messages") return "anthropic";
  return null;
}

/** 模型 id 归一：⛔ 必须形如 <provider>/<model>（P0：裸 id 会被网关拒）。 */
export function normalizeModel(model: unknown): string {
  const m = typeof model === "string" ? model : "";
  if (!m.includes("/")) throw new Error("MODEL_NOT_QUALIFIED");
  return m;
}

export type ErrCode =
  | "UNAUTHENTICATED"
  | "QUOTA_SERVICE_UNAVAILABLE"
  | "QUOTA_EXCEEDED"
  | "BAD_REQUEST"
  | "UPSTREAM_ERROR";

/** 统一的错误面：❗ 每一种都要可区分（裁定 fail-closed），并给真实出路。 */
export function errorResponse(code: ErrCode, detail: string): Response {
  const status =
    code === "UNAUTHENTICATED" ? 401 : code === "BAD_REQUEST" ? 400 : code === "QUOTA_EXCEEDED" ? 402 : 503;
  const hint =
    code === "QUOTA_EXCEEDED"
      ? "额度已用尽：请联系管理员在 Casdoor 调整套餐/额度。"
      : code === "QUOTA_SERVICE_UNAVAILABLE"
        ? "额度服务暂不可用，已按 fail-closed 拒绝本次请求；请稍后重试。"
        : code === "UNAUTHENTICATED"
          ? "请先运行 hyprial login。"
          : code === "BAD_REQUEST"
            ? "请求格式或模型 id 不合法（模型需形如 <provider>/<model>）。"
            : "上游错误，请重试；若持续请报障。";
  return new Response(JSON.stringify({ error: { code, message: detail, hint } }), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
