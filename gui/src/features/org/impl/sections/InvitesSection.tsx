import { useRef, useState } from 'react';
import { Button, Field, Icon, Notice, Stepper, type StepDefinition } from '../../../../components/primitives';
import { JOIN_FAILURES, ORG_COPY, zh } from '../domain/copy';
import { pendingInvites } from '../domain/selectors';
import type { InviteResult, JoinStep, OrgSummary, ReceivedInvite } from '../domain/types';
import { validateAccount, validateInviteLink } from '../domain/validation';
import { ClaimInviteDialog } from '../dialogs/ClaimInviteDialog';
import { InviteResultDialog } from '../dialogs/InviteResultDialog';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import {
  MetaParts,
  OrgReadStateBlock,
  SectionHeader,
  TabList,
  TabPanel,
  focusSoon,
  useBusy,
  type TabItem,
} from '../workspace/controls';

type Segment = 'send' | 'received' | 'join';

const JOIN_STEPS: readonly JoinStep[] = ['connect', 'join', 'register', 'peers'];

/** 邀请（5.4）：发出邀请｜我收到的邀请｜用邀请链接加入。三类状态各用一套文案（11.1）。 */
export function InvitesSection({ api }: { readonly api: WorkspaceApi }) {
  const segments: TabItem<Segment>[] = [];
  if (api.perms.invite) {
    segments.push({ id: 'send', label: '发出邀请' });
  }
  if (api.gateway.claimInvite) {
    segments.push({ id: 'received', label: '我收到的邀请', badge: pendingInvites(api.snapshot)?.length || undefined });
  }
  segments.push({ id: 'join', label: ORG_COPY.joinByLink });
  const requested = api.intent?.segment;
  const initial = segments.some((item) => item.id === requested)
    ? (requested as Segment)
    : segments[0].id;
  const [segment, setSegment] = useState<Segment>(initial);

  return (
    <div className="org-section" data-org-section="invites">
      <SectionHeader title="邀请" />
      {api.org && !api.perms.invite ? (
        <p className="org-muted">{ORG_COPY.inviteOwnerOnly}</p>
      ) : null}
      <TabList
        items={segments}
        value={segment}
        onChange={setSegment}
        label="邀请分组"
        idPrefix="org-invites"
        variant="segments"
        narrowLabel="邀请分组"
      />
      <TabPanel idPrefix="org-invites" value={segment}>
        {segment === 'send' ? <SendInvite api={api} /> : null}
        {segment === 'received' ? <ReceivedInvites api={api} /> : null}
        {segment === 'join' ? <JoinByLink api={api} /> : null}
      </TabPanel>
    </div>
  );
}

function SendInvite({ api }: { readonly api: WorkspaceApi }) {
  const [account, setAccount] = useState(api.intent?.prefillAccount ?? '');
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [result, setResult] = useState<InviteResult | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useBusy();
  const org = api.org;

  async function submit(): Promise<void> {
    if (!org) return;
    const invalid = validateAccount(account);
    setError(invalid);
    setFailure(null);
    if (invalid) {
      inputRef.current?.focus();
      return;
    }
    if (!api.guard(ORG_COPY.generateInvite, 'createInvite')) return;
    const outcome = await run(() => api.gateway.createInvite(org.id, account.trim()));
    if (!outcome || outcome.kind === 'unavailable') return;
    if (outcome.kind === 'ok') {
      setResult(outcome.value);
      return;
    }
    focusSoon(inputRef);
    setFailure(
      outcome.kind === 'failed' ? ORG_COPY.inviteFailed(outcome.message) : resultMessage(outcome),
    );
  }

  return (
    <form
      className="org-form"
      noValidate
      data-dev-ui="DEV-UI-ORG-005"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field
        label="成员账号"
        htmlFor="org-invite-account"
        hint={org ? zh`邀请加入 ${org.name}；对方加入后才会出现在成员列表中。` : undefined}
        error={error ?? undefined}
        required
      >
        <input
          id="org-invite-account"
          ref={inputRef}
          className="org-input"
          autoComplete="off"
          value={account}
          disabled={busy}
          onChange={(event) => setAccount(event.target.value)}
        />
      </Field>
      <div className="org-actions">
        <Button type="submit" variant="primary" loading={busy} leadingIcon={<Icon name="paper-plane-tilt" size="sm" />}>
          {busy ? ORG_COPY.generatingInvite : ORG_COPY.generateInvite}
        </Button>
      </div>
      {failure ? (
        <Notice tone="danger" role="alert">
          {failure}
        </Notice>
      ) : null}
      {result ? (
        <InviteResultDialog
          result={result}
          onClose={() => {
            // 关闭后链接不再显示，也不保存；需要时重新生成。
            setResult(null);
          }}
        />
      ) : null}
    </form>
  );
}

