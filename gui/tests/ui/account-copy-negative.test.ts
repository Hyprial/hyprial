// ACCOUNT-011：负向探针——1440 隐藏或窄屏意外可见都必须失败，避免自证空跑。
import { describe, expect, it } from 'vitest';
import { assertAccountCopy } from '../support/account-copy-assertions.mjs';

const base = {
  display: 'flex', height: 18, text: '我的', small: 0,
  triggerWidth: 120, triggerHeight: 44, triggerLabel: '我的账号与 Agent 设置菜单',
  menuItems: ['用户设置', 'Agent 设置', '退出本机登录'],
};
const narrow = { ...base, display: 'none', height: 0, triggerWidth: 38, triggerHeight: 38 };

describe('account copy responsive contract', () => {
  it('passes for the owner-approved states', () => {
    expect(() => assertAccountCopy(base, 1440)).not.toThrow();
    expect(() => assertAccountCopy(narrow, 760)).not.toThrow();
    expect(() => assertAccountCopy(narrow, 375)).not.toThrow();
  });

  it('fails when 1440 copy is hidden', () => {
    expect(() => assertAccountCopy(narrow, 1440)).toThrow();
  });

  it('fails when a narrow viewport unexpectedly shows the copy', () => {
    expect(() => assertAccountCopy(base, 375)).toThrow();
  });

  it('fails when menu items are missing', () => {
    expect(() => assertAccountCopy({ ...base, menuItems: ['用户设置'] }, 1440)).toThrow();
  });
});
