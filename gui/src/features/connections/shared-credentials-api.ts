/**
 * Saved user entries use the existing Secret catalog/writer and exact grant IDs.
 * Inventory and grant observation stay separate. Missing times and authentication
 * evidence are not synthesized. Unknown writes are never retried automatically.
 * Custom Pi routes and temporary probes consume generated operator methods.
 * Native Auth remains on its existing independent port; entry deletion is absent.
 * Add rechecks the catalog before writing and refuses existing entry IDs across
 * providers. This read is not an atomic create-only backend guarantee.
 * Custom service creation uses the daemon's atomic createOnly admission.
 */

import {
  ClientError,
  accessRefusal,
  assertMethodAvailable,
  methodRefusal,
  type MethodAccess,
  type MethodRefusal,
} from "../../client";
import type { GeneratedClient } from "../../client/generated";
import type {
  AgentSecretListResult,
  AgentSecretProviderTestResult,
} from "../../client/generated/types";
import type { NativeAuthApi } from "../agent-provider";
import {
  SECRET_PROVIDER_ENVIRONMENTS,
  parseSecretEntryReference,
  secretEntryReference,
  secretProviderForEnvironments,
} from "./impl/secret-entries";

export interface SharedGrantRow {
  readonly grantId: string;
  readonly actor: string;
  readonly source: string;
  readonly entryId: string;
  readonly fieldName: string | null;
  readonly environments: readonly string[];
  readonly ownerActor: string | null;
  readonly revision: number;
}

export interface CredentialGrant {
  readonly agent: string;
  readonly agentId: string | null;
  readonly grantId: string;
  readonly provider: string;
  readonly state: string;
}

export type CredentialProtocol = "openai-completions" | "anthropic-messages";

export interface ConnectionConfiguration {
  readonly version: 1;
  readonly endpoint: string;
  readonly protocol: "openai-completions";
  readonly profile: "openai-chat-completions/v1";
  readonly preset: { readonly id: string; readonly version: number };
}
export interface ConnectionMetadata extends ConnectionConfiguration {
  readonly connectionRef: string;
  readonly configRevision: number;
}
export interface ConnectionPreset {
  readonly label: string;
  readonly connection: ConnectionConfiguration;
}
export type CredentialTestRequest = {
  readonly model: string;
} & (
  | {
      readonly provider: string;
      readonly secret: string;
      readonly endpoint?: string;
      readonly protocol?: CredentialProtocol;
    }
  | {
      readonly connection: {
        readonly entryId: string;
        readonly fieldName: string;
        readonly connectionRef: string;
        readonly configRevision: number;
      };
    }
);

export function validModelId(model: string): boolean {
  return (
    model.length > 0 &&
    model.length <= 256 &&
    model === model.trim() &&
    !/[\u0000-\u001f\u007f]/.test(model)
  );
}
export function validConnectionKey(value: string): boolean {
  return /^[\x21-\x7e]{1,16384}$/.test(value);
}
export function validConnection(value: ConnectionConfiguration): boolean {
  return (
    value?.version === 1 &&
    validEndpoint(value.endpoint) &&
    value.protocol === "openai-completions" &&
    value.profile === "openai-chat-completions/v1" &&
    /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.preset?.id) &&
    Number.isSafeInteger(value.preset?.version) &&
    value.preset.version >= 1
  );
}
function validMetadata(value: ConnectionMetadata): boolean {
  return (
    validConnection(value) &&
    /^[a-f0-9]{32}$/.test(value.connectionRef) &&
    Number.isSafeInteger(value.configRevision) &&
    value.configRevision >= 1
  );
}
function connectionConfiguration(
  value: ConnectionConfiguration,
): ConnectionConfiguration {
  return {
    version: 1,
    endpoint: value.endpoint,
    protocol: value.protocol,
    profile: value.profile,
    preset: value.preset,
  };
}
export function connectionLabel(entry: CredentialEntry): string {
  return entry.connection
    ? entry.connectionPreset
      ? `${entry.connectionPreset} 官方地址`
      : "自定义连接"
    : entry.provider || "旧 Key";
}

export interface CredentialEntry {
  readonly connection?: ConnectionMetadata;
  readonly connectionPreset?: string;
  readonly keyId: string;
  readonly provider: string;
  readonly endpoint?: string;
  readonly protocol?: CredentialProtocol;
  readonly label: string;
  readonly state: string;
  readonly createdAt: number | null;
  readonly grantsKnown?: boolean;
  readonly lastUsedAt: number | null;
  readonly grants: readonly CredentialGrant[];
}

export type SharedCredentialWrite =
  | { readonly status: "saved"; readonly message?: string }
  | { readonly status: "failed"; readonly message: string }
  | { readonly status: "unknown"; readonly message: string }
  | { readonly status: "unavailable"; readonly refusal: MethodRefusal };

