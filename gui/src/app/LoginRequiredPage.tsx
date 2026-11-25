/** Shared signed-out page state; protected controllers are mounted only after account confirmation. */
import { Button, Card, EmptyState, PageHeader } from '../components/primitives';
import type { GuiPage } from '../state/gui-view-model';

const titles: Partial<Record<GuiPage, string>> = {
  contacts: 'Agent', connections: 'Agent 设置', chat: '聊天', org: '组织',
};

export function LoginRequiredPage({ page, confirmed, onLogin, onRefresh }: {
  readonly page: GuiPage; readonly confirmed: boolean;
  readonly onLogin: () => void; readonly onRefresh: () => void;
}) {
  return <section className="page" data-page={page}>
    <PageHeader title={titles[page] ?? '开始'} />
    <Card padding="lg">
      <EmptyState title={confirmed ? '请先登录' : '账号状态未确认'} />
      <Button variant="primary" onClick={onLogin}>去登录</Button>
      {!confirmed && <Button variant="secondary" onClick={onRefresh}>刷新账号状态</Button>}
    </Card>
  </section>;
}
