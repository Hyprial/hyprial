/**
 * 组织名就地校验（B1）：只用小写字母、数字和连字符，最长 57 个字符，
 * 不能以 `-acl` 结尾。新建与改名共用同一规则。
 */
export const ORG_NAME_MAX = 57;

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
