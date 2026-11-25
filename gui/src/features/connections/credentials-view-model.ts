/**
 * Agent 设置「凭据设置」的显示名称和开发数据形状。
 * Key 保存沿用 agent.secret.provider-write；服务商选择不等于配置 harness 或模型。
 * 自定义服务使用 provider-configure 一起保存地址和 Key；首批仅支持 Pi/DeepSeek。
 * 测试由注入的数据端口提供；浏览器不直接携带 Key 请求模型服务。
 */
import type { CredentialEntry, SharedGrantRow } from './shared-credentials-api';

export interface CredentialsSectionModel {
  readonly entries: readonly CredentialEntry[];
  readonly authGrants: readonly SharedGrantRow[];
}

export interface CredentialProviderPreset {
  readonly id: string;
  readonly label: string;
}

export const CREDENTIAL_PROVIDERS: readonly CredentialProviderPreset[] = [
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'google', label: 'Google' },
  { id: 'hyprial', label: 'Hyprial' },
  { id: 'kimi-coding', label: 'Kimi Code' },
  { id: 'moonshotai', label: 'Moonshot AI' },
] as const;

export const CREDENTIAL_PROTOCOLS = [
  { value: 'openai-completions', label: 'OpenAI Chat Completions' },
  { value: 'anthropic-messages', label: 'Anthropic Messages' },
] as const;

export const CREDENTIAL_PROVIDER_ENVIRONMENTS: Readonly<Record<string, readonly string[]>> = {
  google: ['GEMINI_API_KEY'],
  'kimi-coding': ['KIMI_API_KEY'],
  moonshotai: ['MOONSHOT_API_KEY'],
};

export const CREDENTIALS_SECTION_COPY = {
  title: '凭据设置',
  add: '添加 Key',
  provider: '模型服务商',
  name: '名称',
  nameHint: '名称以字母或数字开头，支持点、下划线和短横线，最多 128 字符。',
  keyValue: 'Key',
  replaceKey: '更换 Key',
  replaceTitle: '更换 Key',
  replaceDescription: '仅更换 Key；名称、服务商、服务地址和已授权 Agent 保持不变。',
  replaceHint: '已运行的 Agent 需要重启后才会使用新 Key。',
  save: '保存',
  cancel: '取消',
  empty: '暂无 Key',
  refresh: '刷新',
  detail: '查看详情',
  keyBadge: 'Key',
  authBadge: '服务秘钥',
  revokeGrant: '撤销授权',
  deleteCredential: '删除凭据',
  deleteConfirmTitle: '删除凭据',
  deleteConfirmDescription: '删除后该 Key 及其全部 Agent 授权都会撤销，且不可恢复。',
  deleteConfirm: '确认删除',
  authManaged: '服务秘钥授权记录（只读）。',
} as const;
