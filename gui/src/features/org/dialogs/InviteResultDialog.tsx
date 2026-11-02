import { useRef, useState } from 'react';
import { Notice } from '../../../components/primitives';
import { ORG_COPY } from '../domain/copy';
import type { InviteResult } from '../domain/types';
import { OrgDialog } from './OrgDialog';

async function writeClipboard(text: string): Promise<boolean> {
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
  if (!text || !clipboard?.writeText) {
    return false;
  }
  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * 邀请结果（5.4）：主形式为 https 链接，1 小时有效；复制后只提示“已复制”，
 * 不说“已送达”。关闭后链接不再显示，也不保存。
 */
export function InviteResultDialog({
  result,
  onClose,
}: {
  readonly result: InviteResult;
  readonly onClose: () => void;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  const leadRef = useRef<HTMLParagraphElement>(null);

  async function copy(kind: 'link' | 'command'): Promise<void> {
    const ok = await writeClipboard(kind === 'link' ? result.link : result.joinCommand);
    setCopied(ok ? `${ORG_COPY.copied}${kind === 'link' ? '邀请链接' : '加入命令'}` : ORG_COPY.copyFailed);
  }

  return (
    <OrgDialog
      title="邀请链接已生成"
      onClose={onClose}
      initialFocusRef={leadRef}
      wide
      devUi="DEV-UI-ORG-005"
      actions={[
        { label: '关闭', onClick: onClose },
        { label: ORG_COPY.copyCommand, onClick: () => void copy('command') },
        { label: ORG_COPY.copyLink, variant: 'primary', onClick: () => void copy('link') },
      ]}
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        {ORG_COPY.inviteGenerated}
      </p>
      <Notice tone="warning">{ORG_COPY.inviteSecretHint}</Notice>
      <dl className="org-facts">
        <div>
          <dt>邀请链接</dt>
          <dd>
            <code className="org-code">{result.link}</code>
          </dd>
        </div>
        <div>
          <dt>加入命令</dt>
          <dd>
            <code className="org-code">{result.joinCommand}</code>
          </dd>
        </div>
        <div>
          <dt>有效期</dt>
          <dd>1 小时内有效（{result.expiresAt} 前）</dd>
        </div>
        <div>
          <dt>短链接</dt>
          <dd>{result.shortLink ?? ORG_COPY.shortLinkUnavailable}</dd>
        </div>
      </dl>
      <p className="org-copy-result" role="status" aria-live="polite">
        {copied}
      </p>
    </OrgDialog>
  );
}
