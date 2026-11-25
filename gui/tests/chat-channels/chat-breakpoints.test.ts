import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(resolve(import.meta.dirname, '../../src/styles/app.css'), 'utf8');

describe('chat responsive contract', () => {
  it('keeps two columns at 760-979 and only collapses to one column at 759 and below', () => {
    const mid = css.slice(css.indexOf('@media (max-width: 979px)'));
    expect(mid).toContain('.page-chat .chat-layout--readonly { grid-template-columns: 240px minmax(0, 1fr); }');
    expect(mid).toContain('.page-chat .chat-info { display: none; }');
    const small = css.slice(css.indexOf('@media (max-width: 759px)'));
    expect(small).toContain('grid-template-columns: minmax(0, 1fr);');
    expect(css).not.toContain('@media (max-width: 979px) {\n  .page-chat .chat-list-scroll {\n    display: flex;');
  });
});
