import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import type { OrgRequestPort } from '../../../src/features/org/impl/api/org-request-api';
import { createOrgLiveSourceForPort } from '../../support/org-backend';
import { openOrgTab, openRail, orgLive, renderOrgScenario, stubRadixPointerApis } from './org-render';

beforeAll(stubRadixPointerApis);

type User = ReturnType<typeof renderOrgScenario>['user'];

const ORG = 'group-hyprial-dev';
const SPACE = 'space-team';

function folderNode(id: string, path: string, kind: 'dir' | 'doc', version = 'delete-batch') {
  return {
    node_id: id,
    path,
    name: path.split('/').at(-1) ?? path,
    kind,
    modified_by: 'user:me',
    modified_via: null,
    blob_hash: null,
    contentState: 'arrived' as const,
    deleted: true,
    doc_id: kind === 'doc' ? `doc-${id}` : null,
    name_conflict: false,
    size: 0,
    space_id: SPACE,
    uri: `orgfs:owner:${SPACE}:${id}`,
    version,
  };
}

/**
 * 目录恢复的可见点击场景：d1 是文件夹，c1 与它同一删除批次，e1 在文件夹之前已单独删除。
 * 假 daemon 按设计 §6 的 `restore(node, recursive=True)` 恢复同批次的后代；GUI 只发出一次
 * 只带节点的 `orgfs.restore`，不自己挑选子项。
 */
function folderRestorePort() {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const folder = folderNode('d1', 'dir', 'dir');
  const keep = folderNode('c1', 'dir/keep.md', 'doc');
  const early = folderNode('e1', 'dir/early.md', 'doc', 'earlier-batch');
  const nodes = new Map([folder, keep, early].map((item) => [item.node_id, item]));
  const deleted = new Map([
    ['d1', true],
    ['c1', true],
    ['e1', true],
  ]);
  const contents = new Map([
    ['dir/keep.md', 'keep'],
    ['dir/early.md', 'early'],
  ]);
  const base: Record<string, unknown> = {
    'org.list': () => {
      return {
        orgs: [{ org: ORG, spaceId: SPACE, role: 'owner', devices: 1, state: 'active' }],
        leftOrgs: [],
      };
    },
    'org.network': {
      orgs: [ORG],
      unavailableOrgs: [],
      dataProblems: [],
      directory: {
        [ORG]: [
          {
            deviceId: 'dev-1',
            owner: 'user:me',
            presence: 'online',
            updatedAt: '2026-11-02T10:00:00Z',
          },
        ],
      },
      self: { deviceId: 'dev-1', owner: 'user:me' },
    },
    hosts: { hosts: [] },
    'orgfs.spaces': {
      spaces: [{ space_id: SPACE, name: 'team', owner: 'user:me', created_at: '2026-11-02T10:00:00Z' }],
    },
    'orgfs.status': {
      space_id: SPACE,
      synced: true,
      holders_online: ['dev-1'],
      content_pending: 0,
      rejected_updates: 0,
    },
    'orgfs.ls': () => ({
      nodes: [...nodes.values()]
        .filter((item) => !deleted.get(item.node_id))
        .map((item) => ({ ...item, deleted: false })),
    }),
    'orgfs.trash': () => ({
      nodes: [...nodes.values()].filter((item) => deleted.get(item.node_id)),
    }),
    'orgfs.read': (params: Record<string, unknown>) => {
      const path = String(params.path);
      const item = [...nodes.values()].find((entry) => entry.path === path);
      if (!item) throw new Error(`unexpected read ${path}`);
      return { text: contents.get(path) ?? '', node: item, version: item.version };
    },
    'orgfs.restore': (params: Record<string, unknown>) => {
      const id = String(params.node).replace(/^id:/, '');
      const item = nodes.get(id);
      if (!item) throw new Error(`unexpected restore ${id}`);
      deleted.set(id, false);
      if (item.kind === 'dir' && params.recursive !== false) {
        for (const child of nodes.values()) {
          if (child.path.startsWith(`${item.path}/`) && child.version === item.version) {
            deleted.set(child.node_id, false);
          }
        }
      }
      return { ...item, deleted: false, version: 'after-restore' };
    },
  };
  const port: OrgRequestPort = {
    async request(method, params) {
      calls.push({ method, params });
      const value = base[method];
      if (typeof value === 'function') return value(params);
      if (value === undefined) throw new Error(`unexpected method ${method}`);
      return value;
    },
  };
  return { port, calls, deleted };
}

