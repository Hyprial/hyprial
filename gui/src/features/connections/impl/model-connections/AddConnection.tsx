/** Shared connection creation uses daemon presets and the existing atomic Secret authority. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Dialog,
  Field,
  Notice,
  Select,
} from "../../../../components/primitives";
import { SecretField } from "../../../../components/forms";
import { useNotifyUnavailable } from "../../../../components/notifications";
import {
  validConnectionKey,
  validEndpoint,
  type ConnectionConfiguration,
  type ConnectionPreset,
  type SharedCredentialsApi,
  type SharedCredentialWrite,
} from "../../shared-credentials-api";
import { ConnectionProbe } from "./Probe";

export function AddConnection({
  api,
  disabled,
  onClose,
  onOutcome,
}: {
  readonly api: SharedCredentialsApi;
  readonly disabled: boolean;
  readonly onClose: () => void;
  readonly onOutcome: (outcome: SharedCredentialWrite) => Promise<void>;
}) {
  const [presets, setPresets] = useState<readonly ConnectionPreset[]>([]);
  const [presetState, setPresetState] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [selected, setSelected] = useState("custom");
  const [label, setLabel] = useState("");
  const [value, setValue] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState("");
  const notify = useNotifyUnavailable();
  const active = useRef(0);
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => {
    active.current += 1;
    return () => {
      active.current += 1;
    };
  }, [api]);
  useEffect(() => {
    let current = true;
    if (!api.listConnectionPresets) {
      setPresetState("error");
      return;
    }
    void api
      .listConnectionPresets()
      .then((rows) => {
        if (current) {
          setPresets(rows);
          setPresetState("ready");
        }
      })
      .catch(() => {
        if (current) setPresetState("error");
      });
    return () => {
      current = false;
    };
  }, [api]);
  const preset = presets.find((item) => item.connection.preset.id === selected);
  const connection: ConnectionConfiguration = preset?.connection ?? {
    version: 1,
    endpoint,
    protocol: "openai-completions",
    profile: "openai-chat-completions/v1",
    preset: { id: "custom", version: 1 },
  };
  const nameValid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(label);
  const writable =
    !disabled && !saving && !testing && Boolean(api.configureKey);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (
      !writable ||
      !nameValid ||
      !validEndpoint(connection.endpoint) ||
      !validConnectionKey(value) ||
      !api.configureKey
    )
      return;
    const epoch = active.current;
    setSaving(true);
    setFailure("");
    try {
      const outcome = await api.configureKey({ label, value, connection });
      if (active.current !== epoch) return;
      if (outcome.status === "unavailable") {
        notify("保存连接", outcome.refusal);
        return;
      }
      setValue("");
      if (outcome.status === "failed") setFailure(outcome.message);
      await onOutcome(outcome);
    } catch {
      if (active.current !== epoch) return;
      setValue("");
      await onOutcome({
        status: "unknown",
        message: "保存结果未知，请刷新清单核对后再操作。",
      });
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog
      open
      title="添加连接"
      className="credentials-connection-dialog"
      onClose={() => {
        if (!saving) onClose();
      }}
      actions={[
        {
          label: "取消",
          variant: "secondary",
          disabled: saving,
          onClick: onClose,
        },
        {
          label: "保存",
          variant: "primary",
          loading: saving,
          disabled:
            !writable ||
            !nameValid ||
            !validConnectionKey(value) ||
            !validEndpoint(connection.endpoint),
          onClick: () => form.current?.requestSubmit(),
        },
      ]}
    >
      <form
        ref={form}
        className="credentials-form credentials-add"
        onSubmit={(event) => void save(event)}
      >
        <p className="credentials-row__meta">
          保存服务地址和 Key；模型 ID 在测试或配置 Agent 时手填。
        </p>
        {failure && (
          <Notice tone="danger" title="未保存">
            {failure}
          </Notice>
        )}
        <Field label="服务地址来源" htmlFor="connection-preset">
          <Select
            id="connection-preset"
            value={selected}
            disabled={saving || testing || disabled}
            onValueChange={setSelected}
            options={[
              { value: "custom", label: "自定义地址" },
              ...presets.map((item) => ({
                value: item.connection.preset.id,
                label: `${item.label} 官方地址`,
              })),
            ]}
          />
        </Field>
        {presetState === "loading" && (
          <p role="status">正在读取官方地址预设…</p>
        )}
        {presetState === "error" && (
          <p role="status">官方地址预设暂不可用，可填写自定义地址。</p>
        )}
        <Field
          label="服务地址"
          htmlFor="connection-endpoint"
          hint="完整 Base URL；HTTPS 或本机 HTTP。"
          error={
            connection.endpoint && !validEndpoint(connection.endpoint)
              ? "地址无效；请去掉空白、查询参数和账号信息。"
              : undefined
          }
        >
          <input
            id="connection-endpoint"
            value={connection.endpoint}
            readOnly={Boolean(preset)}
            autoComplete="off"
            disabled={saving || testing || disabled}
            onChange={(event) => setEndpoint(event.target.value)}
          />
        </Field>
        <div className="credentials-row__meta" aria-label="兼容协议">
          OpenAI Chat Completions · 当前支持的协议
        </div>
        <Field
          label="名称"
          htmlFor="connection-name"
          hint="字母或数字开头；可含 . _ -，最多 128 字符。"
          error={label && !nameValid ? "名称不符合格式要求。" : undefined}
        >
          <input
            id="connection-name"
            value={label}
            autoComplete="off"
            disabled={saving || testing || disabled}
            onChange={(event) => setLabel(event.target.value)}
          />
        </Field>
        <SecretField
          id="connection-key"
          label="Key"
          layout="inline"
          value={value}
          onChange={setValue}
          disabled={saving || testing || disabled}
        />
        {value && !validConnectionKey(value) && (
          <p role="alert">Key 不能包含空白或非 ASCII 字符。</p>
        )}
        <ConnectionProbe
          api={api}
          identity={`${connection.endpoint}\u0000${value}`}
          disabled={
            saving ||
            disabled ||
            !validConnectionKey(value) ||
            !validEndpoint(connection.endpoint)
          }
          onTestingChange={setTesting}
          request={(model) => ({
            provider: "custom",
            secret: value,
            model,
            endpoint: connection.endpoint,
            protocol: connection.protocol,
          })}
        />
        {!api.configureKey && (
          <Notice tone="warning" title="保存不可用">
            当前服务未提供连接配置能力。
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
