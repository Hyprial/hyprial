// BUTTON-009：production 失败页主按钮严格断言（矩阵与负向单测共用，无浏览器副作用）。
import assert from 'node:assert/strict';

const BLUE = /rgb\(0, 72, 168\)|rgb\(17, 89, 184\)/;

export function assertProductionPrimary(buttons, width) {
  assert.equal(buttons.length, 1, `production primary count @${width}`);
  const [button] = buttons;
  assert.equal(button.tag, 'BUTTON', `production primary tag @${width}`);
  assert.equal(button.slot, 'button', `production primary data-slot @${width}`);
  assert.ok(button.className.includes('ui-button--primary'), `production primary class @${width}`);
  assert.equal(button.radius, '999px', `production primary radius @${width}`);
  assert.equal(button.background, 'rgb(26, 22, 32)', `production primary normal bg @${width}`);
  assert.equal(button.color, 'rgb(255, 255, 255)', `production primary normal text @${width}`);
  assert.equal(button.border, 'rgb(26, 22, 32)', `production primary normal border @${width}`);
  for (const value of [button.background, button.color, button.border]) {
    assert.ok(!BLUE.test(value), `production primary no blue @${width}: ${value}`);
  }
}