function renderLiveOrg(port: OrgRequestPort) {
  const source = createOrgLiveSourceForPort(port);
  const user = userEvent.setup();
  render(
    <OrgDomainProvider source={source}>
      <FixtureApp dataMode="fixture" firstRunMode="complete" />
    </OrgDomainProvider>,
  );
  return { user, source };
}

async function openTrash(user: User): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: '组织' }));
  await screen.findByRole('heading', { name: 'hyprial-dev' });
  await openOrgTab(user, '文件');
  const groups = screen.getByRole('tablist', { name: '文件分组' });
  await user.click(within(groups).getByRole('tab', { name: '回收站' }));
  return screen.getByRole('list', { name: '回收站' });
}

async function confirmFolderRestore(user: User): Promise<HTMLElement> {
  const trash = await openTrash(user);
  const row = within(trash)
    .getAllByRole('listitem')
    .find((item) => item.textContent?.startsWith('dir/'));
  expect(row).toBeDefined();
  await user.click(within(row as HTMLElement).getByRole('button', { name: '恢复' }));
  const dialog = screen.getByRole('dialog', { name: '恢复 dir？' });
  await user.click(within(dialog).getByRole('button', { name: '恢复' }));
  return dialog;
}

function restoreCalls(calls: { method: string }[]) {
  return calls.filter((call) => call.method === 'orgfs.restore');
}

async function openConfigEditor(user: User): Promise<HTMLElement> {
  await openRail(user, '组织');
  await openOrgTab(user, '设置');
  await user.click(screen.getByRole('tab', { name: '结构与部门' }));
  await user.click(screen.getByRole('button', { name: '编辑' }));
  return screen.getByRole('dialog', { name: '配置编辑器' });
}

function membersField(editor: HTMLElement): HTMLInputElement {
  return within(editor).getAllByRole<HTMLInputElement>('textbox', { name: '成员（逗号分隔）' })[0];
}

describe('配置编辑器：成员输入', () => {
  it.todo('逐字键入中英文逗号不吞分隔符；失焦后整理显示；发布、采信后按解析结果保存', async () => {
    // 逐字符事件全部保留，只去掉事件间默认延迟，避免高并发下压线。
    const { user } = renderOrgScenario('owner', { delay: null });
    const editor = await openConfigEditor(user);
    const members = membersField(editor);
    await user.clear(members);
    await user.type(members, '姚，林澄');
    expect(members.value).toBe('姚，林澄');
    await user.type(members, ',周屿,');
    expect(members.value).toBe('姚，林澄,周屿,');
    await user.tab();
    expect(members.value).toBe('姚，林澄，周屿');

    await user.click(within(editor).getByRole('button', { name: '校验' }));
    const publish = within(editor).getByRole('button', { name: '发布候选' });
    await vi.waitFor(() => expect(publish).toBeEnabled());
    await user.click(publish);
    expect(await within(editor).findByText('已发布候选，等待采信')).toBeInTheDocument();
    // 已发布且没有新改动：直接关闭，不再确认。
    await user.click(within(editor).getByRole('button', { name: '关闭' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: '版本' }));
    await user.click(screen.getByRole('button', { name: '采信' }));
    await user.click(
      within(screen.getByRole('dialog', { name: /^采信版本 \d+？$/ })).getByRole('button', { name: '确认采信' }),
    );
    expect(await within(orgLive()).findByText(/本节点已采信版本 \d+。/)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: '结构与部门' }));
    expect(screen.getByRole('row', { name: /^研发部/ })).toHaveTextContent('姚、林澄、周屿');
  });

  it.todo('删除到分隔符为止时保留分隔符；中文输入法合成中的拼音不被改写', async () => {
    const { user } = renderOrgScenario('owner');
    const members = membersField(await openConfigEditor(user));
    expect(members.value).toBe('姚，陈墨');
    await user.type(members, '{Backspace}{Backspace}');
    expect(members.value).toBe('姚，');

    // 输入法合成：先出现拼音（音节之间有空格），确认后换成汉字。
    fireEvent.compositionStart(members);
    fireEvent.change(members, { target: { value: '姚，lin cheng' } });
    expect(members.value).toBe('姚，lin cheng');
    fireEvent.change(members, { target: { value: '姚，林澄' } });
    fireEvent.compositionEnd(members);
    expect(members.value).toBe('姚，林澄');
    await user.tab();
    expect(members.value).toBe('姚，林澄');
  });
});

