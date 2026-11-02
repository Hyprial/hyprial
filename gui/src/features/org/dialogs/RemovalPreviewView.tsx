import { Stepper, type StepDefinition } from '../../../components/primitives';
import type { RemovalPreview } from '../domain/types';

/**
 * 移除成员与离开组织共用的预览：受影响的设备、tailnet、authority、断连影响。
 * 预览变化后重新读取时传入上一次的预览，新出现的设备单独标出，便于核对。
 */
export function RemovalPreviewView({
  preview,
  previous,
}: {
  readonly preview: RemovalPreview;
  readonly previous?: RemovalPreview | null;
}) {
  const isNew = (device: string) => Boolean(previous) && !previous?.devices.includes(device);
  return (
    <dl className="org-facts">
      <div>
        <dt>受影响的设备</dt>
        <dd>
          {preview.devices.length > 0 ? (
            <ul className="org-device-list">
              {preview.devices.map((device) => (
                <li key={device}>
                  {device}
                  {isNew(device) ? <span className="org-tag org-tag--warning">新出现</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            '没有设备'
          )}
        </dd>
      </div>
      <div>
        <dt>tailnet</dt>
        <dd>{preview.tailnet}</dd>
      </div>
      <div>
        <dt>authority 设备</dt>
        <dd>{preview.authority}</dd>
      </div>
      <div>
        <dt>断连影响</dt>
        <dd>{preview.impact}</dd>
      </div>
      <div>
        <dt>预览版本</dt>
        <dd>{preview.revision}</dd>
      </div>
    </dl>
  );
}

const STAGES = ['已确认', '待 authority 或 OrgFS 处理', '待 tailnet 处理', '完成'] as const;

/** 提交后的阶段：只标记已确认；后续阶段以后端事件或刷新为准。 */
export function RemovalStages() {
  const steps: StepDefinition[] = STAGES.map((label, index) => ({
    id: `stage-${index}`,
    label,
    status: index === 0 ? 'completed' : index === 1 ? 'active' : 'pending',
  }));
  return <Stepper steps={steps} currentId="stage-1" ariaLabel="处理阶段" className="org-stepper org-stepper--rows" />;
}
