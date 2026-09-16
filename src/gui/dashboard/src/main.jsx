import KanbanView from './KanbanView.jsx';
import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const SECTIONS = [
  ["overview", "运行全景", "overview"],
  ["agents", "节点与 Agent", "network"],
  ["runs", "运行与调度", "activity"],
  ["kanban", "任务看板", "layers"],
  ["delivery", "投递", "send"],
  ["integrations", "集成", "layers"],
  ["system", "系统", "server"],
];
const PATHS = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  network: "M9 3h6v6H9z M2 16h6v5H2z M16 16h6v5h-6z M12 9v4 M5 16v-3h14v3",
  activity: "M2 12h5l3-8 4 16 3-8h5",
  send: "m3 3 18 9-18 9 4-9-4-9z M7 12h14",
  layers: "m12 3 10 5-10 5L2 8l10-5z M2 12l10 5 10-5 M2 16l10 5 10-5",
  server: "M3 3h18v7H3z M3 14h18v7H3z M7 6v1 M7 17v1",
  refresh: "M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 13 1 M18 18a8 8 0 0 1-13-1",
  expand: "M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
  arrow: "M5 12h14 M14 7l5 5-5 5",
  lock: "M6 10h12v11H6z M8 10V6a4 4 0 0 1 8 0v4",
  close: "m6 6 12 12 M6 18 18 6",
  check: "m5 12 4 4L19 6",
  alert: "m12 3 10 18H2L12 3z M12 9v4 M12 16v1",
};
function Icon({ name, size = 18 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name] || PATHS.overview} />
    </svg>
  );
}
const STATUS = {
  online: ["在线", "good"],
  running: ["运行中", "good"],
  ok: ["正常", "good"],
  idle: ["空闲", "good"],
  busy: ["执行中", "blue"],
  completed: ["已完成", "good"],
  succeeded: ["已完成", "good"],
  offline: ["离线", "muted"],
  stopped: ["已停止", "muted"],
  unknown: ["未知", "muted"],
  failed: ["失败", "bad"],
  fail: ["异常", "bad"],
  error: ["错误", "bad"],
  warn: ["需关注", "warn"],
  paused: ["已暂停", "warn"],
  pending: ["等待中", "warn"],
  undeliverable: ["地址不可投递", "bad"],
  queued: ["排队中", "warn"],
  cancelled: ["已取消", "muted"],
  canceled: ["已取消", "muted"],
  enabled: ["已启用", "good"],
  disabled: ["未启用", "muted"],
  accepted: ["已采纳", "good"],
  absent: ["未采纳", "muted"],
};
function Badge({ value }) {
  const [label, tone] = STATUS[value] || [value || "未知", "muted"];
  return (
    <span className={`badge ${tone}`}>
      <i />
      {label}
    </span>
  );
}
function time(value) {
  return value
    ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false })
    : "尚未取得";
}
function date(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "未知";
}
function display(value) {
  return value === null || value === undefined || value === ""
    ? "未知"
    : value === true
      ? "是"
      : value === false
        ? "否"
        : String(value);
}
function stale(record, now) {
  return (
    !!record?.data &&
    (!!record.error || now - record.updatedAt > record.intervalMs * 2)
  );
}
function sourceText(record, now) {
  if (!record) return "正在获取状态…";
  if (record.error)
    return record.data
      ? `数据过期 · ${record.error.message}`
      : record.error.message;
  return `${stale(record, now) ? "数据过期 · " : ""}${record.scope} · ${time(record.updatedAt)}`;
}
async function request(url, signal) {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (!response.ok) throw new Error("Dashboard 服务连接失败");
  return response.json();
}
function useDashboard() {
  const [sources, setSources] = useState([]);
  const [records, setRecords] = useState({});
  const [error, setError] = useState("");
  const [apps, setApps] = useState({});
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const refresh = useRef(() => {});
  useEffect(() => {
    let mounted = true;
    let active = false;
    let catalogue = [];
    const controller = new AbortController();
    async function load() {
      if (active || document.hidden || !mounted) return;
      active = true;
      setBusy(true);
      setNow(Date.now());
      try {
        if (!catalogue.length) {
          const caps = await request(
            "/api/capabilities",
            AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
          );
          if (
            caps.protocolVersion !== 1 ||
            caps.guiMode !== "display" ||
            caps.readOnly !== true ||
            !Array.isArray(caps.sources)
          )
            throw new Error("只读服务能力无法确认");
          catalogue = caps.sources;
          if (mounted) setSources(catalogue);
        }
        if (mounted) setError("");
        void request(
          "/api/apps",
          AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
        )
          .then((value) => {
            if (mounted) setApps(value);
          })
          .catch(() => {
            if (mounted) setApps({});
          });
        await Promise.allSettled(
          catalogue.map(async (source) => {
            try {
              const record = await request(
                `/api/sources/${source.id}`,
                AbortSignal.any([
                  controller.signal,
                  AbortSignal.timeout(45000),
                ]),
              );
              if (
                record.operation !== source.id ||
                typeof record.intervalMs !== "number"
              )
                throw new Error("状态响应无法识别");
              if (mounted)
                setRecords((old) => ({ ...old, [source.id]: record }));
            } catch {
              if (mounted)
                setRecords((old) => ({
                  ...old,
                  [source.id]: {
                    ...old[source.id],
                    operation: source.id,
                    label: source.label,
                    scope: source.scope,
                    intervalMs: source.intervalMs,
                    error: {
                      code: "CONNECTION_FAILED",
                      message: "状态服务连接失败",
                    },
                  },
                }));
            }
          }),
        );
      } catch (e) {
        if (mounted) setError(e.message);
      } finally {
        active = false;
        if (mounted) {
          setBusy(false);
          setNow(Date.now());
        }
      }
    }
    refresh.current = load;
    void load();
    const timer = setInterval(load, 10000);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    document.addEventListener("visibilitychange", load);
    window.addEventListener("online", load);
    return () => {
      mounted = false;
      controller.abort();
      clearInterval(timer);
      clearInterval(clock);
      document.removeEventListener("visibilitychange", load);
      window.removeEventListener("online", load);
    };
  }, []);
  return {
    sources,
    records,
    apps,
    error,
    busy,
    now,
    refresh: useCallback(() => refresh.current(), []),
  };
}

