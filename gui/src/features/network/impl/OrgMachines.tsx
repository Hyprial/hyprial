import { Badge } from '../../../components/Badge';
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
      className="org-machines"
      aria-labelledby="org-machines-title"
      data-machine-scene={model.scene}
    >
      <h2 id="org-machines-title" className="org-network-title">{model.title}</h2>

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
                  <Badge tone="neutral">{model.currentLabel}</Badge>
                ) : null}
                <Badge tone={machine.connection === 'online' ? 'positive' : 'muted'}>
                  {MACHINE_CONNECTION_LABELS[machine.connection]}
                </Badge>
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
