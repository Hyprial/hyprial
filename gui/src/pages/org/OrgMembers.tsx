import type { OrgMember } from './org-view-model';

export function OrgMembers({
  members,
  emptyText,
}: {
  readonly members: readonly OrgMember[];
  readonly emptyText: string | null;
}) {
  return (
    <section className="card org-block" aria-labelledby="org-members">
      <div className="section-heading">
        <div>
          <h2 id="org-members">成员</h2>
          <p>成员加入后显示在这里。</p>
        </div>
      </div>
      {members.length === 0 ? (
        emptyText ? (
          <p className="copy org-members-empty">{emptyText}</p>
        ) : null
      ) : (
        <ul className="org-members" aria-label="成员列表">
          {members.map((member) => (
            <li className="org-member-row" data-org-member={member.id} key={member.id}>
              <span className="org-member-name">{member.name}</span>
              <span className="badge badge-positive">
                {member.status === 'joined' ? '已加入' : '未知'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
