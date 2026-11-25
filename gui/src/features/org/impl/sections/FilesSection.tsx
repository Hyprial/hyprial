import { useState } from 'react';
import { Button, Notice, Select } from '../../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import { spaceWriteBlock } from '../domain/selectors';
import type { OrgFileEntry, TrashEntry } from '../domain/types';
import {
  DeleteEntryDialog,
  FileEditorDialog,
  MoveEntryDialog,
  NewEntryDialog,
  RestoreDialog,
} from '../dialogs/FileDialogs';
import type { OrgGuardKey } from '../domain/unwired';
import type { WorkspaceApi } from '../workspace/api';
import {
  MetaParts,
  OrgReadStateBlock,
  SectionHeader,
  TabList,
  TabPanel,
  UnknownBlock,
} from '../workspace/controls';

type Segment = 'files' | 'trash';
type FileDialog =
  | { readonly kind: 'edit'; readonly entry: OrgFileEntry }
  | { readonly kind: 'new'; readonly parent: string | null }
  | { readonly kind: 'move'; readonly entry: OrgFileEntry }
  | { readonly kind: 'delete'; readonly entry: OrgFileEntry; readonly childCount: number }
  | {
      readonly kind: 'restore';
      readonly entry: TrashEntry;
      /** 恢复期间可能触发刷新；空间短暂不可读取时仍要显示结果。 */
      readonly orgId: string;
      readonly spaceId: string;
    };

const SEGMENTS = [
  { id: 'files' as const, label: '文件' },
  { id: 'trash' as const, label: '回收站' },
];

/**
 * 文件（5.8，OrgFS）：先选空间，再看目录与预览。按所选空间的状态决定可用操作，
 * 不把一种情况套到所有空间；导入导出用 CLI，本期不呈现。
 * 文件与文件夹都可以选中：文件看预览、编辑；文件夹看包含几项，可移动、改名、删除。
 */
