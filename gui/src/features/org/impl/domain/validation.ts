/**
 * 组织名就地校验（B1）：只用小写字母、数字和连字符，最长 57 个字符，
 * 不能以 `-acl` 结尾。新建与改名共用同一规则。
 */
export const ORG_NAME_MAX = 57;

/** 组织 id 的固定前缀；用户输入的名称不需要带它。 */
export const ORG_ID_PREFIX = 'group-';

/**
 * 把用户输入的组织名转成 daemon 的组织 id：先剥一次 `group-` 再补回，
 * 与 CLI 的 `org create` 同一规则（`foo` 与 `group-foo` 是同一个组织）。
 * 只做这一件事；名称语法仍由 `validateOrgName` 与 daemon 校验。
 */
export function orgIdFromName(raw: string): string {
  const name = raw.trim();
  const remainder = name.startsWith(ORG_ID_PREFIX)
    ? name.slice(ORG_ID_PREFIX.length)
    : name;
  return `${ORG_ID_PREFIX}${remainder}`;
}

export function validateOrgName(raw: string): string | null {
  const name = raw.trim();
  if (!name) {
    return '请填写组织名称';
  }
  if (!/^[a-z0-9-]+$/.test(name)) {
    return '只能使用小写字母、数字和连字符';
  }
  if (name.length > ORG_NAME_MAX) {
    return `最长 ${ORG_NAME_MAX} 个字符`;
  }
  if (name.endsWith('-acl')) {
    return '不能以 -acl 结尾';
  }
  return null;
}

export function validateAccount(raw: string): string | null {
  return raw.trim() ? null : '请填写成员账号';
}

export function validateInviteLink(raw: string): string | null {
  return raw.trim() ? null : '请粘贴邀请链接';
}
