// 用量与计价：把 provider 的 usage 三段（普通输入/缓存写/缓存读/输出）解析出来。
// 设计依据：design-...-casdoor §16（实测 anthropic 返回 cache_creation/cache_read；CF 日志 tokens_in 只算未命中）
export type Usage = {
  inputTokens: number;
  outputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
};

export function parseUsage(raw: unknown): Usage {
  const u = (raw ?? {}) as Record<string, unknown>;
  const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    inputTokens: n(u.input_tokens) || n(u.prompt_tokens), // anthropic / openai 两种形态
    outputTokens: n(u.output_tokens) || n(u.completion_tokens),
    cacheCreationTokens: n(u.cache_creation_input_tokens),
    // OpenAI 把缓存命中放在 prompt_tokens_details.cached_tokens
    cacheReadTokens:
      n(u.cache_read_input_tokens) ||
      n((u.prompt_tokens_details as Record<string, unknown> | undefined)?.cached_tokens),
  };
}

/** 三段费率的单价表（美元/百万 token）。⛔ 先只放已实测过的 anthropic；其余等 CF 目录读取补齐。 */
export type Rate = { input: number; output: number; cacheWrite: number; cacheRead: number };
export const RATES: Record<string, Rate> = {
  "anthropic/claude-sonnet-4-6": { input: 3, output: 15, cacheWrite: 3.75, cacheRead: 0.3 },
};

/** 自算口径（乙）；与 CF 日志 cost（甲）互校。费率缺省时返回 null，⛔ 不猜。 */
export function estimateCostUsd(model: string, u: Usage): number | null {
  const r = RATES[model];
  if (!r) return null;
  return (
    (u.inputTokens * r.input +
      u.outputTokens * r.output +
      u.cacheCreationTokens * r.cacheWrite +
      u.cacheReadTokens * r.cacheRead) /
    1_000_000
  );
}