describe('编辑器：未保存的修改在退出前确认', () => {
  it.todo('配置编辑器有未发布的修改：Esc、×、关闭都在同一对话框内确认；继续编辑保留内容，放弃后恢复为已采信内容', async () => {
    const { user } = renderOrgScenario('owner');
    const editor = await openConfigEditor(user);
    const name = within(editor).getAllByRole<HTMLInputElement>('textbox', { name: '部门名称' })[0];
    await user.clear(name);
    await user.type(name, '未保存的部门');

    await user.keyboard('{Escape}');
    const confirm = screen.getByRole('dialog', { name: '放弃未发布的修改？' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(within(confirm).queryByRole('textbox', { name: '部门名称' })).not.toBeInTheDocument();
    await user.click(within(confirm).getByRole('button', { name: '继续编辑' }));
    const back = screen.getByRole('dialog', { name: '配置编辑器' });
    const restored = within(back).getAllByRole<HTMLInputElement>('textbox', { name: '部门名称' })[0];
    expect(restored.value).toBe('未保存的部门');
    expect(restored).toHaveFocus();

    // Esc 在确认步骤里等于“继续编辑”。
    await user.click(within(back).getByRole('button', { name: '关闭对话框' }));
    expect(screen.getByRole('dialog', { name: '放弃未发布的修改？' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: '配置编辑器' })).toBeInTheDocument();

    await user.click(within(screen.getByRole('dialog', { name: '配置编辑器' })).getByRole('button', { name: '关闭' }));
    await user.click(
      within(screen.getByRole('dialog', { name: '放弃未发布的修改？' })).getByRole('button', { name: '放弃修改' }),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '编辑' }));
    const reopened = screen.getByRole('dialog', { name: '配置编辑器' });
    expect(within(reopened).getAllByRole<HTMLInputElement>('textbox', { name: '部门名称' })[0].value).toBe('研发部');
  });

  it.todo('配置编辑器没有修改时 Esc 直接关闭', async () => {
    const { user } = renderOrgScenario('owner');
    await openConfigEditor(user);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('文件编辑器有未保存的修改：退出前确认；放弃后预览仍是原内容', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: 'README.md' }));
    await user.click(screen.getByRole('button', { name: '编辑' }));
    const editor = screen.getByRole('dialog', { name: '编辑 README.md' });
    await user.type(within(editor).getByRole('textbox', { name: '文件内容' }), '没保存的一行');
    await user.keyboard('{Escape}');
    const confirm = screen.getByRole('dialog', { name: '放弃未保存的修改？' });
    await user.click(within(confirm).getByRole('button', { name: '继续编辑' }));
    const text = within(screen.getByRole('dialog', { name: '编辑 README.md' })).getByRole<HTMLTextAreaElement>(
      'textbox',
      { name: '文件内容' },
    );
    expect(text.value).toContain('没保存的一行');
    await user.keyboard('{Escape}');
    await user.click(
      within(screen.getByRole('dialog', { name: '放弃未保存的修改？' })).getByRole('button', { name: '放弃修改' }),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: '预览 README.md' })).not.toHaveTextContent('没保存的一行');
  });
});

