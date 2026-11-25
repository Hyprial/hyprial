import { useState } from 'react';
import { usePageAvailable } from '../../../../app/page-availability';
import { Button, Icon } from '../../../../components/primitives';
import type { MachineSectionModel } from '../../../network';
import { ORG_COPY, ORG_ROLE_LABELS, SYNC_LABELS, zh } from '../domain/copy';
import type { OrgPerson } from '../domain/types';
import { RemoveMemberDialog } from '../dialogs/RemoveMemberDialog';
import { MachinesSection } from './MachinesSection';
import type { WorkspaceApi } from '../workspace/api';
import {
  MetaParts,
  OrgReadStateBlock,
  SectionHeader,
  SidePanel,
  TabList,
  TabPanel,
  UnknownBlock,
  type TabItem,
} from '../workspace/controls';

type Segment = 'people' | 'machines';

/** 成员（5.3）：人｜设备；点击打开侧栏（L2）。 */
export function MembersSection({ api, fallback }: { readonly api: WorkspaceApi; readonly fallback?: MachineSectionModel }) {
  const { org, detail, perms } = api;
  const segments: TabItem<Segment>[] = [
    { id: 'people', label: '人' },
    { id: 'machines', label: '设备' },
  ];
  const initial = segments.some((item) => item.id === api.intent?.segment)
    ? (api.intent?.segment as Segment)
    : 'people';
  const [segment, setSegment] = useState<Segment>(initial);
  const [person, setPerson] = useState<OrgPerson | null>(null);
  const [removing, setRemoving] = useState<OrgPerson | null>(null);
  const chatAvailable = usePageAvailable('chat');

  if (!org) {
    return (
      <OrgReadStateBlock
        snapshot={api.snapshot}
        onRefresh={() => void api.refresh()}
        detail="选择或加入一个组织后，这里显示成员。"
      />
    );
  }

  const people = detail?.people ?? null;
  const agents = detail?.agents ?? null;
  const machineName = (id: string) =>
    detail?.machines?.find((machine) => machine.id === id)?.hostname ?? id;
  const agentName = (id: string) => agents?.find((item) => item.id === id)?.name ?? id;

  function openChat(chatTargetId: string | undefined): void {
    // 只是导航：先切到聊天页；有对应会话时再选中它，不新增后端能力。
    api.dispatch({ type: 'navigate', page: 'chat' });
    if (chatTargetId) {
      api.dispatch({ type: 'open-chat', targetId: chatTargetId });
    }
  }

  return (
    <div className="org-section" data-org-section="members">
      <SectionHeader
        title="成员"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <TabList
        items={segments}
        value={segment}
        onChange={(next) => {
          setSegment(next);
          setPerson(null);
        }}
        label="成员分组"
        idPrefix="org-members"
        variant="segments"
      />
      <TabPanel idPrefix="org-members" value={segment}>
        <div className={`org-master-detail${person ? ' has-panel' : ''}`}>
          <div className="org-master">
            {segment === 'people' ? (
              people === null ? (
                <UnknownBlock onRefresh={() => void api.refresh()} />
              ) : (
                <>
                  {people.length > 0 ? (
                  <ul className="org-rows" aria-label="组织成员">
                    {people.map((item) => (
                      <li key={item.id} className="org-row" data-org-person={item.id}>
                        <button
                          type="button"
                          className="org-row__main"
                          aria-label={zh`查看成员 ${item.displayName}`}
                          aria-current={person?.id === item.id ? 'true' : undefined}
                          onClick={() => {
                            setPerson(item);
                          }}
                        >
                          <span className="org-row__title">
                            {item.displayName}
                            {item.isMe ? <span className="org-tag">我</span> : null}
                          </span>
                          <span className="org-row__meta">
                            {item.account} · <MetaParts parts={[ORG_ROLE_LABELS[item.role], `来源 ${item.source}`]} />
                          </span>
                        </button>
                        <span className={`org-status org-status--${item.sync}`}>
                          {SYNC_LABELS[item.sync]}
                        </span>
                      </li>
                    ))}
                  </ul>
                  ) : null}
                  {people.every((item) => item.isMe) ? (
                    <div className="org-empty-inline">
                      <p>{ORG_COPY.noOtherMembers}</p>
                      {perms.invite ? (
                        <Button
                          size="sm"
                          onClick={() => api.goTo('invites', { segment: 'send' })}
                          leadingIcon={<Icon name="plus" size="sm" />}>
                          {ORG_COPY.inviteMember}
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                </>
              )
            ) : null}

            {segment === 'machines' ? <MachinesSection api={api} heading="设备" showRefresh={false} fallback={fallback} /> : null}
          </div>

          {person ? (
            <SidePanel parentLabel="成员" title={person.displayName} onClose={() => setPerson(null)}>
              <dl className="org-facts">
                <div>
                  <dt>账号</dt>
                  <dd>{person.account}</dd>
                </div>
                <div>
                  <dt>角色</dt>
                  <dd>{ORG_ROLE_LABELS[person.role]}</dd>
                </div>
                <div>
                  <dt>来源</dt>
                  <dd>{person.source}</dd>
                </div>
                <div>
                  <dt>同步状态</dt>
                  <dd>{SYNC_LABELS[person.sync]}</dd>
                </div>
                <div>
                  <dt>在本组织的机器</dt>
                  <dd>
                    {person.machineIds.length > 0
                      ? person.machineIds.map(machineName).join('、')
                      : '没有机器记录'}
                  </dd>
                </div>
                <div>
                  <dt>在本组织的 Agent</dt>
                  <dd>
                    {agents === null
                      ? ORG_COPY.unknown
                      : person.agentIds.length > 0
                        ? person.agentIds.map(agentName).join('、')
                        : '没有 Agent'}
                  </dd>
                </div>
              </dl>
              {!person.isMe && (chatAvailable || perms.removeMember) ? (
              <div className="org-actions">
                {chatAvailable && !person.isMe ? (
                  <Button onClick={() => openChat(person.chatTargetId)}>
                    {ORG_COPY.sendMessage}
                  </Button>
                ) : null}
                {perms.removeMember && !person.isMe ? (
                  <Button variant="danger" onClick={() => {
                      if (api.guard(ORG_COPY.removeMember, 'previewRemoveMember', 'removeMember')) setRemoving(person);
                    }} leadingIcon={<Icon name="trash" size="sm" />}>
                    {ORG_COPY.removeMember}
                  </Button>
                ) : null}
              </div>
              ) : null}
            </SidePanel>
          ) : null}

        </div>
      </TabPanel>

      {removing ? (
        <RemoveMemberDialog
          api={api}
          orgId={org.id}
          person={removing}
          onClose={() => setRemoving(null)}
        />
      ) : null}
    </div>
  );
}
