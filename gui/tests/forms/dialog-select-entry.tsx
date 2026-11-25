// C1/C5 浏览器探针 harness（PR #2579 测试切片）：真实 createRoot 挂载
// Dialog + Field + Select + Button 的受控表单，状态经 <output data-testid> 暴露给
// dialog-select-browser.mjs 读取；样式按交接文档 §5 加载 tokens.css 与 shadcn.css，
// primitives.css 经组件公共入口自带，不连接 daemon/账号。
import { StrictMode, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { Button, Dialog, Field, Select } from '../../src/components/primitives';
import '../../src/styles/tokens.css';
import '../../src/styles/shadcn.css';

const HARNESS_OPTIONS = [
  { value: 'claude', label: 'claude' },
  { value: 'codex', label: 'codex' },
];

function Harness() {
  const [open, setOpen] = useState(false);
  const [harness, setHarness] = useState('claude');
  const [name, setName] = useState('alpha');
  const [closeCalls, setCloseCalls] = useState(0);
  const [changeCalls, setChangeCalls] = useState(0);
  const [lastChange, setLastChange] = useState('(none)');

  return (
    <main style={{ padding: 24, fontFamily: 'var(--ui-font-ui, sans-serif)' }}>
      <Button data-contract="open-button" onClick={() => setOpen(true)}>
        打开表单
      </Button>
      <p>
        <output data-testid="value">{harness}</output>
        {' / change '}
        <output data-testid="change-calls">{changeCalls}</output>
        {' / last '}
        <output data-testid="last-change">{lastChange}</output>
        {' / close '}
        <output data-testid="close-calls">{closeCalls}</output>
      </p>
      <Dialog
        open={open}
        title="接入设置"
        description="调整默认 harness 与展示名称"
        onClose={() => {
          setCloseCalls((count) => count + 1);
          setOpen(false);
        }}
        actions={[
          { label: '保存', variant: 'primary', onClick: () => {} },
        ]}
      >
        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
          }}
        >
          <Field label="名称" htmlFor="contract-name">
            <input
              id="contract-name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </Field>
          <div>
            <label htmlFor="contract-harness">harness</label>
            <Select
              id="contract-harness"
              value={harness}
              options={HARNESS_OPTIONS}
              onValueChange={(next) => {
                setChangeCalls((count) => count + 1);
                setLastChange(next);
                setHarness(next);
              }}
            />
          </div>
        </form>
      </Dialog>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
