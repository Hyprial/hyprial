import { useState } from 'react';
import { Button } from '../../../components/primitives';
import { ORG_COPY, ORG_ROLE_LABELS, SYNC_LABELS, zh } from '../domain/copy';
import { orgDisplayName } from '../domain/selectors';
import type { OrgAgentRecord, OrgPerson } from '../domain/types';
import { RemoveMemberDialog } from '../dialogs/RemoveMemberDialog';
import type { WorkspaceApi } from '../workspace/api';
import {
  MetaParts,
  SectionHeader,
  SidePanel,
  TabList,
  TabPanel,
  UnavailableBlock,
  UnknownBlock,
  type TabItem,
} from '../workspace/controls';

type Segment = 'people' | 'agents' | 'approvals';

const RUN_LABELS: Readonly<Record<OrgAgentRecord['runState'], string>> = {
  running: '运行中',
  stopped: '已停止',
  unknown: ORG_COPY.unknown,
};

/** 成员（5.3）：人｜Agent｜待审批；点击打开侧栏（L2）。 */
export function MembersSection({ api }: { readonly api: WorkspaceApi }) {
  const { org, detail, perms } = api;
  const segments: TabItem<Segment>[] = [
    { id: 'people', label: '人' },
    { id: 'agents', label: 'Agent' },
  ];
  if (perms.approve) {
    segments.push({ id: 'approvals', label: '待审批' });
  }
  const initial = segments.some((item) => item.id === api.intent?.segment)
    ? (api.intent?.segment as Segment)
    : 'people';
  const [segment, setSegment] = useState<Segment>(initial);
  const [person, setPerson] = useState<OrgPerson | null>(null);
  const [agent, setAgent] = useState<OrgAgentRecord | null>(null);
  const [removing, setRemoving] = useState<OrgPerson | null>(null);

  if (!org) {
    return <UnknownBlock detail="选择或加入一个组织后，这里显示成员。" />;
  }

  const people = detail?.people ?? null;
  const agents = detail?.agents ?? null;
  const machineName = (id: string) =>
    detail?.machines?.find((machine) => machine.id === id)?.hostname ?? id;
  const agentName = (id: string) => agents?.find((item) => item.id === id)?.name ?? id;
  const orgName = (id: string) => {
    const found = api.snapshot.orgs?.find((item) => item.id === id);
    return found ? orgDisplayName(found) : ORG_COPY.orgUnknown;
  };

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
          setAgent(null);
        }}
        label="成员分组"
        idPrefix="org-members"
        variant="segments"
      />
      <TabPanel idPrefix="org-members" value={segment}>
        <div className={`org-master-detail${person || agent ? ' has-panel' : ''}`}>
          <div className="org-master">
            {segment === 'people' ? (
              people === null ? (
                <UnknownBlock onRefresh={() => void api.refresh()} />
              ) : (
                <>
                  <ul className="org-rows" aria-label="组织成员">
                    {people.map((item) => (
                      <li key={item.id} className="org-row" data-org-person={item.id}>
                        <button
                          type="button"
                          className="org-row__main"
                          aria-label={zh`查看成员 ${item.displayName}`}
                          aria-current={person?.id === item.id ? 'true' : undefined}
                          onClick={() => {
                            setAgent(null);
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
                  {people.every((item) => item.isMe) ? (
                    <div className="org-empty-inline">
                      <p>{ORG_COPY.noOtherMembers}</p>
                      {perms.invite ? (
                        <Button
                          size="sm"
                          onClick={() => api.goTo('invites', { segment: 'send' })}
                        >
                          {ORG_COPY.inviteMember}
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                </>
              )
            ) : null}

            {segment === 'agents' ? (
              <>
                <div className="org-toolbar">
                  <Button
                    size="sm"
                    onClick={() => {
                      api.requestAgentView({ createInOrgId: org.id });
                      api.dispatch({ type: 'open-agent-create' });
                    }}
                  >
                    {ORG_COPY.newAgentHereShort}
                  </Button>
                </div>
                {agents === null ? (
                  <UnknownBlock onRefresh={() => void api.refresh()} />
                ) : agents.length === 0 ? (
                  <p className="org-empty-inline">{ORG_COPY.noAgents}</p>
                ) : (
                  <ul className="org-rows" aria-label="组织内的 Agent">
                    {agents.map((item) => (
                      <li key={item.id} className="org-row" data-org-agent={item.id}>
                        <button
                          type="button"
                          className="org-row__main"
                          aria-label={zh`查看 Agent ${item.name}`}
                          aria-current={agent?.id === item.id ? 'true' : undefined}
                          onClick={() => {
                            setPerson(null);
                            setAgent(item);
                          }}
                        >
                          <span className="org-row__title">{item.name}</span>
                          <span className="org-row__meta">
                            <MetaParts parts={[`主人 ${item.ownerLabel}`, `机器 ${item.machineLabel}`]} />
                          </span>
                        </button>
                        <span className={`org-status org-status--${item.runState}`}>
                          {RUN_LABELS[item.runState]}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : null}

            {segment === 'approvals' ? (
              <UnavailableBlock text={ORG_COPY.approvalsUnavailable} />
            ) : null}
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
                    {person.agentIds.length > 0
                      ? person.agentIds.map(agentName).join('、')
                      : '没有 Agent'}
                  </dd>
                </div>
              </dl>
              <div className="org-actions">
                {!person.isMe ? (
                  <Button onClick={() => openChat(person.chatTargetId)}>
                    {ORG_COPY.sendMessage}
                  </Button>
                ) : null}
                {perms.removeMember && !person.isMe ? (
                  <Button variant="danger" onClick={() => setRemoving(person)}>
                    {ORG_COPY.removeMember}
                  </Button>
                ) : null}
              </div>
            </SidePanel>
          ) : null}

          {agent ? (
            <SidePanel parentLabel="成员" title={agent.name} onClose={() => setAgent(null)}>
              <dl className="org-facts">
                <div>
                  <dt>主人</dt>
                  <dd>{agent.ownerLabel}</dd>
                </div>
                <div>
                  <dt>所在机器</dt>
                  <dd>{agent.machineLabel}</dd>
                </div>
                <div>
                  <dt>运行状态</dt>
                  <dd>{RUN_LABELS[agent.runState]}</dd>
                </div>
                <div>
                  <dt>所属组织</dt>
                  <dd>{agent.orgIds.map(orgName).join('、') || ORG_COPY.orgUnknown}</dd>
                </div>
              </dl>
              <div className="org-actions">
                <Button onClick={() => openChat(agent.chatTargetId)}>
                  {ORG_COPY.sendMessage}
                </Button>
                {agent.mine ? (
                  <Button
                    onClick={() => {
                      api.requestAgentView({ agentId: agent.id, section: 'orgs' });
                      api.dispatch({ type: 'open-agent-detail', agentId: agent.id });
                    }}
                  >
                    {ORG_COPY.openInAgentPage}
                  </Button>
                ) : null}
              </div>
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