type ClaimState =
  | { readonly kind: 'pending' }
  | { readonly kind: 'joined'; readonly text: string }
  | { readonly kind: 'failed'; readonly text: string; readonly retry: boolean }
  | { readonly kind: 'skipped'; readonly text: string };

function ReceivedInvites({ api }: { readonly api: WorkspaceApi }) {
  const invites = api.snapshot.receivedInvites;
  const [confirming, setConfirming] = useState<ReceivedInvite | null>(null);
  const [states, setStates] = useState<Readonly<Record<string, ClaimState>>>({});
  // 后端处理后的邀请会从列表消失；已处理的行保留结果，直到刷新。
  const [handled, setHandled] = useState<readonly ReceivedInvite[]>([]);
  const [order] = useState<readonly string[]>(() => (invites ?? []).map((invite) => invite.id));

  const claimInvite = api.gateway.claimInvite;

  async function claim(invite: ReceivedInvite): Promise<void> {
    if (!claimInvite || !api.guard(ORG_COPY.join, 'claimInvite')) return;
    setHandled((prev) => (prev.some((item) => item.id === invite.id) ? prev : [...prev, invite]));
    setStates((prev) => ({ ...prev, [invite.id]: { kind: 'pending' } }));
    const result = await claimInvite(invite.id);
    if (result.kind === 'unavailable') {
      setHandled((prev) => prev.filter((item) => item.id !== invite.id));
      setStates((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => id !== invite.id)));
      return;
    }
    let next: ClaimState;
    if (result.kind === 'ok') {
      next = { kind: 'joined', text: ORG_COPY.joined(result.value.name) };
      api.askSetCurrent(result.value);
    } else if (result.kind === 'skipped') {
      next = { kind: 'skipped', text: ORG_COPY.skipped(result.message) };
    } else {
      // 过期、版本过旧等原因重试无效，只给下一步说明；未知原因可以重试。
      const known = result.kind === 'failed' && result.code ? JOIN_FAILURES[result.code] : undefined;
      next = {
        kind: 'failed',
        text: ORG_COPY.claimFailed(invite.orgName, resultMessage(result) ?? ORG_COPY.unknown),
        retry: known?.retry ?? true,
      };
    }
    setStates((prev) => ({ ...prev, [invite.id]: next }));
  }

  if (invites === null) {
    return (
      <OrgReadStateBlock
        snapshot={api.snapshot}
        onRefresh={() => void api.refresh()}
        detail="在网页上接受的邀请会列在这里。"
      />
    );
  }
  // 保持首次看到的顺序；刷新后新出现的邀请排在后面。
  const known = [...invites, ...handled];
  const rows = [
    ...order.flatMap((id) => known.find((item) => item.id === id) ?? []),
    ...invites.filter((invite) => !order.includes(invite.id)),
  ];
  if (rows.length === 0) {
    return <p className="org-empty-inline">{ORG_COPY.noReceivedInvites}</p>;
  }

  return (
    <>
      <p className="org-muted">曾主动离开的组织需用邀请链接重新加入。</p>
      <ul className="org-rows" aria-label="我收到的邀请">
        {rows.map((invite) => {
          const state: ClaimState | undefined =
            states[invite.id] ??
            (invite.skipReason ? { kind: 'skipped', text: ORG_COPY.skipped(invite.skipReason) } : undefined);
          return (
            <li key={invite.id} className="org-row" data-org-invite={invite.id}>
              <div className="org-row__main org-row__main--static">
                <span className="org-row__title">{invite.orgName}</span>
                <span className="org-row__meta">
                  <MetaParts parts={[`邀请人 ${invite.inviterLabel}`, `网页上已接受 ${invite.acceptedAt}`]} />
                </span>
                {state ? (
                  <span
                    className={`org-row__result org-row__result--${state.kind}`}
                    role={state.kind === 'failed' ? 'alert' : 'status'}
                  >
                    {state.kind === 'pending' ? ORG_COPY.claiming(invite.orgName) : state.text}
                  </span>
                ) : null}
              </div>
              {claimInvite && (!state || (state.kind === 'failed' && state.retry)) ? (
                <Button size="sm" variant="secondary" onClick={() => setConfirming(invite)}>
                  {state?.kind === 'failed' ? '重试' : ORG_COPY.join}
                </Button>
              ) : null}
            </li>
          );
        })}
      </ul>
      {confirming ? (
        <ClaimInviteDialog
          invite={confirming}
          onClose={() => setConfirming(null)}
          onConfirm={() => {
            const invite = confirming;
            setConfirming(null);
            void claim(invite);
          }}
        />
      ) : null}
    </>
  );
}

type JoinState =
  | { readonly kind: 'idle' }
  // 请求已提交、还没有任何服务端完成步骤；不能把按钮锁提前伪装成某一步完成。
  | { readonly kind: 'pending' }
  | { readonly kind: 'running'; readonly step: JoinStep }
  | { readonly kind: 'joined'; readonly org: OrgSummary }
  | {
      readonly kind: 'failed';
      readonly step: JoinStep;
      readonly reason: string;
      readonly next: string;
      readonly retry: boolean;
    }
  // 请求可能已生效（超时、中断或返回结果无法校验）；不展示失败步骤、不给重试。
  | { readonly kind: 'unknown'; readonly message: string };

