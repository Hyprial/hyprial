/** Exact single-request probes; cancelling invalidates the result and aborts transport. */
import { useEffect, useRef, useState } from "react";
import { Button, Field } from "../../../../components/primitives";
import { ClientError, methodRefusal } from "../../../../client";
import { useNotifyUnavailable } from "../../../../components/notifications";
import {
  validModelId,
  type CredentialProbe,
  type CredentialTestRequest,
  type SharedCredentialsApi,
} from "../../shared-credentials-api";

export function probeMessage(result: CredentialProbe): string {
  const outcomes = {
    passed: "本次模型测试通过；未验证 Agent 启动。",
    "authentication-rejected": "Key 被模型服务拒绝，请核对 Key。",
    "rate-limited": "模型服务限流，请稍后手动重试。",
    "redirect-refused": "服务地址发生重定向，请核对地址。",
    "request-rejected": "模型服务拒绝请求，请核对模型 ID 与协议。",
    "upstream-error": "模型服务暂时出错。",
    "invalid-response": "未收到有效模型回复，请核对协议。",
    timeout: "30 秒内未收到模型回复，本次测试未完成。",
    unreachable: "无法连接模型服务。",
  };
  const message = result.outcome
    ? outcomes[result.outcome]
    : result.ok
      ? outcomes.passed
      : "测试未通过。";
  return `${message}${result.simulated ? "（模拟）" : ` · ${result.latencyMs} ms`}`;
}
export function probeError(error: unknown): string {
  if (error instanceof ClientError) {
    if (error.code === "MODEL_CONFIGURATION_CONFLICT")
      return "连接已变更，请刷新清单后重新测试。";
    if (error.code === "MODEL_AUTH_UNAVAILABLE")
      return "已保存的凭据不可用，请刷新并核对授权。";
    if (error.code === "INVALID_ARGUMENT")
      return "配置无效，请核对服务地址、Key 和模型 ID。";
  }
  return "测试未完成，请核对连接后手动重试。";
}
export function ConnectionProbe({
  api,
  identity,
  request,
  disabled = false,
  onTestingChange,
  id = "connection-model",
}: {
  readonly api: SharedCredentialsApi;
  readonly identity: string;
  readonly request: (model: string) => CredentialTestRequest;
  readonly disabled?: boolean;
  readonly onTestingChange?: (testing: boolean) => void;
  readonly id?: string;
}) {
  const [model, setModel] = useState("");
  const notify = useNotifyUnavailable();
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef<AbortController | null>(null);
  const changed = useRef(onTestingChange);
  changed.current = onTestingChange;
  useEffect(() => {
    active.current?.abort();
    active.current = null;
    setTesting(false);
    setMessage("");
    changed.current?.(false);
    return () => {
      active.current?.abort();
      active.current = null;
      changed.current?.(false);
    };
  }, [api, identity]);
  function cancel() {
    active.current?.abort();
    active.current = null;
    setTesting(false);
    changed.current?.(false);
    setMessage("本次测试已取消。");
  }
  async function test() {
    if (!api.testKey || disabled || !validModelId(model) || active.current)
      return;
    const controller = new AbortController();
    active.current = controller;
    setTesting(true);
    setMessage("");
    changed.current?.(true);
    try {
      const result = await api.testKey(request(model), {
        signal: controller.signal,
      });
      if (active.current === controller) setMessage(probeMessage(result));
    } catch (error) {
      if (active.current === controller) {
        const refusal = methodRefusal(error);
        if (refusal) notify("测试连接", refusal);
        else setMessage(probeError(error));
      }
    } finally {
      if (active.current === controller) {
        active.current = null;
        setTesting(false);
        changed.current?.(false);
      }
    }
  }
  if (!api.testKey)
    return (
      <p className="credentials-row__meta">
        当前连接测试不可用，保存不代表模型可调用。
      </p>
    );
  return (
    <div className="credentials-probe">
      <Field
        label="测试模型 ID"
        htmlFor={id}
        hint="手填服务实际接受的完整 ID。只用于本次测试，不保存为 Agent 配置。"
        error={
          model && !validModelId(model)
            ? "模型 ID 不能含首尾空白或控制字符，最多 256 字符。"
            : undefined
        }
      >
        <input
          id={id}
          value={model}
          autoComplete="off"
          disabled={testing}
          onChange={(event) => {
            setModel(event.target.value);
            setMessage("");
          }}
        />
      </Field>
      {message && (
        <p className="credentials-test-result" role="status">
          {message}
        </p>
      )}
      <div className="credentials-form-actions">
        {testing ? (
          <Button type="button" variant="secondary" onClick={cancel}>
            取消测试
          </Button>
        ) : (
          <Button
            type="button"
            variant="secondary"
            disabled={disabled || !validModelId(model)}
            onClick={() => void test()}
          >
            测试
          </Button>
        )}
      </div>
    </div>
  );
}