export type SharedCredentialAction = "add" | "replace" | "delete" | "revoke";

const ACTION_METHODS: Readonly<
  Partial<Record<SharedCredentialAction, string>>
> = {
  add: "agent.secret.provider-configure",
  replace: "agent.secret.provider-write",
  revoke: "agent.secret.revoke",
};

export interface CredentialProbe {
  readonly kind: "model" | "blocked";
  readonly status: number | null;
  readonly ok: boolean | null;
  readonly latencyMs: number;
  readonly simulated?: boolean;
  readonly outcome?: AgentSecretProviderTestResult["outcome"];
}

export interface ConfiguredKeyInput {
  readonly label: string;
  readonly value: string;
  readonly connection: ConnectionConfiguration;
}

export function validEndpoint(endpoint: string): boolean {
  try {
    const url = new URL(endpoint);
    return (
      endpoint.length <= 2048 &&
      !/[\s\\"<>]/.test(endpoint) &&
      !url.username &&
      !url.password &&
      !endpoint.includes("?") &&
      !endpoint.includes("#") &&
      (url.protocol === "https:" ||
        (url.protocol === "http:" &&
          ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))
    );
  } catch {
    return false;
  }
}

export interface SharedCredentialsApi {
  readonly nativeAuth?: NativeAuthApi;
  listKeys(): Promise<readonly CredentialEntry[]>;
  listSecretGrants(): Promise<readonly SharedGrantRow[]>;
  addKey(
    provider: string,
    label: string,
    value: string,
  ): Promise<SharedCredentialWrite>;
  configureKey?(input: ConfiguredKeyInput): Promise<SharedCredentialWrite>;
  listConnectionPresets?(): Promise<readonly ConnectionPreset[]>;
  replaceConnection?(
    entry: CredentialEntry,
    value: string,
  ): Promise<SharedCredentialWrite>;
  testKey?(
    request: CredentialTestRequest,
    options?: { readonly signal?: AbortSignal },
  ): Promise<CredentialProbe>;
  readonly testProviders?: readonly string[];
  refusal?(
    action: SharedCredentialAction,
    entry?: CredentialEntry,
  ): MethodRefusal | null;
  replaceKey(keyId: string, value: string): Promise<SharedCredentialWrite>;
  deleteKey?(keyId: string): Promise<SharedCredentialWrite>;
  revokeGrant(agent: string, grantId: string): Promise<SharedCredentialWrite>;
}

function writeOutcome(error: unknown): SharedCredentialWrite {
  const refusal = methodRefusal(error);
  if (refusal) return { status: "unavailable", refusal };
  if (error instanceof ClientError && error.dispatch === "not-dispatched") {
    return { status: "failed", message: "本次操作未派发，未写入任何凭据。" };
  }
  return {
    status: "unknown",
    message: "操作结果未知，已暂停重复提交。请刷新凭据状态后再试。",
  };
}

function validKey(value: string): boolean {
  return value.length > 0 && !/[\r\n]/.test(value);
}

export function createNativeAuthApiFromClient(
  client: Pick<GeneratedClient, "agent">,
  access?: MethodAccess,
): NativeAuthApi {
  return {
    async list(actor) {
      const params = actor === undefined ? {} : { actor };
      assertMethodAvailable(access, "agent.provider.auth.list", params);
      const result = await client.agent.provider.auth.list(params);
      return result.records;
    },
  };
}

export function createSharedCredentialsApiFromClient(
  client: Pick<GeneratedClient, "agent">,
  access?: MethodAccess,
): SharedCredentialsApi {
  const nativeAuth = createNativeAuthApiFromClient(client, access);
  async function saveConnection(
    entryId: string,
    fieldName: string,
    value: string,
    connection: ConnectionConfiguration,
    expectedRevision: number,
    reference?: string,
  ): Promise<SharedCredentialWrite> {
    if (
      !validConnection(connection) ||
      !validConnectionKey(value) ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(entryId)
    ) {
      return {
        status: "failed",
        message: "请检查名称、Key、服务地址和兼容协议。",
      };
    }
    const params = {
      entryId,
      fieldName,
      value,
      connection: connectionConfiguration(connection),
      expectedRevision,
    };
    const refusal = accessRefusal(
      access,
      "agent.secret.provider-configure",
      params,
    );
    if (refusal) return { status: "unavailable", refusal };
    try {
      const result = await client.agent.secret.providerConfigure(params);
      const receipt = result.connections?.[fieldName];
      if (
        result.entryId !== entryId ||
        !result.fieldNames.includes(fieldName) ||
        !receipt ||
        !validMetadata(receipt) ||
        receipt.configRevision !== expectedRevision + 1 ||
        (reference !== undefined && receipt.connectionRef !== reference) ||
        receipt.endpoint !== connection.endpoint ||
        receipt.protocol !== connection.protocol ||
        receipt.profile !== connection.profile ||
        receipt.preset.id !== connection.preset.id ||
        receipt.preset.version !== connection.preset.version
      ) {
        return {
          status: "unknown",
          message: "连接回执与目标不一致，结果未知；请刷新清单核对。",
        };
      }
      return {
        status: "saved",
        message:
          expectedRevision === 0
            ? "连接和 Key 已保存；尚未验证模型，也未启动 Agent。"
            : "Key 已更换，连接版本已更新。请重新配置使用此连接的 Agent 后再启动。",
      };
    } catch (error) {
      if (
        error instanceof ClientError &&
        [
          "INVALID_ARGUMENT",
          "MODEL_CONFIGURATION_INVALID",
          "MODEL_CONFIGURATION_CONFLICT",
          "MODEL_AUTH_UNAVAILABLE",
        ].includes(error.code)
      ) {
        return {
          status: "failed",
          message:
            error.code === "MODEL_CONFIGURATION_CONFLICT"
              ? "连接已变更或名称已存在，未写入；请刷新后重试。"
              : "配置校验或凭据读取失败，未写入；请核对后重试。",
        };
      }
      return writeOutcome(error);
    }
  }
  const configure =
    typeof client.agent?.secret?.providerConfigure === "function"
      ? {
          configureKey(input: ConfiguredKeyInput) {
            return saveConnection(
              input.label.trim(),
              "key",
              input.value,
              input.connection,
              0,
            );
          },
          replaceConnection(
            entry: CredentialEntry,
            value: string,
          ): Promise<SharedCredentialWrite> {
            const target = parseSecretEntryReference(entry.keyId);
            if (
              !target ||
              !entry.connection ||
              !validMetadata(entry.connection)
            )
              return Promise.resolve({
                status: "failed",
                message: "连接标识未通过校验，请刷新清单。",
              });
            return saveConnection(
              target.entryId,
              target.fieldName,
              value,
              entry.connection,
              entry.connection.configRevision,
              entry.connection.connectionRef,
            );
          },
          async listConnectionPresets(): Promise<readonly ConnectionPreset[]> {
            const params = { connectionVersion: 1 as const };
            assertMethodAvailable(access, "agent.secret.provider-list", params);
            const result = await client.agent.secret.providerList(params);
            const presets = result.presets ?? [];
            if (
              presets.some(
                (item) =>
                  !item.label ||
                  !item.connection.preset ||
                  !validConnection(item.connection as ConnectionConfiguration),
              ) ||
              new Set(presets.map((item) => item.connection.preset?.id))
                .size !== presets.length
            )
              throw new Error("连接预设无法识别。");
            return presets as readonly ConnectionPreset[];
          },
        }
      : {};
  const test =
    typeof client.agent?.secret?.providerTest === "function"
      ? {
          testProviders: [
            "custom",
            "connection",
            "deepseek",
            "openai",
            "anthropic",
            "hyprial",
          ],
          async testKey(
            request: CredentialTestRequest,
            options?: { readonly signal?: AbortSignal },
          ): Promise<CredentialProbe> {
            if (!validModelId(request.model))
              throw new Error(
                "模型 ID 必须手填，且不能包含首尾空白或控制字符。",
              );
            const params = { ...request, timeoutSeconds: 30 };
            assertMethodAvailable(access, "agent.secret.provider-test", params);
            const result = await client.agent.secret.providerTest(params, {
              signal: options?.signal,
              budgetMs: 35000,
            });
            return {
              kind: "model",
              ok: result.outcome === "passed",
              outcome: result.outcome,
              status: result.httpStatus,
              latencyMs: result.latencyMs,
            };
          },
        }
      : {};
  return {
    ...configure,
    ...test,
    ...(accessRefusal(access, "agent.provider.auth.list") === null
      ? { nativeAuth }
      : {}),
    async listKeys() {
      assertMethodAvailable(access, "agent.secret.provider-list", {
        connectionVersion: 1,
      });
      const result = await client.agent.secret.providerList({
        connectionVersion: 1,
      });
      return result.entries.flatMap((entry) =>
        entry.fieldNames.map((fieldName) => {
          const route = entry.routes?.[fieldName];
          const connection = entry.connections?.[fieldName];
          if (
            connection !== undefined &&
            (!validMetadata(connection) || route !== undefined)
          )
            throw new Error("连接元数据无法识别，请刷新核对。");
          if (
            route !== undefined &&
            (!validEndpoint(route.endpoint) ||
              route.protocol !== "openai-completions" ||
              route.vendorId !== "deepseek")
          ) {
            throw new Error("服务配置无法识别，请刷新后核对。");
          }
          const preset =
            connection &&
            result.presets?.find(
              (item) =>
                item.connection.endpoint === connection.endpoint &&
                item.connection.protocol === connection.protocol &&
                item.connection.profile === connection.profile &&
                item.connection.preset?.id === connection.preset.id &&
                item.connection.preset.version === connection.preset.version,
            );
          return {
            ...(preset ? { connectionPreset: preset.label } : {}),
            keyId: secretEntryReference(entry.entryId, fieldName),
            provider: connection
              ? "connection"
              : (route?.vendorId ?? secretProviderForEnvironments([fieldName])),
            ...(connection
              ? {
                  connection,
                  endpoint: connection.endpoint,
                  protocol: connection.protocol,
                }
              : {}),
            ...(route
              ? { endpoint: route.endpoint, protocol: route.protocol }
              : {}),
            label:
              entry.fieldNames.length > 1
                ? `${entry.entryId} / ${fieldName}`
                : entry.entryId,
            state: "active",
            createdAt: null,
            lastUsedAt: null,
            grants: [],
            grantsKnown: false,
          };
        }),
      );
    },

    async listSecretGrants() {
      assertMethodAvailable(access, "agent.secret.list", {});
      const grantResult = await client.agent.secret.list({});
      return (grantResult as AgentSecretListResult).grants.map((grant) => ({
        grantId: grant.grantId,
        actor: grant.actor,
        source: grant.source,
        entryId: grant.entryId,
        fieldName: grant.fieldName,
        environments: grant.environmentNames,
        ownerActor: grant.ownerActor,
        revision: grant.revision,
      }));
    },

    refusal(action, entry) {
      const method =
        action === "replace" && entry?.connection
          ? "agent.secret.provider-configure"
          : ACTION_METHODS[action];
      return method ? accessRefusal(access, method) : null;
    },

    async addKey(provider, label, value) {
      const entryId = label.trim();
      const fieldName = SECRET_PROVIDER_ENVIRONMENTS[provider];
      if (!fieldName)
        return { status: "failed", message: "请选择已支持的模型服务商。" };
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(entryId)) {
        return {
          status: "failed",
          message:
            "名称以字母或数字开头，仅支持字母、数字、点、下划线和短横线，最多 128 字符。",
        };
      }
      if (!validKey(value))
        return { status: "failed", message: "请输入有效的 Key。" };
      const refusal = accessRefusal(access, "agent.secret.provider-write", {
        entryId,
        fieldName,
      });
      if (refusal) return { status: "unavailable", refusal };
      try {
        assertMethodAvailable(access, "agent.secret.provider-list", {});
        const catalog = await client.agent.secret.providerList({});
        if (catalog.entries.some((entry) => entry.entryId === entryId)) {
          return {
            status: "failed",
            message:
              "该名称已存在，未写入 Key。请使用新名称；如需更新，请在原条目中选择“更换 Key”。",
          };
        }
      } catch (error) {
        const refusal = methodRefusal(error);
        return refusal
          ? { status: "unavailable", refusal }
          : {
              status: "failed",
              message: "无法核对已有名称，未写入 Key。请刷新清单后重试。",
            };
      }
      return this.replaceKey(secretEntryReference(entryId, fieldName), value);
    },

    async replaceKey(keyId, value) {
      const target = parseSecretEntryReference(keyId);
      if (!target)
        return {
          status: "failed",
          message: "缺少有效的凭据标识，请刷新清单。",
        };
      if (!validKey(value))
        return { status: "failed", message: "请输入有效的 Key。" };
      const refusal = accessRefusal(
        access,
        "agent.secret.provider-write",
        target,
      );
      if (refusal) return { status: "unavailable", refusal };
      try {
        const result = await client.agent.secret.providerWrite({
          ...target,
          value,
        });
        if (
          result.entryId !== target.entryId ||
          !result.fieldNames.includes(target.fieldName)
        ) {
          return {
            status: "unknown",
            message: "写入回执与目标不一致，结果未知；已暂停重复提交。",
          };
        }
        return {
          status: "saved",
          message: "Key 已保存；已运行的 Agent 需重启后使用。",
        };
      } catch (error) {
        return writeOutcome(error);
      }
    },

    async revokeGrant(actor, grantId) {
      if (!actor.trim() || !grantId.trim())
        return { status: "failed", message: "缺少 Agent 或授权标识。" };
      const params = { actor, grantId };
      const refusal = accessRefusal(access, "agent.secret.revoke", params);
      if (refusal) return { status: "unavailable", refusal };
      try {
        const result = await client.agent.secret.revoke(params);
        if (!result.revoked)
          return {
            status: "failed",
            message: "未找到该授权，请刷新清单后核对。",
          };
        return {
          status: "saved",
          message: "授权已撤销；已运行的 Agent 需停止后才能清除已加载的 Key。",
        };
      } catch (error) {
        return writeOutcome(error);
      }
    },
  };
}
