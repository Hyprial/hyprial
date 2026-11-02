import type { GuiPage } from '../state/gui-view-model';

export type DevModuleId =
  | 'dev-01'
  | 'dev-02'
  | 'dev-03'
  | 'dev-04'
  | 'dev-05'
  | 'dev-06';

export interface DevModuleDefinition {
  readonly id: DevModuleId;
  readonly name: string;
  readonly owner: DevModuleId;
  readonly scope: readonly string[];
}

export const DEV_MODULES: readonly DevModuleDefinition[] = [
  {
    id: 'dev-01',
    name: '账号与首次设置',
    owner: 'dev-01',
    scope: ['账号', '首次设置', '模型服务', 'Agent provider 参考样例', '本机初始化'],
  },
  {
    id: 'dev-02',
    name: 'Agent 管理',
    owner: 'dev-02',
    scope: ['Agent 创建', 'Agent 列表', 'Agent 管理'],
  },
  {
    id: 'dev-03',
    name: '聊天与飞书',
    owner: 'dev-03',
    scope: ['聊天', 'Agent 聊天参考样例', '消息', '飞书渠道'],
  },
  {
    id: 'dev-04',
    name: '组织与成员',
    owner: 'dev-04',
    scope: ['组织', '邀请', '成员'],
  },
  {
    id: 'dev-05',
    name: '设备与拓扑',
    owner: 'dev-05',
    scope: ['第二台电脑', '机器列表', '组织拓扑', 'Inspect', 'Monitor'],
  },
  {
    id: 'dev-06',
    name: '任务与 Routines',
    owner: 'dev-06',
    scope: ['PAC 任务', 'show', 'publish', 'Routines'],
  },
] as const;

export const DEV_MODULE_BY_PAGE: Readonly<Record<GuiPage, DevModuleId>> = {
  start: 'dev-01',
  settings: 'dev-01',
  'first-run': 'dev-01',
  contacts: 'dev-02',
  chat: 'dev-03',
  org: 'dev-04',
  connections: 'dev-05',
  tasks: 'dev-06',
};
