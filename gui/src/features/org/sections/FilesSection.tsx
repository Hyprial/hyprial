import { useState } from 'react';
import { Button, Notice, Select } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import { spaceWriteBlock } from '../domain/selectors';
import type { OrgFileEntry, TrashEntry } from '../domain/types';
import {
  DeleteEntryDialog,
  FileEditorDialog,
  MoveEntryDialog,
  NewEntryDialog,
  PurgeDialog,
  RestoreDialog,
} from '../dialogs/FileDialogs';
import type { WorkspaceApi } from '../workspace/api';
import {
  MetaParts,
  SectionHeader,
  SidePanel,
  TabList,
  TabPanel,
  UnavailableBlock,
  UnknownBlock,
} from '../workspace/controls';

type Segment = 'files' | 'trash';
type FileDialog =
  | { readonly kind: 'edit'; readonly entry: OrgFileEntry }
  | { readonly kind: 'new'; readonly parent: string | null }
  | { readonly kind: 'move'; readonly entry: OrgFileEntry }
  | { readonly kind: 'delete'; readonly entry: OrgFileEntry; readonly childCount: number }
  | { readonly kind: 'restore'; readonly entry: TrashEntry }
  | { readonly kind: 'purge'; readonly entry: TrashEntry };

const SEGMENTS = [
  { id: 'files' as const, label: '文件' },
  { id: 'trash' as const, label: '回收站' },
];

/**
 * 文件（5.8，OrgFS）：先选空间，再看目录与预览。按所选空间的状态决定可用操作，
 * 不把一种情况套到所有空间；导入导出用 CLI，本期不呈现。
 * 文件与文件夹都可以选中：文件看预览、编辑、历史；文件夹看包含几项，可移动、改名、删除。
 */
export function FilesSection({ api }: { readonly api: WorkspaceApi }) {
  const { snapshot, detail, org, perms } = api;
  const spaces = detail?.spaces ?? null;
  const requestedSpace = spaces?.find((space) => space.id === api.intent?.segment);
  const [spaceId, setSpaceId] = useState<string | null>(
    requestedSpace?.id ?? spaces?.[0]?.id ?? null,
  );
  const [segment, setSegment] = useState<Segment>('files');
  const [selected, setSelected] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // 历史里选中的版本，绑定“空间:路径”；换了文件或空间就不再显示。
  const [historyPick, setHistoryPick] = useState<{ readonly key: string; readonly version: number } | null>(
    null,
  );
  const [dialog, setDialog] = useState<FileDialog | null>(null);

  function closeHistory(): void {
    setHistoryOpen(false);
    setHistoryPick(null);
  }

  function select(path: string | null): void {
    setSelected(path);
    closeHistory();
  }

  if (snapshot.status === 'unwired') {
    return (
      <div className="org-section" data-org-section="files">
        <SectionHeader title="文件" />
        <UnavailableBlock text={ORG_COPY.filesUnavailable} />
      </div>
    );
  }
  if (!org || spaces === null) {
    return (
      <div className="org-section" data-org-section="files">
        <SectionHeader title="文件" />
        <UnknownBlock onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  const space = spaces.find((item) => item.id === spaceId) ?? null;
  const writeBlock = spaceWriteBlock(space);
  const historyBlocked = !space || space.health !== 'healthy';
  const files = space ? (detail?.files[space.id] ?? []) : [];
  const trash = space ? (detail?.trash[space.id] ?? []) : [];
  const entry = files.find((item) => item.path === selected) ?? null;
  const file = entry?.kind === 'file' ? entry : null;
  const folder = entry?.kind === 'folder' ? entry : null;
  const childCount = folder ? files.filter((item) => item.path.startsWith(`${folder.path}/`)).length : 0;
  const historyKey = file && space ? `${space.id}:${file.path}` : null;
  const versions = historyKey ? (detail?.history[historyKey] ?? []) : [];
  const picked =
    historyPick && historyPick.key === historyKey
      ? (versions.find((version) => version.version === historyPick.version) ?? null)
      : null;

  return (
    <div className="org-section" data-org-section="files" data-dev-ui="DEV-UI-ORG-012">
      <SectionHeader
        title="文件"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
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
          onChange={(next) => {
            setSegment(next);
            closeHistory();
          }}
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
          <div className={`org-master-detail${historyOpen ? ' has-panel' : ''}`}>
            <div className="org-master">
              <div className="org-toolbar">
                <Button
                  size="sm"
                  disabled={writeBlock !== null}
                  title={writeBlock ?? undefined}
                  onClick={() => setDialog({ kind: 'new', parent: folder?.path ?? null })}
                >
                  新建
                </Button>
                {writeBlock ? <span className="org-muted">{writeBlock}</span> : null}
              </div>
              {files.length === 0 ? (
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
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'edit', entry: file })}
                    >
                      编辑
                    </Button>
                    <Button
                      size="sm"
                      disabled={historyBlocked}
                      title={historyBlocked ? ORG_COPY.historyAnomalyReason : undefined}
                      onClick={() => {
                        setHistoryPick(null);
                        setHistoryOpen(true);
                      }}
                    >
                      历史
                    </Button>
                    <Button
                      size="sm"
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'move', entry: file })}
                    >
                      移动
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'delete', entry: file, childCount: 0 })}
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
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'move', entry: folder })}
                    >
                      移动
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'delete', entry: folder, childCount })}
                    >
                      删除
                    </Button>
                  </div>
                </section>
              ) : null}
            </div>
            {historyOpen && file ? (
              <SidePanel parentLabel="文件" title={`历史：${file.name}`} onClose={closeHistory}>
                {versions.length === 0 ? (
                  <p className="org-empty-inline">没有版本记录。</p>
                ) : (
                  <ul className="org-rows" aria-label="版本记录">
                    {versions.map((version) => (
                      <li key={version.version} className="org-row">
                        <button
                          type="button"
                          className="org-row__main"
                          aria-pressed={picked?.version === version.version}
                          onClick={() => historyKey && setHistoryPick({ key: historyKey, version: version.version })}
                        >
                          <span className="org-row__title">版本 {version.version}</span>
                          <span className="org-row__meta">
                            <MetaParts parts={[version.modifiedBy, version.modifiedAt]} />
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {picked ? (
                  <pre className="org-preview__content" aria-label={`版本 ${picked.version} 的内容`}>
                    {picked.content}
                  </pre>
                ) : null}
              </SidePanel>
            ) : null}
          </div>
        ) : (
          <>
            {trash.length === 0 ? (
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
                      disabled={writeBlock !== null}
                      title={writeBlock ?? undefined}
                      onClick={() => setDialog({ kind: 'restore', entry: item })}
                    >
                      恢复
                    </Button>
                    {perms.purge ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="org-text-danger"
                        disabled={writeBlock !== null}
                        onClick={() => setDialog({ kind: 'purge', entry: item })}
                      >
                        彻底清除
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </TabPanel>

      {dialog && space ? renderDialog(dialog, api, org.id, space.id, () => setDialog(null), select) : null}
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
    case 'purge':
      return <PurgeDialog {...common} entry={dialog.entry} />;
  }
}
