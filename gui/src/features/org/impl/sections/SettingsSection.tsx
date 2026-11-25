import { useState } from 'react';
import { Button, Notice } from '../../../../components/primitives';
import { ORG_COPY, ORG_KIND_LABELS, ORG_LIFECYCLE_LABELS, ORG_ROLE_LABELS } from '../domain/copy';
import type { OrgConfig, OrgContextSyncResult, OrgResult } from '../domain/types';
import { ConfigEditorDialog, TemplateWizardDialog } from '../dialogs/ConfigDialogs';
import { RoutesTable, StructureView } from './ConfigViews';
import { AdoptDialog, DeleteOrgDialog, LeaveOrgDialog, RenameDialog } from '../dialogs/SettingsDialogs';
import type { WorkspaceApi } from '../workspace/api';
import {
  OrgReadStateBlock,
  OrgReadStateNotice,
  SectionHeader,
  TabList,
  TabPanel,
  UnknownBlock,
  type TabItem,
} from '../workspace/controls';

type Segment = 'basic' | 'structure' | 'comm' | 'norms' | 'versions' | 'danger';
type SyncPhase =
  | { readonly state: 'idle' }
  | { readonly state: 'running' }
  | { readonly state: 'done'; readonly result: OrgResult<OrgContextSyncResult> };

function SyncResult({ phase }: { readonly phase: SyncPhase }) {
  if (phase.state === 'idle') return null;
  if (phase.state === 'running') {
    return (
      <p className="org-pending" role="status">
        {ORG_COPY.syncConfigPending}
      </p>
    );
  }
  const result = phase.result;
  switch (result.kind) {
    case 'unavailable':
      return null;
    case 'skipped':
      return <p className="org-muted">{result.message}</p>;
    case 'failed':
      return (
        <Notice tone="warning" role="alert">
          {result.message}
          {result.code ? `（${result.code}）` : ''}
        </Notice>
      );
    case 'submitted':
      return <p className="org-pending">{result.message}</p>;
    case 'ok': {
      const value = result.value;
      return (
        <>
          <p className="org-pending" role="status">
            {ORG_COPY.syncConfigCounts(
              value.requestedSources.length,
              value.responseCount,
              value.receivedCount,
              value.rejectedCount,
            )}
          </p>
          {value.source === 'orgfs' && value.spaceId ? (
            <p className="org-muted">{ORG_COPY.syncConfigOrgFs(value.spaceId)}</p>
          ) : null}
          {value.deprecatedSourceTarget && value.sourceTargetHint ? (
            <p className="org-muted">{ORG_COPY.syncConfigDeprecated(value.sourceTargetHint)}</p>
          ) : null}
          {value.candidates.length === 0 ? (
            <p className="org-empty-inline">{ORG_COPY.syncConfigEmpty}</p>
          ) : (
            <ul className="org-diff">
              {value.candidates.map((candidate) => (
                <li key={`${candidate.source}:${candidate.path}`}>
                  版本 {candidate.version} · {candidate.publisher} · 来源 {candidate.source}
                </li>
              ))}
            </ul>
          )}
        </>
      );
    }
  }
}
type Modal =
  | { readonly kind: 'rename' }
  | { readonly kind: 'wizard' }
  | { readonly kind: 'editor'; readonly config: OrgConfig | null }
  | { readonly kind: 'adopt' }
  | { readonly kind: 'leave' }
  | { readonly kind: 'delete' };

const EMPTY_CONFIG: OrgConfig = {
  departments: [],
  lines: [],
  roles: [],
  routes: [],
  norms: { autonomy: '', escalation: '', etiquette: '' },
};

/**
 * 设置（5.9）：按动作区分权限——配置、改名、删除只给所有者；采信按本节点 owner；
 * 离开给成员；查看给所有成员。个人组织不显示结构与危险操作。
 */
