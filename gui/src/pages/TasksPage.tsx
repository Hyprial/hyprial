export function TasksPage() {
  return (
    <section className="page page-tasks" data-page="tasks">
      <div className="page-heading">
        <div>
          <h1 className="page-title">任务</h1>
          <p className="page-subtitle">查看、分派和跟踪任务。</p>
        </div>
      </div>

      <section className="card page-note-card task-unavailable">
        <span className="eyebrow">暂不可用</span>
        <h2>任务功能暂不可用</h2>
        <p>暂不可用</p>
      </section>
    </section>
  );
}
