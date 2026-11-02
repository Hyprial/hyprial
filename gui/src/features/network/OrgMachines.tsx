import {
  MACHINE_CONNECTION_LABELS,
  type MachineSectionModel,
} from './machine-view-model';
import './machines.css';

export function OrgMachines({ model }: { readonly model: MachineSectionModel }) {
  if (model.scene === 'hidden') {
    return null;
  }

  return (
    <section
      className="card org-machines"
      aria-labelledby="org-machines-title"
      data-machine-scene={model.scene}
    >
      <div className="section-heading">
        <div>
          <h2 id="org-machines-title">{model.title}</h2>
          <p>加入组织后的机器显示在这里。</p>
        </div>
      </div>

      {model.unavailableNote ? (
        <p className="org-machines-note" role="note">
          {model.unavailableNote}
        </p>
      ) : null}

      {model.machines.length > 0 ? (
        <ul className="org-machines-list" aria-label="机器列表">
          {model.machines.map((machine) => (
            <li
              className="org-machines-row"
              data-machine-id={machine.id}
              key={machine.id}
            >
              <div className="org-machines-main">
                <span className="org-machines-hostname">{machine.hostname}</span>
                {machine.current ? (
                  <span className="badge badge-neutral">{model.currentLabel}</span>
                ) : null}
                <span
                  className={
                    machine.connection === 'online'
                      ? 'badge badge-positive'
                      : 'badge badge-muted'
                  }
                >
                  {MACHINE_CONNECTION_LABELS[machine.connection]}
                </span>
              </div>
              <p className="org-machines-meta">
                {machine.system} · 加入于 {machine.joinedAt}
              </p>
            </li>
          ))}
        </ul>
      ) : null}

      {model.emptyText ? (
        <p className="org-machines-empty">{model.emptyText}</p>
      ) : null}
    </section>
  );
}