const COLUMNS = {
  hosts: [
    ["nodeId", "节点"],
    ["status", "在线状态"],
  ],
  agents: [
    ["actor", "Agent"],
    ["machine", "节点"],
    ["runtime", "运行方式"],
    ["status", "在线状态"],
  ],
  topology: [
    ["name", "Agent"],
    ["runtime", "运行方式"],
    ["state", "执行状态"],
    ["pendingCount", "等待消息"],
  ],
  workflows: [
    ["name", "Workflow"],
    ["runId", "运行 ID"],
    ["state", "状态"],
  ],
  routines: [
    ["name", "Routine"],
    ["enabled", "调度状态"],
    ["nextDueMs", "下次触发"],
    ["inFlightCount", "执行中"],
  ],
  outbox: [
    ["messageId", "消息 ID"],
    ["recipient", "接收者"],
    ["state", "状态"],
  ],
  adapters: [
    ["name", "Adapter"],
    ["provider", "平台"],
    ["status", "状态"],
  ],
};
const FIELD_LABELS = {
  ...Object.fromEntries(Object.values(COLUMNS).flat()),
  uri: "完整 Agent URI",
  actor: "Agent 身份",
  owner: "Owner",
  model: "模型",
  harness: "Harness",
  provider: "Provider",
  sender: "发送者",
  to: "接收地址",
  id: "标识",
  processState: "进程状态",
  running: "进程运行",
  pid: "PID",
  turnCount: "已执行轮次",
  openTurnStartedAtMs: "当前轮次开始",
  lastTurnEndedAtMs: "上次轮次结束",
  sourceErrorStreak: "连续源错误",
  createdAtMs: "创建时间",
  expiresAtMs: "过期时间",
  attempts: "尝试次数",
  online: "在线",
  configured: "已配置",
  desired: "期望启用",
  processRunning: "进程运行",
  nextAttemptMs: "下次尝试",
  undeliverableScheme: "地址类型不可投递",
};
function valueFor(row, key) {
  if (key === "messageId") return row.messageId || row.id;
  if (key === "recipient") return row.recipient || row.to;
  if (key === "state") return row.state || row.status;
  return row[key];
}
function Cell({ row, field }) {
  const value = valueFor(row, field);
  if (field === "state" || field === "status") return <Badge value={value} />;
  if (field === "enabled")
    return (
      <Badge
        value={
          value === true ? "enabled" : value === false ? "paused" : "unknown"
        }
      />
    );
  if (/AtMs$|DueMs$/.test(field)) return date(value);
  return display(value);
}
function rowKey(row) {
  return (
    row.uri ||
    row.runId ||
    row.messageId ||
    row.id ||
    row.nodeId ||
    row.name ||
    row.actor
  );
}

