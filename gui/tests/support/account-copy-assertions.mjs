// ACCOUNT-011：owner 响应式意图——1440 显示「我的」，≤979（760/375）为 avatar-only（批准态）。
import assert from 'node:assert/strict';

export const OWNER_COPY = '我的';
export const OWNER_NARROW_BREAKPOINT = 979;

export function assertAccountCopy(sample, width) {
  if (width > OWNER_NARROW_BREAKPOINT) {
    assert.notEqual(sample.display, 'none', `1440 copy must be visible @${width}`);
    assert.ok(sample.height > 0, `1440 copy has height @${width}`);
    assert.equal(sample.text, OWNER_COPY, `1440 copy text @${width}`);
    assert.equal(sample.small, 0, `no subtitle @${width}`);
  } else {
    assert.equal(sample.display, 'none', `narrow copy hidden by owner intent @${width}`);
    assert.equal(sample.height, 0, `narrow copy has no box @${width}`);
    assert.ok(sample.triggerWidth >= 36 && sample.triggerHeight >= 36, `avatar trigger stays tappable @${width}`);
    assert.equal(sample.triggerLabel, '我的账号与 Agent 设置菜单', `accessible name kept @${width}`);
  }
  assert.equal(sample.menuItems.length, 3, `menu keeps three items @${width}`);
  for (const label of ['用户设置', 'Agent 设置', '退出本机登录']) {
    assert.ok(sample.menuItems.includes(label), `menu item ${label} @${width}`);
  }
}
