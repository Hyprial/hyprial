import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import { OrgResultShapeError, type OrgRequestPort } from '../../../src/features/org/impl/api/org-request-api';
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

function folderHistory(item: ReturnType<typeof folderNode>, at: string, deleted: boolean) {
  return { author: 'user:me', at, node: { ...item, deleted } };
}

/**
 * 目录恢复的可见点击场景：d1 是文件夹，c1 是本次删除批次的子项，c3 是更早已删除的子项。
 * 刷新后的列表由 deleted 集合同步生成，恢复动作只改变集合，不伪造成功。
 */
function folderRestorePort(options: {
  readonly failOn?: string;
  readonly failure?: unknown;
  readonly failRefresh?: boolean;
} = {}) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const folder = folderNode('d1', 'dir', 'dir');
  const keep = folderNode('c1', 'dir/keep.md', 'doc');
  const old = folderNode('c3', 'dir/old.md', 'doc');
  const nodes = new Map([folder, keep, old].map((item) => [item.node_id, item]));
  const deleted = new Map([
    ['d1', true],
    ['c1', true],
    ['c3', true],
  ]);
  const contents = new Map([
    ['dir/keep.md', 'keep'],
    ['dir/old.md', 'old'],
  ]);
  const histories = new Map([
    [
      'id:d1',
      [
        folderHistory(folder, '2026-11-03T10:00:03Z', true),
        {
          ...folderHistory(folder, '2026-11-03T10:00:00Z', false),
          node: { ...folder, deleted: false, version: 'folder-pre' },
        },
      ],
    ],
    [
      'id:c1',
      [
        folderHistory(keep, '2026-11-03T10:00:03Z', true),
        {
          ...folderHistory(keep, '2026-11-03T10:00:00Z', false),
          node: { ...keep, deleted: false, version: 'keep-pre' },
        },
      ],
    ],
    [
      'id:c3',
      [
        folderHistory(old, '2026-11-03T10:00:03Z', true),
        { ...folderHistory(old, '2026-11-03T10:00:01Z', true), node: { ...old, version: 'old-already-deleted' } },
      ],
    ],
  ]);
  let listCalls = 0;
  const base: Record<string, unknown> = {
    'org.list': () => {
      listCalls += 1;
      if (options.failRefresh && listCalls > 1) throw new Error('refresh unavailable');
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
      durable_holders_seen: 1,
      holders_online: 1,
      parked_commits: 0,
      unconfirmed_commits: 0,
    },
    'orgfs.ls': () => ({
      nodes: [...nodes.values()]
        .filter((item) => !deleted.get(item.node_id))
        .map((item) => ({ ...item, deleted: false })),
    }),
    'orgfs.trash': () => ({
      nodes: [...nodes.values()].filter((item) => deleted.get(item.node_id)),
    }),
    'orgfs.history': (params: Record<string, unknown>) => ({
      history: histories.get(String(params.path)) ?? [],
    }),
    'orgfs.read': (params: Record<string, unknown>) => {
      const path = String(params.path);
      const item = [...nodes.values()].find((entry) => entry.path === path);
      if (!item) throw new Error(`unexpected read ${path}`);
      return { text: contents.get(path) ?? '', node: item, version: item.version };
    },
    'orgfs.restore': (params: Record<string, unknown>) => {
      const id = String(params.node).replace(/^id:/, '');
      if (id === options.failOn) throw options.failure ?? new Error('restore denied');
      const item = nodes.get(id);
      if (!item) throw new Error(`unexpected restore ${id}`);
      deleted.set(id, false);
      return { ...item, deleted: false, version: params.version };
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

describe('文件：历史与文件夹', () => {
  it('历史按文件区分：看过 onboarding.md 的版本后切到 README.md，不显示别的文件的内容', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: 'onboarding.md' }));
    await user.click(screen.getByRole('button', { name: '历史' }));
    await user.click(within(screen.getByRole('list', { name: '版本记录' })).getAllByRole('button')[0]);
    expect(screen.getByLabelText('版本 3 的内容')).toHaveTextContent('新成员入门');

    await user.click(screen.getByRole('button', { name: 'README.md' }));
    await user.click(screen.getByRole('button', { name: '历史' }));
    const panel = screen.getByRole('complementary', { name: '历史：README.md' });
    expect(within(panel).getByText('没有版本记录。')).toBeInTheDocument();
    expect(panel).not.toHaveTextContent('新成员入门');
    expect(screen.queryByLabelText(/^版本 \d+ 的内容$/)).not.toBeInTheDocument();

    // 回到 onboarding.md 重新打开历史：不沿用上次选中的版本。
    await user.click(screen.getByRole('button', { name: 'onboarding.md' }));
    await user.click(screen.getByRole('button', { name: '历史' }));
    expect(screen.getByRole('complementary', { name: '历史：onboarding.md' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/^版本 \d+ 的内容$/)).not.toBeInTheDocument();
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

  it('删除带子项的文件夹：确认说明连同子项进回收站；目录恢复入口明确禁用', async () => {
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
    expect(restore).toHaveAttribute(
      'title',
      '将恢复到原路径，并恢复文件夹中的内容。',
    );
  });
});

describe('目录恢复：部分结果跨刷新保留', () => {
  it('第二项被明确拒绝：刷新后对话框仍在，显示已恢复项且不自动重试', async () => {
    const { port, calls, deleted } = folderRestorePort({ failOn: 'c1' });
    const { user } = renderLiveOrg(port);
    const dialog = await confirmFolderRestore(user);

    expect(
      await within(dialog).findByText(/已确认 1\/2 项恢复；第 2 项明确失败/),
    ).toBeInTheDocument();
    const stillOpen = screen.getByRole('dialog', { name: '恢复 dir？' });
    expect(stillOpen).toBeInTheDocument();
    expect(within(stillOpen).getByRole('button', { name: '恢复' })).toBeDisabled();
    // 刷新后的真实成员：文件夹已恢复，keep 仍在回收站，更早删除的 old 没有被复活。
    expect(await screen.findByText('dir/keep.md')).toBeInTheDocument();
    expect(await screen.findByText('dir/old.md')).toBeInTheDocument();
    expect(deleted.get('d1')).toBe(false);
    expect(deleted.get('c1')).toBe(true);
    expect(deleted.get('c3')).toBe(true);
    expect(restoreCalls(calls)).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(restoreCalls(calls)).toHaveLength(2);
  });

  it('第一项结果未知：显示 0/2 与 unknown，刷新后仍可见且不继续恢复', async () => {
    const { port, calls } = folderRestorePort({
      failOn: 'd1',
      failure: new OrgResultShapeError('restore'),
    });
    const { user } = renderLiveOrg(port);
    const dialog = await confirmFolderRestore(user);

    expect(
      await within(dialog).findByText(/已确认 0\/2 项恢复；当前第 1 项结果未知/),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/结果未知/);
    expect(restoreCalls(calls)).toHaveLength(1);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(restoreCalls(calls)).toHaveLength(1);
  });

  it('第二项结果未知：保留 unknown，第一项已确认，不重试第二项', async () => {
    const { port, calls, deleted } = folderRestorePort({
      failOn: 'c1',
      failure: new OrgResultShapeError('restore'),
    });
    const { user } = renderLiveOrg(port);
    const dialog = await confirmFolderRestore(user);

    expect(
      await within(dialog).findByText(/已确认 1\/2 项恢复；当前第 2 项结果未知/),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/结果未知/);
    expect(deleted.get('d1')).toBe(false);
    expect(restoreCalls(calls)).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(restoreCalls(calls)).toHaveLength(2);
  });

  it('刷新失败：保留原写结果与刷新提示，不出现空白且可再次确认的旧操作', async () => {
    const { port, calls } = folderRestorePort({ failOn: 'c1', failRefresh: true });
    const { user } = renderLiveOrg(port);
    const dialog = await confirmFolderRestore(user);

    expect(
      await within(dialog).findByText(/已确认 1\/2 项恢复；第 2 项明确失败/),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/刷新失败，页面可能仍是旧状态/);
    expect(screen.getByRole('dialog', { name: '恢复 dir？' })).toBeInTheDocument();
    expect(restoreCalls(calls)).toHaveLength(2);
    expect(calls.filter((call) => call.method === 'org.list')).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(restoreCalls(calls)).toHaveLength(2);
  });
});
