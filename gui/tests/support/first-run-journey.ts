import { screen } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';

export async function enterRuntimeConfig(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  const initialize = screen.queryByRole('button', { name: '初始化当前设备' });
  if (initialize) await user.click(initialize);
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  await screen.findByRole('heading', { name: '模型服务' });
}

export async function loginPreview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await enterRuntimeConfig(user);
}
export async function preparePreview(user: ReturnType<typeof userEvent.setup>) {
  await loginPreview(user);
  await user.type(screen.getByLabelText(/DeepSeek API Key/), 'preview-key-not-real');
  await user.click(screen.getByRole('button', { name: '保存并继续' }));
  await screen.findByRole('heading', { name: '首次对话' });
}
export async function completeFirstRun(user: ReturnType<typeof userEvent.setup>) {
  await preparePreview(user);
  await user.type(screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' }), '你好');
  await user.click(screen.getByRole('button', { name: '发送' }));
  await screen.findByRole('navigation', { name: '主导航' });
  await user.click(screen.getByRole('button', { name: '开始' }));
}
