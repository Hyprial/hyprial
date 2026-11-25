// BUTTON-009：矩阵严格探针的负向证明——空结果或错误选择器必须失败，不能空跑。
import { describe, expect, it } from 'vitest';
import { assertProductionPrimary } from '../support/button-matrix-assertions.mjs';

const valid = {
  tag: 'BUTTON', slot: 'button', className: 'ui-button ui-button--primary',
  radius: '999px', background: 'rgb(26, 22, 32)', color: 'rgb(255, 255, 255)', border: 'rgb(26, 22, 32)',
};

describe('production primary matrix probe', () => {
  it('fails when the selector matches nothing (old .bootstrap-card__primary regression)', () => {
    expect(() => assertProductionPrimary([], 1440)).toThrow();
  });

  it('fails when the node is not the shared primary button', () => {
    expect(() => assertProductionPrimary([{ ...valid, className: 'bootstrap-card__primary', slot: null }], 1440)).toThrow();
  });

  it('fails on a non-ink or non-pill production button', () => {
    expect(() => assertProductionPrimary([{ ...valid, radius: '0px' }], 1440)).toThrow();
    expect(() => assertProductionPrimary([{ ...valid, background: 'rgb(0, 72, 168)' }], 1440)).toThrow();
  });

  it('passes for the real shared primary contract', () => {
    expect(() => assertProductionPrimary([valid], 1440)).not.toThrow();
  });
});