function JoinByLink({ api }: { readonly api: WorkspaceApi }) {
  const [link, setLink] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<JoinState>({ kind: 'idle' });
  const inputRef = useRef<HTMLInputElement>(null);
  /** 同步防重入：state 更新前的连续点击/回车也只能发出一次 org.join。 */
  const inFlight = useRef(false);
  const busy = state.kind === 'pending' || state.kind === 'running';

  async function submit(): Promise<void> {
    if (inFlight.current) return;
    const invalid = validateInviteLink(link);
    setError(invalid);
    if (invalid) {
      inputRef.current?.focus();
      return;
    }
    if (!api.guard(ORG_COPY.join, 'joinByLink')) return;
    inFlight.current = true;
    const value = link.trim();
    // 邀请链接是凭据：提交后立即清空，不写入存储、URL 或日志。
    setLink('');
    setState({ kind: 'pending' });
    let lastStep: JoinStep = 'connect';
    try {
      const result = await api.gateway.joinByLink(value, (step) => {
        lastStep = step;
        setState({ kind: 'running', step });
      });
      if (result.kind === 'ok') {
        setState({ kind: 'joined', org: result.value });
        api.askSetCurrent(result.value);
        return;
      }
      if (result.kind === 'unavailable') {
        setLink(value);
        setState({ kind: 'idle' });
        return;
      }
      if (result.kind === 'failed' && result.unknown) {
        // 请求可能已经执行：不把默认的连接步骤标成失败，也不引导重试。
        setState({ kind: 'unknown', message: result.message });
        return;
      }
      const known =
        result.kind === 'failed' && result.code ? JOIN_FAILURES[result.code] : undefined;
      setState({
        kind: 'failed',
        step: lastStep,
        reason: known?.reason ?? resultMessage(result) ?? ORG_COPY.unknown,
        next: known?.next ?? '请检查邀请链接后重试',
        retry: known?.retry ?? true,
      });
    } catch {
      // 数据源本应把所有失败转成结果；若仍抛错，写请求已发出，按未知处理。
      setState({ kind: 'unknown', message: ORG_COPY.joinUnknown });
    } finally {
      inFlight.current = false;
    }
  }

  const stepIndex =
    state.kind === 'running' || state.kind === 'failed' ? JOIN_STEPS.indexOf(state.step) : -1;
  const steps: StepDefinition[] = JOIN_STEPS.map((step, index) => ({
    id: step,
    label: ORG_COPY.joinSteps[index],
    status:
      state.kind === 'joined'
        ? 'completed'
        : index < stepIndex
          ? 'completed'
          : index === stepIndex
            ? state.kind === 'failed'
              ? 'failed'
              : 'active'
            : 'pending',
  }));

  return (
    <form
      className="org-form"
      noValidate
      data-dev-ui="DEV-UI-ORG-006"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field
        label={ORG_COPY.inviteLinkLabel}
        htmlFor="org-join-invite-link"
        hint="粘贴邀请链接或加入命令。"
        error={error ?? undefined}
        required
      >
        <input
          id="org-join-invite-link"
          ref={inputRef}
          className="org-input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={link}
          disabled={busy}
          onChange={(event) => setLink(event.target.value)}
        />
      </Field>
      <div className="org-actions">
        <Button type="submit" variant="primary" loading={busy}>
          {busy ? ORG_COPY.processing : ORG_COPY.join}
        </Button>
      </div>
      {state.kind === 'running' || state.kind === 'joined' || state.kind === 'failed' ? (
        <Stepper
          steps={steps}
          // 完成或失败后没有“当前步骤”；失败步骤本身带失败标记。
          currentId={state.kind === 'running' ? JOIN_STEPS[Math.max(stepIndex, 0)] : 'none'}
          ariaLabel="加入进度"
          className="org-stepper"
        />
      ) : null}
      <div
        role="status"
        aria-live="polite"
        className={state.kind === 'joined' ? 'org-join-result org-join-result--joined' : 'org-join-result'}
      >
        {state.kind === 'joined' ? ORG_COPY.joined(state.org.name) : null}
      </div>
      {state.kind === 'failed' ? (
        <Notice
          tone="danger"
          role="alert"
          title={ORG_COPY.joinFailed(state.reason)}
          action={
            state.retry
              ? {
                  label: '重试',
                  onClick: () => {
                    setState({ kind: 'idle' });
                    inputRef.current?.focus();
                  },
                }
              : undefined
          }
        >
          {state.next}
        </Notice>
      ) : null}
      {state.kind === 'unknown' ? (
        <Notice tone="warning" role="status">
          {state.message}
        </Notice>
      ) : null}
    </form>
  );
}