describe('文件：文件与文件夹', () => {
  it('选中文件只提供编辑、移动、删除，没有历史入口', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: 'onboarding.md' }));
    const preview = screen.getByRole('region', { name: '预览 onboarding.md' });
    expect(within(preview).getAllByRole('button').map((button) => button.textContent)).toEqual(['编辑', '移动', '删除']);
    expect(screen.queryByRole('button', { name: '历史' })).not.toBeInTheDocument();
  });

  it('新建的文件夹可以选中，移动或改名后选中跟随新位置', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: '新建' }));
    const create = screen.getByRole('dialog', { name: '新建' });
    await user.click(within(create).getByRole('radio', { name: '文件夹' }));
    await user.type(within(create).getByRole('textbox', { name: /^路径/ }), 'review-folder');
    await user.click(within(create).getByRole('button', { name: '新建' }));

    const folder = await screen.findByRole('button', { name: /review-folder\/$/ });
    expect(folder).toHaveAttribute('aria-pressed', 'true');
    const details = screen.getByRole('region', { name: '文件夹 review-folder' });
    expect(details).toHaveTextContent('包含 0 项');
    await user.click(within(details).getByRole('button', { name: '移动' }));
    const move = screen.getByRole('dialog', { name: '移动或重命名 review-folder' });
    const path = within(move).getByRole('textbox', { name: /^新路径/ });
    await user.clear(path);
    await user.type(path, 'review-notes');
    await user.click(within(move).getByRole('button', { name: '移动' }));
    expect(await screen.findByRole('button', { name: /review-notes\/$/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: /review-folder\/$/ })).not.toBeInTheDocument();
  });

  it('文件夹不能移到自己里面；选中文件夹后“新建”默认建在其中', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: /guides\/$/ }));
    await user.click(within(screen.getByRole('region', { name: '文件夹 guides' })).getByRole('button', { name: '移动' }));
    const move = screen.getByRole('dialog', { name: '移动或重命名 guides' });
    const path = within(move).getByRole('textbox', { name: /^新路径/ });
    await user.clear(path);
    await user.type(path, 'guides/old');
    await user.click(within(move).getByRole('button', { name: '移动' }));
    expect(await within(move).findByText('不能把文件夹移到它自己里面')).toBeInTheDocument();
    await user.click(within(move).getByRole('button', { name: '取消' }));

    await user.click(screen.getByRole('button', { name: '新建' }));
    expect(
      within(screen.getByRole('dialog', { name: '新建' })).getByRole<HTMLInputElement>('textbox', { name: /^路径/ })
        .value,
    ).toBe('guides/');
  });

  it('删除带子项的文件夹：确认说明连同子项进回收站；恢复文件夹后子项一起回到原位置', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: /guides\/$/ }));
    const details = screen.getByRole('region', { name: '文件夹 guides' });
    expect(details).toHaveTextContent('包含 1 项');
    await user.click(within(details).getByRole('button', { name: '删除' }));
    const remove = screen.getByRole('dialog', { name: '删除 guides？' });
    expect(remove).toHaveTextContent('文件夹和其中的 1 项');
    await user.click(within(remove).getByRole('button', { name: '删除' }));
    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'onboarding.md' })).not.toBeInTheDocument());

    const groups = screen.getByRole('tablist', { name: '文件分组' });
    await user.click(within(groups).getByRole('tab', { name: '回收站' }));
    const row = within(screen.getByRole('list', { name: '回收站' }))
      .getAllByRole('listitem')
      .find((item) => item.textContent?.startsWith('guides/'));
    expect(row).toBeDefined();
    expect(row).toHaveTextContent('含 1 项');
    const restore = within(row as HTMLElement).getByRole('button', { name: '恢复' });
    expect(restore).toBeEnabled();
    expect(restore).not.toHaveAttribute('title');
    await user.click(restore);
    const dialog = screen.getByRole('dialog', { name: '恢复 guides？' });
    await user.click(within(dialog).getByRole('button', { name: '恢复' }));
    await vi.waitFor(() => expect(dialog).not.toBeInTheDocument());
    await user.click(within(groups).getByRole('tab', { name: '文件' }));
    await user.click(await screen.findByRole('button', { name: /guides\/$/ }));
    expect(screen.getByRole('region', { name: '文件夹 guides' })).toHaveTextContent('包含 1 项');
    expect(screen.getByRole('button', { name: 'onboarding.md' })).toBeInTheDocument();
  });
});

describe('目录恢复', () => {
  it('恢复文件夹：只发出一次带文件夹节点的 orgfs.restore；同批删除的子项回来，更早删除的留在回收站', async () => {
    const { port, calls, deleted } = folderRestorePort();
    const { user } = renderLiveOrg(port);
    const dialog = await confirmFolderRestore(user);

    await vi.waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(restoreCalls(calls)).toEqual([
      { method: 'orgfs.restore', params: { spaceId: SPACE, node: 'id:d1' } },
    ]);
    expect(calls.some((call) => call.method === 'orgfs.stat')).toBe(false);
    expect(deleted.get('d1')).toBe(false);
    expect(deleted.get('c1')).toBe(false);
    expect(deleted.get('e1')).toBe(true);
    const trash = screen.getByRole('list', { name: '回收站' });
    await vi.waitFor(() => expect(within(trash).queryByText('dir/keep.md')).not.toBeInTheDocument());
    expect(within(trash).getByText('dir/early.md')).toBeInTheDocument();
  });
});