export function SettingsSection({ api }: { readonly api: WorkspaceApi }) {
  const { org, detail, perms, snapshot } = api;
  const work = org?.kind === 'work';
  const segments: TabItem<Segment>[] = [{ id: 'basic', label: '基础信息' }];
  if (work) segments.push({ id: 'structure', label: '结构与部门', disabled: true });
  segments.push(
    { id: 'comm', label: '通信权限', disabled: true },
    { id: 'norms', label: '规范', disabled: true },
    { id: 'versions', label: '版本' },
  );
  if (work) segments.push({ id: 'danger', label: '危险操作' });
  const requested = api.intent?.segment;
  const [segment, setSegment] = useState<Segment>(
    segments.some((item) => item.id === requested && !item.disabled) ? (requested as Segment) : 'basic',
  );
  const [modal, setModal] = useState<Modal | null>(null);
  // 已提交、尚未生效的改名：后端返回新名称（或刷新）前，在基础信息里标出并暂停再次改名。
  const [renaming, setRenaming] = useState<string | null>(null);
  const [sync, setSync] = useState<SyncPhase>({ state: 'idle' });

  async function runSync(): Promise<void> {
    if (!api.guard(ORG_COPY.syncConfig, 'syncContext')) return;
    setSync({ state: 'running' });
    const result = await api.gateway.syncContext();
    setSync(result.kind === 'unavailable' ? { state: 'idle' } : { state: 'done', result });
  }

  if (!org) {
    return (
      <div className="org-section" data-org-section="settings">
        <SectionHeader title="设置" />
        <OrgReadStateBlock snapshot={snapshot} onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  const config = detail?.config ?? null;
  const adopted = config?.adopted ?? null;
  const candidate = config?.candidate ?? null;
  const configSpace = detail?.spaces?.find((space) => space.id === config?.spaceId) ?? null;
  const configBlocked =
    configSpace && configSpace.health === 'history-anomaly' ? ORG_COPY.historyAnomaly : null;

  const { gateway } = api;
  const canEdit = gateway.validateConfig !== undefined && gateway.publishCandidate !== undefined;
  const canImport =
    canEdit && gateway.listTemplates !== undefined && gateway.previewImport !== undefined;
  const ownerConfigActions = perms.configure && (canImport || canEdit) ? (
    <div className="org-actions">
      {canImport ? (
        <Button
          variant={adopted ? 'secondary' : 'primary'}
          disabled={configBlocked !== null}
          onClick={() => setModal({ kind: 'wizard' })}
        >
          {ORG_COPY.importTemplate}
        </Button>
      ) : null}
      {canEdit && adopted ? (
        <Button
          disabled={configBlocked !== null}
          onClick={() => setModal({ kind: 'editor', config: candidate?.config ?? adopted.config })}
        >
          {ORG_COPY.edit}
        </Button>
      ) : canEdit ? (
        <Button
          disabled={configBlocked !== null}
          onClick={() => setModal({ kind: 'editor', config: EMPTY_CONFIG })}
        >
          {ORG_COPY.startBlank}
        </Button>
      ) : null}
    </div>
  ) : null;

  return (
    <div className="org-section" data-org-section="settings" data-dev-ui="DEV-UI-ORG-013">
      <SectionHeader
        title="设置"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <OrgReadStateNotice snapshot={snapshot} onRefresh={() => void api.refresh()} />
      <TabList
        items={segments}
        value={segment}
        onChange={setSegment}
        label="设置分组"
        idPrefix="org-settings"
        variant="segments"
        narrowLabel="设置分组"
      />
      <TabPanel idPrefix="org-settings" value={segment}>
        {segment === 'basic' ? (
          <>
            <dl className="org-facts">
              <div>
                <dt>组织名</dt>
                <dd>{org.name}</dd>
              </div>
              <div>
                <dt>类型</dt>
                <dd>{ORG_KIND_LABELS[org.kind]}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>{ORG_LIFECYCLE_LABELS[org.lifecycle]}</dd>
              </div>
              <div>
                <dt>所有者</dt>
                <dd>{org.ownerLabel ?? ORG_COPY.unknown}</dd>
              </div>
              <div>
                <dt>我的角色</dt>
                <dd>{org.role ? ORG_ROLE_LABELS[org.role] : ORG_COPY.unknown}</dd>
              </div>
              <div>
                <dt>设备数</dt>
                <dd>{org.deviceCount === null ? ORG_COPY.unknown : String(org.deviceCount)}</dd>
              </div>
            </dl>
            {renaming && renaming !== org.name ? (
              <p className="org-pending" role="status">
                改名为 {renaming}：{ORG_COPY.submitted}。完成后显示新名称。
              </p>
            ) : null}
            {perms.rename && gateway.renameOrg ? (
              <Button
                disabled={Boolean(renaming && renaming !== org.name)}
                title={renaming && renaming !== org.name ? '改名处理中，完成前不能再次改名' : undefined}
                onClick={() => setModal({ kind: 'rename' })}
              >
                改名
              </Button>
            ) : null}
          </>
        ) : null}

        {segment === 'structure' ? (
          config === null ? (
            <UnknownBlock onRefresh={() => void api.refresh()} />
          ) : (
            <>
              {configBlocked ? (
                <Notice tone="warning" role="alert">
                  {configBlocked}
                </Notice>
              ) : null}
              {!adopted || adopted.config.departments.length === 0 ? (
                <div className="org-empty-inline">
                  <p>{ORG_COPY.structureEmpty}</p>
                  {ownerConfigActions}
                </div>
              ) : (
                <>
                  {ownerConfigActions}
                  <StructureView config={adopted.config} />
                </>
              )}
            </>
          )
        ) : null}

        {segment === 'comm' ? (
          <>
            <p className="org-scope-note">{ORG_COPY.admissionRule}</p>
            <h3 className="org-subheading">路由约定</h3>
            {detail?.routes ? (
              <RoutesTable routes={detail.routes} />
            ) : (
              <UnknownBlock />
            )}
            {api.tabAvailable('topology') ? <Button onClick={() => api.goTo('topology', { segment: 'communication' })}>
              {ORG_COPY.viewInTopology}
            </Button> : null}
          </>
        ) : null}

        {segment === 'norms' ? (
          adopted ? (
            <dl className="org-facts">
              <div>
                <dt>自主程度</dt>
                <dd>{adopted.config.norms.autonomy || '未设置'}</dd>
              </div>
              <div>
                <dt>升级路径</dt>
                <dd>{adopted.config.norms.escalation || '未设置'}</dd>
              </div>
              <div>
                <dt>协作礼仪</dt>
                <dd>{adopted.config.norms.etiquette || '未设置'}</dd>
              </div>
            </dl>
          ) : config === null ? (
            <UnknownBlock />
          ) : (
            <p className="org-empty-inline">还没有采信的规范。</p>
          )
        ) : null}

        {segment === 'versions' ? (
          <>
            <h3 className="org-subheading">{ORG_COPY.syncConfig}</h3>
            <p className="org-scope-note">{ORG_COPY.syncConfigScope}</p>
            <div className="org-actions">
              <Button disabled={sync.state === 'running'} onClick={() => void runSync()}>
                {sync.state === 'running' ? ORG_COPY.syncConfigPending : ORG_COPY.syncConfig}
              </Button>
            </div>
            <SyncResult phase={sync} />
            {config === null ? (
              <UnknownBlock onRefresh={() => void api.refresh()} />
            ) : (
              <>
              {configBlocked ? (
                <Notice tone="warning" role="alert">
                  {configBlocked}
                </Notice>
              ) : null}
              <dl className="org-facts">
                <div>
                  <dt>本节点已采信</dt>
                  <dd>
                    {adopted
                      ? `版本 ${adopted.version} · ${adopted.publishedAt} · ${adopted.publisher}`
                      : '还没有采信的版本'}
                  </dd>
                </div>
                <div>
                  <dt>待采信的候选</dt>
                  <dd>
                    {candidate
                      ? `版本 ${candidate.version} · ${candidate.publishedAt} · ${candidate.publisher}`
                      : '没有候选'}
                  </dd>
                </div>
              </dl>
              {candidate ? (
                <>
                  <h3 className="org-subheading">候选与已采信版本的差异</h3>
                  <ul className="org-diff">
                    {candidate.diff.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </>
              ) : null}
              <div className="org-actions">
                {perms.configure && canEdit ? (
                  <Button
                    disabled={configBlocked !== null}
                    onClick={() =>
                      setModal({
                        kind: 'editor',
                        config: candidate?.config ?? adopted?.config ?? EMPTY_CONFIG,
                      })
                    }
                  >
                    {ORG_COPY.edit}
                  </Button>
                ) : null}
                {perms.configure && canImport ? (
                  <Button disabled={configBlocked !== null} onClick={() => setModal({ kind: 'wizard' })}>
                    {ORG_COPY.importTemplate}
                  </Button>
                ) : null}
                {perms.adopt && candidate && gateway.adoptCandidate ? (
                  <Button
                    variant="primary"
                    disabled={configBlocked !== null}
                    onClick={() => setModal({ kind: 'adopt' })}
                  >
                    {ORG_COPY.adopt}
                  </Button>
                ) : null}
                {api.tabAvailable('files') ? (
                  <Button onClick={() => api.goTo('files', { segment: config.spaceId })}>
                    {ORG_COPY.viewInFiles}
                  </Button>
                ) : null}
              </div>
              <p className="org-muted">各节点所有者采信后才生效。</p>
              </>
            )}
          </>
        ) : null}

        {segment === 'danger' ? (
          <div className="org-danger">
            <section aria-labelledby="org-leave-title">
              <h3 id="org-leave-title" className="org-subheading">
                {ORG_COPY.leaveOrg}
              </h3>
              <p>离开后不再收到该组织的成员、机器和文件；需要时用新的邀请链接重新加入。</p>
              <Button
                variant="danger"
                disabled={perms.leaveBlockedReason !== null}
                onClick={() => {
                  if (api.guard(ORG_COPY.leaveOrg, 'previewLeave', 'leaveOrg')) setModal({ kind: 'leave' });
                }}
              >
                {ORG_COPY.leaveOrg}
              </Button>
              {perms.leaveBlockedReason ? (
                <p className="org-muted">{perms.leaveBlockedReason}</p>
              ) : null}
            </section>
            {perms.deleteOrg ? (
              <section aria-labelledby="org-delete-title">
                <h3 id="org-delete-title" className="org-subheading">
                  {ORG_COPY.deleteOrg}
                </h3>
                <Button
                  variant="danger"
                  onClick={() => {
                    if (api.guard(ORG_COPY.deleteOrg, 'previewDelete', 'deleteOrg')) setModal({ kind: 'delete' });
                  }}
                >
                  {ORG_COPY.deleteOrg}
                </Button>
              </section>
            ) : null}
          </div>
        ) : null}
      </TabPanel>

      {modal?.kind === 'rename' ? (
        <RenameDialog api={api} org={org} onClose={() => setModal(null)} onSubmitted={setRenaming} />
      ) : null}
      {modal?.kind === 'wizard' ? (
        <TemplateWizardDialog
          api={api}
          orgId={org.id}
          current={adopted?.config ?? null}
          onClose={() => setModal(null)}
          onOpenEditor={(next) => setModal({ kind: 'editor', config: next })}
        />
      ) : null}
      {modal?.kind === 'editor' ? (
        <ConfigEditorDialog
          api={api}
          orgId={org.id}
          initial={modal.config ?? EMPTY_CONFIG}
          onClose={() => setModal(null)}
        />
      ) : null}
      {modal?.kind === 'adopt' && candidate ? (
        <AdoptDialog
          api={api}
          orgId={org.id}
          candidateVersion={candidate.version}
          adoptedVersion={adopted?.version ?? null}
          onClose={() => setModal(null)}
        />
      ) : null}
      {modal?.kind === 'leave' ? (
        <LeaveOrgDialog api={api} org={org} onClose={() => setModal(null)} />
      ) : null}
      {modal?.kind === 'delete' ? (
        <DeleteOrgDialog api={api} org={org} onClose={() => setModal(null)} />
      ) : null}
    </div>
  );
}
