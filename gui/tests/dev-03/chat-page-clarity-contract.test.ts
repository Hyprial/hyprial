/**
 * Decision record (chat UI clarity, 2026-11-16):
- These are static source guards for the chat layout decisions: a target role
  that is only the address is kept off the narrow list column (the full address
  stays in object information), the conversation heading and composer own their
  separation, and the mobile page must not add a second bottom reservation on
  top of the one the shell already gives the fixed navigation.
- They do not replace the rendered browser check; screenshots and measured
  heights for this change live in docs/notes/chat-ui-clarity-2026-11-16.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');
const chatCss = read('src/features/agent-chat/impl/chat.css');
const appCss = read('src/styles/app.css');

describe('chat page clarity contract', () => {
  it('keeps a role that is only the address out of the narrow list column', () => {
    expect(chatCss).toContain(
      '.chat-workspace__target-copy > .chat-workspace__target-role { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
    );
    expect(chatCss).toContain(
      '.chat-workspace__facts .chat-workspace__address { color: var(--ui-ink-2); font-family: var(--ui-font-mono); font-size: var(--ui-text-xs); line-height: var(--ui-leading-normal); overflow-wrap: anywhere; }',
    );
  });

  it('separates the conversation heading and composer with hairlines', () => {
    expect(chatCss).toContain(
      '.chat-workspace__heading { display: flex; align-items: center; justify-content: space-between; gap: var(--ui-space-3); padding: var(--ui-space-3) var(--ui-space-5); border-bottom: 1px solid var(--ui-border); }',
    );
    expect(chatCss).toContain(
      '.chat-composer { min-width: 0; padding: var(--ui-space-3) var(--ui-space-5) var(--ui-space-4); border-top: 1px solid var(--ui-border); background: var(--ui-surface); }',
    );
  });

  it('gives an opened system message its own readable detail block', () => {
    expect(chatCss).toContain(
      '.chat-workspace__message[data-author="system"] { align-self: flex-start; max-width: min(100%, 40rem); padding: 0; }',
    );
    expect(chatCss).toContain(
      '.chat-workspace__system-heading { display: flex; align-items: baseline; gap: var(--ui-space-2); }',
    );
    expect(chatCss).toContain(
      '.chat-workspace__system > summary { display: flex; align-items: center; gap: var(--ui-space-2); width: 100%; min-height: var(--ui-control-height-sm); padding: var(--ui-space-2) var(--ui-space-3); border-radius: var(--ui-radius-sm); color: var(--ui-ink-1); font-size: var(--ui-text-sm); font-weight: 500; cursor: pointer; list-style: none; }',
    );
    expect(chatCss).toContain(
      '.chat-workspace__system-body > p { max-width: 64ch; margin: 0; color: var(--ui-ink-2); font-size: var(--ui-text-sm); line-height: var(--ui-leading-relaxed); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }',
    );
  });

  it('does not reserve the intermediate and mobile bottom space twice', () => {
    const narrow = appCss.slice(appCss.indexOf('@media (max-width: 979px)'));
    const rule = narrow.match(/\.page-chat\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toContain('display: flex;');
    expect(rule).toContain('height: 100%;');
    expect(rule).not.toContain('padding-bottom');
  });
});
