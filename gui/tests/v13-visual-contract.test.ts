// PR-A 窄视觉合同（GUI-REL-A-LOCAL-001）：只覆盖 P1–P4 + X1/X2 的目标选择器。
//
// 性质声明（返修 2）：本文件是 **static source-value guard, not rendered acceptance**。
// 它只检查样式表/组件源码里的取值，不能证明任一选择器在真实页面上渲染；
// 真实渲染验收不在本文件范围（原 v13-visual-snapshot.mjs 已删除）。
// 不引用首次设置、workspace、onboarding 的旧断言。
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');

const primitivesCss = read('src/styles/primitives.css');
const buttonSource = read('src/components/ui/button.tsx');
const chatCss = read('src/features/agent-chat/impl/chat.css');
const notificationCss = read('src/components/notification.css');
const agentsCss = read('src/features/agents/impl/agents.css');
const connectionsCss = read('src/features/connections/impl/connections.css');
const startPageCss = read('src/pages/start/start-page.css');
const machinesCss = read('src/features/network/impl/machines.css');
const topologyViewsCss = read('src/features/org/impl/sections/topology-views.css');

const escape = (selector: string) => selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function block(source: string, selector: string): string {
  const match = source.match(new RegExp(`(?:^|\\n)[ \\t]*${escape(selector)}\\s*\\{([^}]*)\\}`, 's'));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return (match?.[1] ?? '').replace(/\s+/g, ' ').trim();
}

function grouped(source: string, selectors: readonly string[]): string {
  const flat = source.replace(/\s+/g, ' ');
  const signature = selectors.join(', ');
  const index = flat.indexOf(`${signature} {`);
  expect(index, `missing CSS rule group for ${signature}`).toBeGreaterThanOrEqual(0);
  return flat.slice(index + signature.length + 2, flat.indexOf('}', index)).trim();
}

describe('PR-A P1 shared field primitive', () => {
  it('sizes field sub-controls with control/space/radius tokens', () => {
    const rule = grouped(primitivesCss, ['.ui-field :is(input, select, textarea)', '.ui-native-control']);
    expect(rule).toContain('min-height: var(--ui-control-height-md);');
    expect(rule).toContain('padding: 0 var(--ui-space-5);');
    expect(rule).toContain('border-radius: var(--ui-radius-pill);');
    expect(rule).toContain('background: var(--ui-input-fill);');
  });

  it('keeps textarea on the same padding and height', () => {
    const rule = grouped(primitivesCss, ['.ui-field textarea', 'textarea.ui-native-control']);
    expect(rule).toContain('border-radius: var(--ui-radius-md);');
    expect(rule).toContain('padding: var(--ui-space-3) var(--ui-space-5);');
    expect(rule).not.toMatch(/padding-block\s*:/);
  });
});

describe('PR-A P1 button', () => {
  it('uses pill radius, 500 weight and 48/40px control heights', () => {
    expect(buttonSource).toContain('tw:rounded-[var(--ui-radius-pill)]');
    expect(buttonSource).toContain('tw:font-medium');
    expect(buttonSource).not.toContain('tw:font-bold');
    expect(buttonSource).toContain(
      "default: 'tw:min-h-[var(--ui-control-height-md)] tw:px-6 tw:py-0 tw:text-base'",
    );
    expect(buttonSource).toContain(
      "sm: 'tw:min-h-[var(--ui-control-height-sm)] tw:px-4.5 tw:py-0 tw:text-sm'",
    );
    expect(buttonSource).toContain('data-slot="button"');
  });
});

describe('PR-A P2 chat containers', () => {
  it('keeps the workspace flush and rounds messages with radius tokens', () => {
    expect(block(chatCss, '.chat-workspace')).toContain('border-radius: 0;');
    expect(block(chatCss, '.chat-workspace__message')).toContain('border-radius: var(--ui-radius-md);');
  });
});

describe('PR-A P3 notifications', () => {
  it('widens and pads the notification panel', () => {
    const rule = block(notificationCss, '.notif-panel');
    expect(rule).toContain('width: min(360px, calc(100dvw - 72px - var(--ui-space-6)));');
    expect(rule).toContain('padding: var(--ui-space-4);');
    expect(rule).toContain('border-radius: var(--ui-radius-lg);');
  });

  it('rounds items and toasts to the approved values', () => {
    expect(block(notificationCss, '.notif-item')).toContain('padding: var(--ui-space-2) var(--ui-space-3);');
    expect(block(notificationCss, '.notif-item')).toContain('border-radius: var(--ui-radius-sm);');
    expect(block(notificationCss, '.toast')).toContain('padding: var(--ui-space-3) var(--ui-space-4);');
    expect(block(notificationCss, '.toast')).toContain('border-radius: var(--ui-radius-md);');
    expect(block(notificationCss, '.toast-tone')).toContain('font-weight: 600;');
    expect(block(notificationCss, '.toast-tone')).not.toContain('border');
  });
});

describe('PR-A P4 agent, connections and provider login', () => {
  it('sizes agent form controls and dialog', () => {
    const controls = block(primitivesCss, '.ui-native-control');
    expect(read('src/features/agents/impl/AgentForm.tsx')).toContain('className="ui-native-control"');
    expect(controls).toContain('padding: 0 var(--ui-space-5);');
    expect(controls).toContain('border-radius: var(--ui-radius-pill);');
    expect(controls).toContain('min-height: var(--ui-control-height-md);');
    expect(agentsCss).not.toContain('.agent-form-guide');
    const dialog = block(agentsCss, '.agent-dialog');
    expect(dialog).toContain('width: min(480px, 100%);');
    expect(dialog).toContain('padding: var(--ui-space-6);');
    expect(dialog).toContain('border-radius: var(--ui-radius-lg);');
  });

  it('pads the model service row', () => {
    const rule = block(connectionsCss, '.model-service-row');
    expect(rule).toContain('padding: var(--ui-space-3) 0;');
    expect(rule).toContain('border-bottom: 1px solid var(--ui-border);');
  });


});

describe('PR-A X1/X2 non-overlapping page hunks', () => {
  it('pads only the reachable recommendation card to 24px', () => {
    // X1 缩窄（返修 2 裁决）：只验当前生产 StartPage 可渲染的 recommendation 卡。
    expect(block(startPageCss, '.start-page .recommendation-card.ui-card')).toContain('padding: var(--ui-space-6);');
  });

  it('rounds the machines row and topology node', () => {
    const machines = block(machinesCss, '.org-machines-row');
    expect(machines).toContain('padding: var(--ui-space-3) 0;');
    expect(machines).toContain('border-bottom: 1px solid var(--ui-border);');
    expect(block(topologyViewsCss, '.org-graph__node')).toContain('border-radius: var(--ui-radius-md);');
  });
});