function Panel({ title, subtitle, children, action, className = "" }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}
function SourceNote({ record, now }) {
  return (
    <p
      className={`source-note ${record?.error || stale(record, now) ? "warning-text" : ""}`}
    >
      <span
        className={`source-dot ${record?.error || stale(record, now) ? "warning" : !record ? "waiting" : ""}`}
      />
      {sourceText(record, now)}
    </p>
  );
}
function Table({ id, record, now, onSelect, compact = false }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const items = record?.data?.items || [];
  const statuses = [
    ...new Set(
      items.map(
        (row) =>
          row.status ||
          row.state ||
          (row.enabled === true
            ? "enabled"
            : row.enabled === false
              ? "paused"
              : "unknown"),
      ),
    ),
  ];
  const filtered = items.filter(
    (row) =>
      JSON.stringify(row).toLowerCase().includes(search.toLowerCase()) &&
      (filter === "all" ||
        (row.status ||
          row.state ||
          (row.enabled === true
            ? "enabled"
            : row.enabled === false
              ? "paused"
              : "unknown")) === filter),
  );
  const size = compact ? 5 : 15;
  const currentPage = Math.min(
    page,
    Math.max(0, Math.ceil(filtered.length / size) - 1),
  );
  const visible = compact
    ? filtered.slice(0, size)
    : filtered.slice(currentPage * size, (currentPage + 1) * size);
  return (
    <>
      {!compact && (
        <div className="table-tools">
          <label className="search">
            <Icon name="search" />
            <input
              aria-label={`搜索${record?.label || id}`}
              placeholder="搜索名称、身份、节点…"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
            />
          </label>
          <select
            aria-label={`筛选${record?.label || id}状态`}
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setPage(0);
            }}
          >
            <option value="all">全部状态</option>
            {statuses.map((status) => (
              <option key={status} value={status}>
                {STATUS[status]?.[0] || status}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {COLUMNS[id].map(([key, title]) => (
                <th key={key}>{title}</th>
              ))}
              <th>
                <span className="sr-only">详情</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr key={rowKey(row) || index}>
                {COLUMNS[id].map(([key], i) => (
                  <td key={key} className={i === 0 ? "primary-cell" : ""}>
                    <Cell row={row} field={key} />
                  </td>
                ))}
                <td>
                  <button
                    className="icon-button"
                    aria-label={`查看 ${rowKey(row) || index + 1} 详情`}
                    onClick={() =>
                      onSelect({ source: id, key: rowKey(row), row })
                    }
                  >
                    <Icon name="arrow" size={16} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!visible.length && (
        <div className="empty-state">
          <Icon name={record?.error ? "alert" : "layers"} size={24} />
          <p>
            {!record
              ? "正在读取状态…"
              : !record.data
                ? record.error?.message || "状态未知"
                : search || filter !== "all"
                  ? "没有匹配的记录"
                  : "当前没有记录"}
          </p>
        </div>
      )}
      <div className="table-foot">
        <SourceNote record={record} now={now} />
        {record?.data && (
          <span>
            {compact
              ? `显示 ${visible.length} / ${record.data.total} 条`
              : `匹配 ${filtered.length} 条`}
            {record.data.truncated ? " · 仅载入前 500 条" : ""}
          </span>
        )}
      </div>
      {!compact && filtered.length > size && (
        <div className="pagination">
          <button
            disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)}
          >
            上一页
          </button>
          <span>
            {currentPage + 1} / {Math.ceil(filtered.length / size)}
          </span>
          <button
            disabled={(currentPage + 1) * size >= filtered.length}
            onClick={() => setPage(currentPage + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}

function Details({ selection, record, now, close }) {
  const ref = useRef(null);
  const closeButton = useRef(null);
  const current = record?.data?.items?.find(
    (row) => rowKey(row) === selection.key,
  );
  const row = current || selection.row;
  useEffect(() => {
    const dialog = ref.current;
    const previous = document.activeElement;
    dialog.showModal();
    closeButton.current.focus();
    return () => {
      dialog.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="detail-dialog"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === ref.current) close();
      }}
      aria-labelledby="detail-title"
    >
      <div className="detail-header">
        <span className="eyebrow">只读详情</span>
        <button
          ref={closeButton}
          className="icon-button"
          aria-label="关闭详情"
          onClick={close}
        >
          <Icon name="close" />
        </button>
      </div>
      <h2 id="detail-title">
        {row.name ||
          row.actor ||
          row.nodeId ||
          row.messageId ||
          row.runId ||
          "对象详情"}
      </h2>
      <SourceNote record={record} now={now} />
      {!current && (
        <p className="notice">此对象不在最新列表中，以下保留打开时的快照。</p>
      )}
      <dl className="detail-facts">
        {Object.entries(row).map(([key, value]) => (
          <div key={key}>
            <dt>{FIELD_LABELS[key] || key}</dt>
            <dd>{/AtMs$|DueMs$/.test(key) ? date(value) : display(value)}</dd>
          </div>
        ))}
      </dl>
      <p className="detail-caption">此页面仅展示身份与运行状态。</p>
    </dialog>
  );
}

function Facts({ record, now, rows }) {
  return (
    <>
      <dl className="facts">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{display(value)}</dd>
          </div>
        ))}
      </dl>
      <SourceNote record={record} now={now} />
    </>
  );
}
function App() {
  const { sources, records, apps, error, busy, now, refresh } = useDashboard();
  const readSection = () =>
    SECTIONS.some(([id]) => id === location.hash.slice(1))
      ? location.hash.slice(1)
      : "overview";
  const [section, setSection] = useState(readSection);
  const [selection, setSelection] = useState(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [screenError, setScreenError] = useState("");
  useEffect(() => {
    const change = () => {
      setSection(readSection());
      setSelection(null);
    };
    const screen = () => setFullscreen(!!document.fullscreenElement);
    window.addEventListener("hashchange", change);
    document.addEventListener("fullscreenchange", screen);
    return () => {
      window.removeEventListener("hashchange", change);
      document.removeEventListener("fullscreenchange", screen);
    };
  }, []);
  const data = (id) => records[id]?.data;
  const items = (id) => data(id)?.items || [];
  const count = (id, predicate = () => true) =>
    data(id) ? items(id).filter(predicate).length : "—";
  const daemon = data("processes")?.daemon;
  const issues = sources.flatMap((source) => {
    const record = records[source.id];
    return record?.error || stale(record, now)
      ? [
          {
            name: source.label,
            status: "warn",
            detail: sourceText(record, now),
            section: source.section,
          },
        ]
      : [];
  });
  items("doctor")
    .filter((check) => ["warn", "fail"].includes(check.status))
    .forEach((check) =>
      issues.push({ ...check, detail: "H2B 健康检查", section: "system" }),
    );
  const go = (id) => {
    location.hash = id;
  };
  function tablePanel(id, title, subtitle, compact = false) {
    return (
      <Panel
        key={id}
        title={title}
        subtitle={subtitle}
        action={
          compact && (
            <button
              className="text-button"
              onClick={() =>
                go(
                  id === "workflows"
                    ? "runs"
                    : id === "adapters"
                      ? "integrations"
                      : "agents",
                )
              }
            >
              查看全部 <Icon name="arrow" size={14} />
            </button>
          )
        }
      >
        <Table
          id={id}
          record={records[id]}
          now={now}
          onSelect={setSelection}
          compact={compact}
        />
      </Panel>
    );
  }
  const metrics = [
    {
      label: "Daemon",
      value: !daemon
        ? "未知"
        : data("processes").restoring
          ? "恢复中"
          : daemon.running
            ? "运行中"
            : "已停止",
      source: "processes",
      icon: "server",
      hint: "本机进程",
      section: "system",
    },
    {
      label: "在线节点",
      value: count("hosts", (row) => row.status === "online"),
      source: "hosts",
      icon: "network",
      hint: "Mesh 节点目录",
      section: "agents",
    },
    {
      label: "运行 Agent",
      value: count("topology", (row) => row.running === true),
      source: "topology",
      icon: "layers",
      hint: `本机已确认进程${data("topology") ? ` · ${items("topology").filter((row) => row.running === null).length} 个未知` : ""}`,
      section: "agents",
    },
    {
      label: "活跃 Workflow",
      value: count("workflows", (row) =>
        ["running", "pending", "queued"].includes(row.state),
      ),
      source: "workflows",
      icon: "activity",
      hint: "本机 · 最近 50 条内",
      section: "runs",
    },
    {
      label: "待投递",
      value: data("outbox")?.total ?? "—",
      source: "outbox",
      icon: "send",
      hint: "本机 Outbox",
      section: "delivery",
    },
    {
      label: "健康异常",
      value: count("doctor", (row) => ["warn", "fail"].includes(row.status)),
      source: "doctor",
      icon: "alert",
      hint: "H2B 最近一次检查",
      section: "system",
    },
  ];
  const service = data("service");
  const update = data("autoupdate");
  const org = data("organization");
  const readyCount = sources.filter(
    (source) =>
      records[source.id]?.data &&
      !records[source.id].error &&
      !stale(records[source.id], now),
  ).length;
  return (
    <div className={`app ${fullscreen ? "fullscreen" : ""}`}>
      <aside className="sidebar">
        <a className="brand" href="#overview">
          <span className="brand-symbol">
            H<span>2</span>
          </span>
          <span>
            H2B<span className="brand-sub">DASHBOARD</span>
          </span>
        </a>
        <div className="nav-label">工作空间 / OBSERVE</div>
        <nav aria-label="主导航">
          {SECTIONS.map(([id, title, icon]) => (
            <a
              key={id}
              href={`#${id}`}
              className={section === id ? "active" : ""}
              aria-current={section === id ? "page" : undefined}
            >
              <Icon name={icon} />
              <span>{title}</span>
              {section === id && <i />}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="readonly-stamp">
            <Icon name="lock" size={16} />
            <span>展示模式 · 只读</span>
          </div>
          <p>观察运行，保持专注。</p>
          <span className="sidebar-version">
            H2B {data("version")?.version || "版本待获取"}
          </span>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            H2B <span>/</span> Dashboard <span>/</span>{" "}
            <strong>{SECTIONS.find(([id]) => id === section)[1]}</strong>
          </div>
          <div className="topbar-right">
            {apps.dsh && (
              <a
                className="text-button"
                href={apps.dsh}
                target="_blank"
                rel="noopener noreferrer"
              >
                打开开发工作台 <Icon name="arrow" size={14} />
              </a>
            )}
            <span className="scope-tag">本机 + Mesh 目录</span>
            <span className="readonly-tag">
              <Icon name="lock" size={12} />
              只读
            </span>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">H2B / OBSERVABILITY</div>
              <h1>{SECTIONS.find(([id]) => id === section)[1]}</h1>
              <p>
                {section === "overview"
                  ? "一处了解节点、Agent 与任务的当前运行状态。"
                  : section === "kanban" ? "查看本机任务进度，在工作台中交给 Agent 更新。" : "浏览当前状态，选择记录查看只读详情。"}
              </p>
            </div>
            <div className="heading-actions">
              {section !== "kanban" && <>
              <div className="refresh-meta">
                <span
                  className={`source-dot ${readyCount !== sources.length || !sources.length ? "warning" : ""}`}
                />
                {busy
                  ? "正在更新"
                  : `${readyCount} / ${sources.length || "—"} 数据源可用`}
                <small>每 10 秒刷新 · 系统状态每 60 秒</small>
              </div>
              <button className="button" disabled={busy} onClick={refresh}>
                <Icon name="refresh" />
                刷新
              </button>
              </>}
              <button
                className="button square"
                aria-label={fullscreen ? "退出全屏" : "全屏展示"}
                onClick={async () => {
                  try {
                    if (document.fullscreenElement)
                      await document.exitFullscreen();
                    else await document.documentElement.requestFullscreen();
                    setScreenError("");
                  } catch {
                    setScreenError("当前浏览器无法进入全屏");
                  }
                }}
              >
                <Icon name="expand" />
              </button>
            </div>
          </div>
          {(error || screenError) && (
            <div className="notice" role="alert">
              {error || screenError}
            </div>
          )}
          {section === "overview" && (
            <>
              <div className="metrics">
                {metrics.map((metric) => (
                  <button
                    key={metric.label}
                    className={`metric ${stale(records[metric.source], now) ? "stale" : ""}`}
                    onClick={() => go(metric.section)}
                  >
                    <span className="metric-label">
                      {metric.label}
                      <Icon name={metric.icon} size={17} />
                    </span>
                    <strong>
                      {metric.value}
                      {data(metric.source)?.truncated ? "+" : ""}
                    </strong>
                    <span className="metric-hint">{metric.hint}</span>
                    <span className="metric-time">
                      {records[metric.source]?.error ||
                      stale(records[metric.source], now)
                        ? "状态过期 / 不可用"
                        : time(records[metric.source]?.updatedAt)}
                    </span>
                  </button>
                ))}
              </div>
              <div className="overview-grid">
                <Panel
                  title="节点分布"
                  subtitle="Mesh 节点目录 · 在线不等于可执行"
                  action={
                    <button
                      className="text-button"
                      onClick={() => go("agents")}
                    >
                      节点详情 <Icon name="arrow" size={14} />
                    </button>
                  }
                >
                  <div className="node-grid">
                    {items("hosts")
                      .slice(0, 6)
                      .map((host) => (
                        <button
                          key={host.nodeId}
                          className="node-card"
                          onClick={() =>
                            setSelection({
                              source: "hosts",
                              key: host.nodeId,
                              row: host,
                            })
                          }
                        >
                          <span className="node-symbol">
                            <Icon name="server" size={22} />
                          </span>
                          <span className="node-name">{host.nodeId}</span>
                          <Badge value={host.status} />
                          <span className="node-type">
                            {host.nodeId === daemon?.nodeId
                              ? "本机节点"
                              : "网络节点"}
                          </span>
                        </button>
                      ))}
                  </div>
                  {!items("hosts").length && (
                    <div className="empty-state">
                      <Icon name="network" size={28} />
                      <p>
                        {data("hosts")
                          ? "当前没有节点记录"
                          : "节点状态尚不可用"}
                      </p>
                    </div>
                  )}
                  <SourceNote record={records.hosts} now={now} />
                </Panel>
                <Panel
                  title="需要关注"
                  subtitle="健康检查与数据源状态"
                  className="attention-panel"
                  action={<span className="count-pill">{issues.length}</span>}
                >
                  <div className="attention-list">
                    {issues.length ? (
                      issues.map((issue, i) => (
                        <button
                          className="attention-item"
                          key={`${issue.name}-${i}`}
                          onClick={() => go(issue.section)}
                        >
                          <span
                            className={`attention-icon ${issue.status === "fail" ? "bad" : ""}`}
                          >
                            <Icon name="alert" size={17} />
                          </span>
                          <span>
                            <strong>{issue.name}</strong>
                            <small>{issue.detail}</small>
                          </span>
                          <Icon name="arrow" size={15} />
                        </button>
                      ))
                    ) : (
                      <div className="attention-clear">
                        <span>
                          <Icon name="check" size={25} />
                        </span>
                        <strong>
                          {readyCount === sources.length && sources.length
                            ? "当前未发现异常"
                            : "正在汇总状态"}
                        </strong>
                        <p>各分区保留独立采集时间</p>
                      </div>
                    )}
                  </div>
                  <SourceNote record={records.doctor} now={now} />
                </Panel>
              </div>
              <div className="overview-grid lower-grid">
                {tablePanel(
                  "workflows",
                  "近期运行",
                  "本机最近 50 条 Workflow · 完成状态不代表业务验收",
                  true,
                )}
                {tablePanel("adapters", "集成状态", "平台接入与运行状态", true)}
              </div>
            </>
          )}
          {section === "kanban" && <KanbanView dshUrl={apps.dsh} />}
          {section === "agents" && (
            <div className="section-stack">
              {tablePanel("hosts", "网络节点", "Mesh 目录中的节点在线状态")}
              {tablePanel(
                "agents",
                "Agent 身份",
                "本机身份目录 · 在线、进程运行和正在执行分别展示",
              )}
              {tablePanel(
                "topology",
                "Agent 运行状态",
                "本机运行全景 · 未报告进程状态时显示未知",
              )}
            </div>
          )}
          {section === "runs" && (
            <div className="section-stack">
              {tablePanel(
                "workflows",
                "Workflow 运行",
                "本机最近 50 条 · 完成状态不代表业务验收",
              )}
              {tablePanel(
                "routines",
                "Routine 调度",
                "已注册调度、下次触发与执行中数量",
              )}
            </div>
          )}
          {section === "delivery" &&
            tablePanel(
              "outbox",
              "待投递队列",
              "当前本机 Outbox · 仅展示消息元数据，不读取正文或确认消息",
            )}
          {section === "integrations" &&
            tablePanel(
              "adapters",
              "Adapter 集成",
              "已配置、在线和进程运行是不同状态，可在详情中分别查看",
            )}
          {section === "system" && (
            <>
              <div className="system-grid">
                <Panel
                  title="本机 Daemon"
                  subtitle={daemon?.nodeId || "节点未知"}
                >
                  <Facts
                    record={records.processes}
                    now={now}
                    rows={[
                      ["运行中", daemon?.running],
                      ["恢复中", data("processes")?.restoring],
                      ["Owner", daemon?.owner],
                      ["PID", daemon?.pid],
                      ["Epoch", daemon?.epoch],
                    ]}
                  />
                </Panel>
                <Panel
                  title="服务状态"
                  subtitle="进程运行与系统服务安装分别展示"
                >
                  <Facts
                    record={records.service}
                    now={now}
                    rows={[
                      ["运行模式", service?.mode],
                      ["当前运行", service?.running],
                      ["已安装系统服务", service?.installed],
                      ["H2B 版本", data("version")?.version],
                    ]}
                  />
                  <SourceNote record={records.version} now={now} />
                </Panel>
                <Panel title="更新计划" subtitle="只读查看现有计划">
                  <Facts
                    record={records.autoupdate}
                    now={now}
                    rows={[
                      ["已启用", update?.enabled],
                      ["触发方式", update?.trigger],
                      [
                        "下次执行",
                        update?.nextRunAt ? date(update.nextRunAt) : null,
                      ],
                      [
                        "计划时刻",
                        update?.schedule?.length
                          ? update.schedule
                              .map(
                                (row) =>
                                  `${String(row.hour).padStart(2, "0")}:${String(row.minute).padStart(2, "0")}`,
                              )
                              .join(" / ")
                          : null,
                      ],
                    ]}
                  />
                </Panel>
                <Panel title="组织上下文" subtitle="已采纳状态 · 候选数量未知">
                  <Facts
                    record={records.organization}
                    now={now}
                    rows={[
                      ["状态", org ? STATUS[org.status]?.[0] : null],
                      ["版本", org?.meta?.version],
                      ["发布者", org?.meta?.publisher],
                      ["采纳时间", org?.adoptedAt ? date(org.adoptedAt) : null],
                    ]}
                  />
                </Panel>
              </div>
              <Panel title="健康检查" subtitle="H2B 只读诊断 · 每 60 秒更新">
                <div className="health-list">
                  {items("doctor").map((check) => (
                    <div key={check.name}>
                      <span>{check.name}</span>
                      <Badge value={check.status} />
                    </div>
                  ))}
                </div>
                <SourceNote record={records.doctor} now={now} />
              </Panel>
            </>
          )}
          <footer className="page-footer">
            <span>
              <Icon name="lock" size={12} />
              展示模式 · 仅浏览状态
            </span>
            <span>
              H2B 是状态事实来源 <span className="footer-separator">/</span>{" "}
              {new Date(now).toLocaleDateString("zh-CN")}
            </span>
          </footer>
        </main>
      </div>
      {selection && (
        <Details
          selection={selection}
          record={records[selection.source]}
          now={now}
          close={() => setSelection(null)}
        />
      )}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
