import { StrictMode } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import { buildOrgDevPreview, type OrgDevScenario } from '../../../src/dev/org/scenarios';

/** Radix Select 在 jsdom 中打开弹出层需要的 API（真实浏览器原生提供）。 */
export function stubRadixPointerApis(): void {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
}

/**
 * 以开发情形渲染完整开发组合 FixtureApp（与开发定位层“组织数据”相同的组合），动作延迟为 0。
 * strict 时与入口一样包在 StrictMode 中（开发模式会重复挂载 effect）。
 * 返回占位接口记录：组织动作不应经过它发出任何请求。
 */
export function renderOrgScenario(
  scenario: Exclude<OrgDevScenario, 'off'>,
  { strict = false, delay }: { readonly strict?: boolean; readonly delay?: number | null } = {},
) {
  const preview = buildOrgDevPreview(scenario, { latencyMs: 0 });
  if (!preview) throw new Error(`no preview for ${scenario}`);
  const user = userEvent.setup(delay === undefined ? undefined : { delay });
  const tree = (
    <OrgDomainProvider source={preview.source}>
      <FixtureApp
        dataMode="fixture"
        firstRunMode="complete"
        agentSeed={preview.agentSeed}
        agentGateway={preview.agentGateway}
        chatSeed={preview.chatSeed}
      />
    </OrgDomainProvider>
  );
  render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  return { user };
}

export async function openRail(user: ReturnType<typeof userEvent.setup>, name: string): Promise<void> {
  await user.click(screen.getByRole('button', { name }));
}

export async function openOrgTab(user: ReturnType<typeof userEvent.setup>, name: string): Promise<void> {
  const tabs = screen.getByRole('tablist', { name: '组织子页签' });
  await user.click(within(tabs).getByRole('tab', { name: new RegExp(`^${name}`) }));
}

/** 组织页持久提示区（role=status）的当前文本。 */
export function orgLive(): HTMLElement {
  const live = document.querySelector('.org-live');
  if (!(live instanceof HTMLElement)) throw new Error('missing org live region');
  return live;
}
