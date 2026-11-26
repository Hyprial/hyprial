import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentForm } from '../../../../src/features/agents/impl/AgentForm';
const entry = { keyId: 'gateway:key', label: 'gateway', state: 'active', provider: 'connection', createdAt: null, lastUsedAt: null, grants: [], connection: { version: 1, connectionRef: '0123456789abcdef0123456789abcdef', configRevision: 3, endpoint: 'https://model.example/v1', protocol: 'openai-completions', profile: 'openai-chat-completions/v1', preset: { id: 'custom', version: 1 } } };
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.setPointerCapture ??= () => {};
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});
function setup() {
  const submit = vi.fn();
  render(<AgentForm explicitModel mode="create" initial={{ displayName: 'new', role: '', modelServiceId: '', organizationId: null, preferredHarness: 'pi' }} credentialsApi={{ listKeys: async () => [entry] } as never} pending={false} message={null} onSubmit={submit} onCancel={() => {}} />);
  return { user: userEvent.setup(), submit };
}
it('requires an exact manual model and creates an explicit connection without provider', async () => {
  const { user, submit } = setup();
  await user.click(await screen.findByRole('combobox', { name: '连接' }));
  await user.click(await screen.findByRole('option', { name: /gateway/ }));
  await user.type(screen.getByRole('textbox', { name: '模型 ID' }), 'hy4-flash');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(submit).toHaveBeenCalledTimes(1);
  expect(submit.mock.calls[0][0]).toMatchObject({ modelConfiguration: { configuration: { authKind: 'connection', harness: 'pi', mode: 'headless', model: 'hy4-flash', configRevision: 3, connectionRef: entry.connection.connectionRef } } });
  expect(submit.mock.calls[0][0].provider).toBeUndefined();
  expect(screen.queryByLabelText('模型服务商')).toBeNull();
  expect(screen.queryByText(/deepseek-flash/)).toBeNull();
});
it('does not normalize model IDs or submit missing configuration', async () => {
  const { user, submit } = setup();
  await user.type(screen.getByRole('textbox', { name: '模型 ID' }), ' hy4-flash');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(submit).not.toHaveBeenCalled();
  expect(await screen.findByRole('alert')).toHaveTextContent('完整模型 ID');
});
