/**
 * agents 业务线 fixture：创建表单校验失败样例（implG1）。
 *
 * 表单校验是共享展示逻辑（名称与职责必填）；fixture 只提供提交草稿与
 * 预期错误文案，断言留在测试里。
 */

export interface AgentsCreateDraftFixture {
  readonly displayName: string;
  readonly role: string;
}

/** 名称与职责都缺失：期望两条就地错误并聚焦名称。 */
export const AGENTS_CREATE_DRAFT_INVALID: AgentsCreateDraftFixture = {
  displayName: '   ',
  role: '',
};

/** 仅职责缺失：期望职责错误并聚焦职责。 */
export const AGENTS_CREATE_DRAFT_ROLE_MISSING: AgentsCreateDraftFixture = {
  displayName: '研究者',
  role: ' ',
};

/** 合法草稿：可提交（本节点提交仍为暂不可用，不产生成功文案）。 */
export const AGENTS_CREATE_DRAFT_VALID: AgentsCreateDraftFixture = {
  displayName: '写作者',
  role: '内容写作',
};

export const AGENTS_CREATE_ERRORS = {
  nameRequired: '请填写 Agent 名称',
  roleRequired: '请填写 Agent 职责',
} as const;