export function FilesSection({ api }: { readonly api: WorkspaceApi }) {
  const { snapshot, detail, org } = api;
  const spaces = detail?.spaces ?? null;
  const requestedSpace = spaces?.find((space) => space.id === api.intent?.segment);
  const [spaceId, setSpaceId] = useState<string | null>(
    requestedSpace?.id ?? spaces?.[0]?.id ?? null,
  );
  const [segment, setSegment] = useState<Segment>('files');
  const [selected, setSelected] = useState<string | null>(null);
  const [dialog, setDialog] = useState<FileDialog | null>(null);
  const openDialog = (subject: string, method: OrgGuardKey, next: FileDialog): void => {
    if (api.guard(subject, method)) setDialog(next);
  };
  const space = spaces?.find((item) => item.id === spaceId) ?? null;

  function select(path: string | null): void {
    setSelected(path);
  }

  /**
   * 恢复结果要跨刷新可见：恢复对话框记住打开时的 org/space，刷新进入
   * loading/error 时仍渲染在同一位置，结果文字不会被卸载。
   */
  function renderOpenDialog() {
    if (!dialog) return null;
    if (dialog.kind === 'restore') {
      return renderDialog(dialog, api, dialog.orgId, dialog.spaceId, () => setDialog(null), select);
    }
    return org && space
      ? renderDialog(dialog, api, org.id, space.id, () => setDialog(null), select)
      : null;
  }

  if (snapshot.status === 'loading' || snapshot.status === 'error') {
    return (
      <div className="org-section" data-org-section="files">
        <SectionHeader title="文件" />
        {renderOpenDialog()}
        <OrgReadStateBlock snapshot={snapshot} onRefresh={() => void api.refresh()} />
      </div>
    );
  }
  if (!org || spaces === null) {
    return (
      <div className="org-section" data-org-section="files">
        <SectionHeader title="文件" />
        {renderOpenDialog()}
        <UnknownBlock onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  const writeBlock = spaceWriteBlock(space);
  const files = space ? (detail?.files[space.id] ?? []) : [];
  const trash = space ? (detail?.trash[space.id] ?? []) : [];
  const entry = files.find((item) => item.path === selected) ?? null;
  const file = entry?.kind === 'file' ? entry : null;
  const folder = entry?.kind === 'folder' ? entry : null;
  const childCount = folder ? files.filter((item) => item.path.startsWith(`${folder.path}/`)).length : 0;
  const filesFailed = detail?.filesStatus === 'error';
  const trashFailed = detail?.trashStatus === 'error';
  const listWriteBlock = filesFailed
    ? '文件列表读取失败，暂不能判断是否为空；请刷新后重试。'
    : null;
  const effectiveWriteBlock = listWriteBlock ?? writeBlock;

  return (
    <div className="org-section" data-org-section="files" data-dev-ui="DEV-UI-ORG-012">
      <SectionHeader
        title="文件"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      {renderOpenDialog()}
      <Notice tone="neutral">{ORG_COPY.filesSecretHint}</Notice>
      <div className="org-toolbar">
        <div className="org-inline-field">
          <label htmlFor="org-files-space">空间</label>
          <Select
            id="org-files-space"
            className="org-select-inline"
            value={space?.id ?? ''}
            options={spaces.map((item) => ({ value: item.id, label: item.name }))}
            onValueChange={(next) => {
              setSpaceId(next);
              select(null);
            }}
          />
        </div>
        <TabList
          items={SEGMENTS}
          value={segment}
          onChange={setSegment}
          label="文件分组"
          idPrefix="org-files"
          variant="segments"
        />
      </div>

      {space?.health === 'unknown' ? (
        <UnknownBlock detail={ORG_COPY.spaceUnknownReason} onRefresh={() => void api.refresh()} />
      ) : null}
      {space?.health === 'history-anomaly' ? (
        <Notice tone="warning" role="alert">
          {ORG_COPY.historyAnomaly}
        </Notice>
      ) : null}

      <TabPanel idPrefix="org-files" value={segment}>
        {segment === 'files' ? (
          <div className="org-master-detail">
            <div className="org-master">
              <div className="org-toolbar">
                <Button
                  size="sm"
                  disabled={effectiveWriteBlock !== null}
                  title={effectiveWriteBlock ?? undefined}
                  onClick={() => openDialog('新建', 'createEntry', { kind: 'new', parent: folder?.path ?? null })}
                >
                  新建
                </Button>
                {effectiveWriteBlock ? <span className="org-muted">{effectiveWriteBlock}</span> : null}
              </div>
              {filesFailed ? (
                <Notice tone="warning" role="alert">
                  文件列表读取失败，无法判断是否为空；刷新后重试。
                </Notice>
              ) : files.length === 0 ? (
                <p className="org-empty-inline">这个空间还没有文件。</p>
              ) : (
                <ul className="org-tree" aria-label="目录">
                  {files.map((item) => (
                    <li
                      key={item.path}
                      className="org-tree__row"
                      style={{ paddingInlineStart: `${(item.path.split('/').length - 1) * 16}px` }}
                    >
                      <button
                        type="button"
                        className={item.kind === 'folder' ? 'org-tree__folder' : 'org-tree__file'}
                        aria-pressed={item.path === selected}
                        onClick={() => select(item.path)}
                      >
                        {item.kind === 'folder' ? (
                          <>
                            <span className="ui-visually-hidden">文件夹 </span>
                            {item.name}/
                          </>
                        ) : (
                          item.name
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {file ? (
                <section className="org-preview" aria-label={zh`预览 ${file.name}`}>
                  <p className="org-row__meta">
                    {file.path} ·{' '}
                    <MetaParts parts={[`最后修改 ${file.modifiedBy}`, file.modifiedAt, `版本 ${file.version}`]} />
                  </p>
                  <pre className="org-preview__content">{file.content ?? ''}</pre>
                  <div className="org-actions">
                    <Button
                      size="sm"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() => openDialog('编辑', 'saveFile', { kind: 'edit', entry: file })}
                    >
                      编辑
                    </Button>
                    <Button
                      size="sm"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() => openDialog('移动', 'moveEntry', { kind: 'move', entry: file })}
                    >
                      移动
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() => openDialog('删除', 'deleteEntry', { kind: 'delete', entry: file, childCount: 0 })}
                    >
                      删除
                    </Button>
                  </div>
                </section>
              ) : null}
              {folder ? (
                <section className="org-preview" aria-label={zh`文件夹 ${folder.name}`}>
                  <p className="org-row__meta">
                    {folder.path}/ ·{' '}
                    <MetaParts parts={[`最后修改 ${folder.modifiedBy}`, folder.modifiedAt, `包含 ${childCount} 项`]} />
                  </p>
                  <div className="org-actions">
                    <Button
                      size="sm"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() => openDialog('移动', 'moveEntry', { kind: 'move', entry: folder })}
                    >
                      移动
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() => openDialog('删除', 'deleteEntry', { kind: 'delete', entry: folder, childCount })}
                    >
                      删除
                    </Button>
                  </div>
                </section>
              ) : null}
            </div>
          </div>
        ) : (
          <>
            {trashFailed ? (
              <Notice tone="warning" role="alert">
                回收站读取失败，无法判断是否为空；刷新后重试。
              </Notice>
            ) : trash.length === 0 ? (
              <p className="org-empty-inline">回收站是空的。</p>
            ) : (
              <ul className="org-rows" aria-label="回收站">
                {trash.map((item) => (
                  <li key={item.id} className="org-row">
                    <div className="org-row__main org-row__main--static">
                      <span className="org-row__title">{item.kind === 'folder' ? `${item.path}/` : item.path}</span>
                      <span className="org-row__meta">
                        <MetaParts
                          parts={[
                            `删除人 ${item.deletedBy}`,
                            item.deletedAt,
                            ...(item.kind === 'folder' ? [`含 ${item.childCount ?? 0} 项`] : []),
                          ]}
                        />
                      </span>
                    </div>
                    <Button
                      size="sm"
                      disabled={effectiveWriteBlock !== null}
                      title={effectiveWriteBlock ?? undefined}
                      onClick={() =>
                        openDialog('恢复', 'restoreEntry', { kind: 'restore', entry: item, orgId: org.id, spaceId: space?.id ?? '' })
                      }
                    >
                      恢复
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </TabPanel>
    </div>
  );
}

function renderDialog(
  dialog: FileDialog,
  api: WorkspaceApi,
  orgId: string,
  spaceId: string,
  onClose: () => void,
  onSelect: (path: string | null) => void,
) {
  const common = { api, orgId, spaceId, onClose };
  switch (dialog.kind) {
    case 'edit':
      return <FileEditorDialog {...common} entry={dialog.entry} />;
    case 'new':
      return (
        <NewEntryDialog {...common} initialPath={dialog.parent ? `${dialog.parent}/` : ''} onCreated={onSelect} />
      );
    case 'move':
      return <MoveEntryDialog {...common} entry={dialog.entry} onMoved={onSelect} />;
    case 'delete':
      return (
        <DeleteEntryDialog
          {...common}
          entry={dialog.entry}
          childCount={dialog.childCount}
          onDeleted={() => onSelect(null)}
        />
      );
    case 'restore':
      return <RestoreDialog {...common} entry={dialog.entry} />;
  }
}
