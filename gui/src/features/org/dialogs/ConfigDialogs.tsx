import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Field, Notice, Stepper, type StepDefinition } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { ConfigValidation, ImportPreview, OrgConfig, OrgDepartment, OrgTemplate } from '../domain/types';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { useDiscardGuard } from './useDiscardGuard';
import { RoutesTable, StructureView } from '../sections/ConfigViews';

type WizardStep = 'source' | 'preview' | 'open';

const WIZARD_STEPS: readonly { id: WizardStep; label: string }[] = [
  { id: 'source', label: '选择来源' },
  { id: 'preview', label: '预览' },
  { id: 'open', label: '在编辑器中打开' },
];

/**
 * 从模板导入（5.9，3 步）：选择来源 → 预览（内容、差异、校验）→ 在编辑器中打开。
 * 最后一步关闭向导再打开编辑器，不叠加对话框；发布前不生效。
 */
export function TemplateWizardDialog({
  api,
  orgId,
  current,
  onClose,
  onOpenEditor,
}: {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly current: OrgConfig | null;
  readonly onClose: () => void;
  readonly onOpenEditor: (config: OrgConfig) => void;
}) {
  const [step, setStep] = useState<WizardStep>('source');
  const [mode, setMode] = useState<'template' | 'file'>('template');
  const [templates, setTemplates] = useState<readonly OrgTemplate[] | null>(null);
  const [templatesMessage, setTemplatesMessage] = useState<string | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const { busy, run } = useBusy();

  useEffect(() => {
    let alive = true;
    void api.gateway.listTemplates().then((result) => {
      if (!alive) return;
      if (result.kind === 'ok') {
        setTemplates(result.value);
        setTemplateId(result.value[0]?.id ?? null);
      } else {
        // 未接通时模板列表显示“状态未确认”。
        setTemplatesMessage(result.kind === 'unavailable' ? ORG_COPY.unknown : resultMessage(result));
      }
    });
    return () => {
      alive = false;
    };
  }, [api.gateway]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  async function toPreview(): Promise<void> {
    setMessage(null);
    const source =
      mode === 'template'
        ? templateId
          ? { templateId }
          : null
        : file
          ? { fileName: file.name, text: file.text }
          : null;
    if (!source) {
      setMessage(mode === 'template' ? '请选择一个模板' : '请选择配置文件');
      return;
    }
    const result = await run(() => api.gateway.previewImport(orgId, source));
    if (!result) return;
    if (result.kind === 'ok') {
      setPreview(result.value);
      setStep('preview');
      return;
    }
    setMessage(resultMessage(result));
  }

  const stepIndex = WIZARD_STEPS.findIndex((item) => item.id === step);
  const steps: StepDefinition[] = WIZARD_STEPS.map((item, index) => ({
    id: item.id,
    label: item.label,
    status: index < stepIndex ? 'completed' : index === stepIndex ? 'active' : 'pending',
  }));
  const blocked = (preview?.validation.errors.length ?? 0) > 0;

  return (
    <OrgDialog
      title={ORG_COPY.importTemplate}
      onClose={onClose}
      wide
      devUi="DEV-UI-ORG-013"
      actions={
        step === 'source'
          ? [
              { label: '取消', onClick: onClose },
              {
                label: busy ? ORG_COPY.processing : '下一步：预览',
                variant: 'primary',
                loading: busy,
                onClick: () => void toPreview(),
              },
            ]
          : step === 'preview'
            ? [
                { label: '上一步', onClick: () => setStep('source') },
                {
                  label: '下一步',
                  variant: 'primary',
                  disabled: blocked,
                  onClick: () => setStep('open'),
                },
              ]
            : [
                { label: '上一步', onClick: () => setStep('preview') },
                {
                  label: ORG_COPY.openInEditor,
                  variant: 'primary',
                  onClick: () => {
                    if (preview) onOpenEditor(preview.config);
                  },
                },
              ]
      }
    >
      <Stepper steps={steps} currentId={step} ariaLabel="导入步骤" className="org-stepper" />
      <h3 ref={headingRef} tabIndex={-1} className="org-subheading">
        {WIZARD_STEPS[stepIndex].label}
      </h3>

      {step === 'source' ? (
        <>
          <fieldset className="org-radio-group">
            <legend>来源</legend>
            <label>
              <input
                type="radio"
                name="org-import-source"
                checked={mode === 'template'}
                onChange={() => setMode('template')}
              />
              内置模板
            </label>
            <label>
              <input type="radio" name="org-import-source" checked={mode === 'file'} onChange={() => setMode('file')} />
              上传配置文件
            </label>
          </fieldset>
          {mode === 'template' ? (
            templates === null ? (
              <p className="org-state-block__title" role="status">
                {templatesMessage ?? '正在读取模板…'}
              </p>
            ) : (
              <fieldset className="org-template-list">
                <legend>内置模板</legend>
                {templates.map((template) => (
                  <label key={template.id} className="org-template">
                    <input
                      type="radio"
                      name="org-template"
                      checked={templateId === template.id}
                      onChange={() => setTemplateId(template.id)}
                    />
                    <span>
                      <strong>{template.name}</strong>
                      <span className="org-row__meta">
                        {template.description} · {template.config.departments.length} 个部门 ·{' '}
                        {template.config.roles.length} 个角色
                      </span>
                    </span>
                  </label>
                ))}
              </fieldset>
            )
          ) : (
            <Field
              label="配置文件"
              htmlFor="org-import-file"
              hint="格式与组织配置导入一致。文件只在本次导入中读取，不保存。"
            >
              <input
                id="org-import-file"
                type="file"
                accept=".json,.yaml,.yml,application/json"
                onChange={(event) => {
                  const selected = event.target.files?.[0];
                  if (!selected) {
                    setFile(null);
                    return;
                  }
                  void selected.text().then((text) => setFile({ name: selected.name, text }));
                }}
              />
            </Field>
          )}
        </>
      ) : null}

      {step === 'preview' && preview ? (
        <>
          <ValidationView validation={preview.validation} />
          <h4 className="org-subheading">与当前配置的差异{current ? '' : '（当前没有已采信的配置）'}</h4>
          <ul className="org-diff">
            {preview.diff.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <StructureView config={preview.config} />
          <h4 className="org-subheading">通信路由</h4>
          <RoutesTable routes={preview.config.routes} />
          <h4 className="org-subheading">规范</h4>
          <p>
            自主程度：{preview.config.norms.autonomy || '未设置'}；升级路径：
            {preview.config.norms.escalation || '未设置'}；协作礼仪：
            {preview.config.norms.etiquette || '未设置'}
          </p>
        </>
      ) : null}

      {step === 'open' ? <p>导入的内容会填入配置编辑器，可以继续修改。发布前不生效。</p> : null}

      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}

function ValidationView({ validation }: { readonly validation: ConfigValidation }) {
  if (validation.errors.length === 0 && validation.warnings.length === 0) {
    return (
      <p className="org-validation org-validation--ok" role="status">
        校验通过。
      </p>
    );
  }
  return (
    <div className="org-validation" role="status">
      {validation.errors.length > 0 ? (
        <>
          <p className="org-validation__title">错误（必须修正）</p>
          <ul>
            {validation.errors.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      ) : null}
      {validation.warnings.length > 0 ? (
        <>
          <p className="org-validation__title">警告（可以继续）</p>
          <ul>
            {validation.warnings.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

let rowSeq = 0;
function newId(prefix: string): string {
  rowSeq += 1;
  return `${prefix}-new-${rowSeq}`;
}

/** 成员按中英文逗号或顿号分隔；解析时才去掉空白和空项。 */
function parseMembers(text: string): readonly string[] {
  return text
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * 成员输入：编辑时保留原始文字，刚键入的分隔符和输入法合成中的拼音都不改写；
 * 每次输入都按原文解析进配置，失焦后再按解析结果整理显示。
 */
function MembersInput({
  members,
  onChange,
}: {
  readonly members: readonly string[];
  readonly onChange: (members: readonly string[]) => void;
}) {
  const [text, setText] = useState(() => members.join('，'));
  return (
    <input
      className="org-input"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(parseMembers(event.target.value));
      }}
      onBlur={() => setText(members.join('，'))}
    />
  );
}

/**
 * 配置编辑器（P10）：按结构分区编辑部门与汇报关系、业务线、常驻角色、通信路由、规范；
 * “校验”后错误必须修正；“发布候选”后显示“已发布候选，等待采信”。
 * 字段名在后端对齐时确定；未接通时发布只在本地显示“暂不可用”。
 */
export function ConfigEditorDialog({
  api,
  orgId,
  initial,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly initial: OrgConfig;
  readonly onClose: () => void;
}) {
  const [config, setConfig] = useState<OrgConfig>(initial);
  // 退出前对照的内容：打开时的配置，发布候选成功后换成已发布的内容。
  const [baseline, setBaseline] = useState<OrgConfig>(initial);
  const [validation, setValidation] = useState<ConfigValidation | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [published, setPublished] = useState(false);
  const firstRef = useRef<HTMLInputElement>(null);
  const validating = useBusy();
  const publishing = useBusy();
  const dirty = useMemo(() => JSON.stringify(config) !== JSON.stringify(baseline), [config, baseline]);
  const guard = useDiscardGuard({ dirty, onClose, initialFocusRef: firstRef });

  // 任何修改都让上次的校验与发布结果失效：发布前必须按当前内容重新校验。
  function change(update: (prev: OrgConfig) => OrgConfig): void {
    setConfig(update);
    setValidation(null);
    setPublished(false);
  }

  function updateDept(index: number, patch: Partial<OrgDepartment>): void {
    change((prev) => ({
      ...prev,
      departments: prev.departments.map((dept, i) => (i === index ? { ...dept, ...patch } : dept)),
    }));
  }

  function edit(patch: Partial<OrgConfig>): void {
    change((prev) => ({ ...prev, ...patch }));
  }

  async function validate(): Promise<ConfigValidation | null> {
    setMessage(null);
    const result = await validating.run(() => api.gateway.validateConfig(orgId, config));
    if (!result) return null;
    if (result.kind === 'ok') {
      setValidation(result.value);
      return result.value;
    }
    setMessage(resultMessage(result));
    return null;
  }

  async function publish(): Promise<void> {
    setMessage(null);
    if (validation && validation.errors.length > 0) {
      setMessage('校验有错误，修正后再发布。');
      return;
    }
    const result = await publishing.run(() => api.gateway.publishCandidate(orgId, config));
    if (!result) return;
    if (result.kind === 'submitted' || result.kind === 'ok') {
      setPublished(true);
      setBaseline(config);
      api.notify(ORG_COPY.candidatePublished);
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={guard.confirming ? '放弃未发布的修改？' : '配置编辑器'}
      description={guard.confirming ? undefined : '修改后先校验，再发布候选。是否生效由各节点 owner 采信决定。'}
      onClose={guard.requestClose}
      initialFocusRef={guard.focusRef}
      wide
      devUi="DEV-UI-ORG-013"
      actions={
        guard.confirming
          ? [
              { label: '继续编辑', onClick: guard.resume },
              { label: '放弃修改', variant: 'danger', onClick: onClose },
            ]
          : [
              { label: '关闭', onClick: guard.requestClose },
              {
                label: validating.busy ? ORG_COPY.processing : ORG_COPY.validate,
                loading: validating.busy,
                onClick: () => void validate(),
              },
              {
                label: publishing.busy ? ORG_COPY.processing : ORG_COPY.publishCandidate,
                variant: 'primary',
                loading: publishing.busy,
                disabled: validation === null || validation.errors.length > 0,
                onClick: () => void publish(),
              },
            ]
      }
    >
      {guard.confirming ? (
        <p ref={guard.confirmRef} tabIndex={-1} className="org-dialog__lead">
          关闭后，这次的修改不会保留。要让修改生效，先校验，再发布候选。
        </p>
      ) : null}
      <div className="org-editor" hidden={guard.confirming}>
        {/* 校验与发布结果固定在正文顶部，滚动编辑时也看得到。 */}
        <div className="org-editor-status">
          {published ? (
            <p className="org-validation org-validation--ok" role="status">
              {ORG_COPY.candidatePublished}
            </p>
          ) : validation ? (
            <ValidationView validation={validation} />
          ) : (
            <p className="org-muted">发布前先校验当前内容；修改后需要重新校验。</p>
          )}
          {message ? (
            <Notice tone="danger" role="alert">
              {message}
            </Notice>
          ) : null}
        </div>
        <section className="org-editor-section" aria-labelledby="org-editor-depts">
          <h3 id="org-editor-depts" className="org-subheading">
            部门与汇报关系
          </h3>
          {config.departments.length === 0 ? <p className="org-muted">还没有部门。</p> : null}
          {config.departments.map((dept, index) => (
            <div key={dept.id} className="org-editor-row" role="group" aria-label={`部门 ${index + 1}`}>
              <div className="org-editor-row__head">
                <span className="org-editor-row__title" aria-hidden="true">
                  部门 {index + 1}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="org-text-danger"
                  aria-label={zh`删除部门 ${dept.name || index + 1}`}
                  onClick={() =>
                    change((prev) => ({
                      ...prev,
                      departments: prev.departments.filter((_, i) => i !== index),
                    }))
                  }
                >
                  删除
                </Button>
              </div>
              <div className="org-editor-row__fields">
                <label>
                  <span>部门名称</span>
                  <input
                    ref={index === 0 ? firstRef : undefined}
                    className="org-input"
                    value={dept.name}
                    onChange={(event) => updateDept(index, { name: event.target.value })}
                  />
                </label>
                <label>
                  <span>负责人</span>
                  <input
                    className="org-input"
                    value={dept.lead}
                    onChange={(event) => updateDept(index, { lead: event.target.value })}
                  />
                </label>
                <label>
                  <span>上级部门</span>
                  <select
                    value={dept.parent ?? ''}
                    onChange={(event) => updateDept(index, { parent: event.target.value || null })}
                  >
                    <option value="">无</option>
                    {config.departments
                      .filter((other) => other.id !== dept.id)
                      .map((other) => (
                        <option key={other.id} value={other.id}>
                          {other.name || '未命名部门'}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  <span>成员（逗号分隔）</span>
                  <MembersInput members={dept.members} onChange={(members) => updateDept(index, { members })} />
                </label>
              </div>
            </div>
          ))}
          <Button
            size="sm"
            onClick={() =>
              change((prev) => ({
                ...prev,
                departments: [
                  ...prev.departments,
                  {
                    id: newId('dept'),
                    name: '',
                    lead: '',
                    parent: null,
                    members: [],
                  },
                ],
              }))
            }
          >
            添加部门
          </Button>
        </section>

        <RecordRows
          title="业务线"
          idPrefix="org-editor-lines"
          rows={config.lines}
          fields={[
            ['name', '业务线'],
            ['lead', '负责人'],
            ['coordinator', '协调人'],
            ['board', '看板'],
            ['repo', '代码仓库', true],
          ]}
          onChange={(lines) => edit({ lines })}
          create={() => ({
            id: newId('line'),
            name: '',
            lead: '',
            coordinator: '',
            board: '',
            repo: '',
          })}
          addLabel="添加业务线"
        />
        <RecordRows
          title="常驻角色"
          idPrefix="org-editor-roles"
          rows={config.roles}
          fields={[
            ['node', '节点'],
            ['role', '角色'],
            ['serves', '服务对象'],
          ]}
          onChange={(roles) => edit({ roles })}
          create={() => ({ id: newId('role'), node: '', role: '', serves: '' })}
          addLabel="添加常驻角色"
        />
        <RecordRows
          title="通信路由"
          idPrefix="org-editor-routes"
          rows={config.routes}
          fields={[
            ['kind', '种类'],
            ['target', '对象'],
            ['scope', '范围'],
          ]}
          onChange={(routes) => edit({ routes })}
          create={() => ({ id: newId('route'), kind: '', target: '', scope: '' })}
          addLabel="添加路由"
        />

        <section className="org-editor-section" aria-labelledby="org-editor-norms">
          <h3 id="org-editor-norms" className="org-subheading">
            规范
          </h3>
          {(
            [
              ['autonomy', '自主程度'],
              ['escalation', '升级路径'],
              ['etiquette', '协作礼仪'],
            ] as const
          ).map(([key, label]) => (
            <Field key={key} label={label} htmlFor={`org-editor-norm-${key}`}>
              <textarea
                id={`org-editor-norm-${key}`}
                className="org-textarea"
                rows={2}
                value={config.norms[key]}
                onChange={(event) =>
                  change((prev) => ({
                    ...prev,
                    norms: { ...prev.norms, [key]: event.target.value },
                  }))
                }
              />
            </Field>
          ))}
        </section>
      </div>
    </OrgDialog>
  );
}

/** 一组同构记录的行编辑：每行若干文本字段，可添加、删除。 */
function RecordRows<T extends { readonly id: string }>({
  title,
  idPrefix,
  rows,
  fields,
  onChange,
  create,
  addLabel,
}: {
  readonly title: string;
  readonly idPrefix: string;
  readonly rows: readonly T[];
  readonly fields: readonly (readonly [keyof T & string, string, boolean?])[];
  readonly onChange: (rows: readonly T[]) => void;
  readonly create: () => T;
  readonly addLabel: string;
}) {
  return (
    <section className="org-editor-section" aria-labelledby={`${idPrefix}-title`}>
      <h3 id={`${idPrefix}-title`} className="org-subheading">
        {title}
      </h3>
      {rows.length === 0 ? <p className="org-muted">还没有{title}。</p> : null}
      {rows.map((row, index) => (
        <div key={row.id} className="org-editor-row" role="group" aria-label={`${title} ${index + 1}`}>
          <div className="org-editor-row__head">
            <span className="org-editor-row__title" aria-hidden="true">
              {title} {index + 1}
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="org-text-danger"
              aria-label={`删除${title} ${index + 1}`}
              onClick={() => onChange(rows.filter((_, i) => i !== index))}
            >
              删除
            </Button>
          </div>
          <div className="org-editor-row__fields">
            {fields.map(([key, label, wide]) => (
              <label key={key} className={wide ? 'org-field--wide' : undefined}>
                <span>{label}</span>
                <input
                  className="org-input"
                  value={String(row[key] ?? '')}
                  onChange={(event) =>
                    onChange(rows.map((item, i) => (i === index ? { ...item, [key]: event.target.value } : item)))
                  }
                />
              </label>
            ))}
          </div>
        </div>
      ))}
      <Button size="sm" onClick={() => onChange([...rows, create()])}>
        {addLabel}
      </Button>
    </section>
  );
}
