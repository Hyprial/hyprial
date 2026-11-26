#!/usr/bin/env python3
"""cancel-queue-plan.py — 只读地生成"待取消 run 清单"，供有权限者用 API 执行。

⛔ 本工具不写库（emergent 2026-10-30 规则: 批量取消一律走 API；DB 层 UPDATE/DELETE 需 Allen+emergent 批准）

用法:
  cancel-queue-plan.py --out /tmp/cancel-plan.tsv [--exclude-workflows a,b,c]

输出: TSV(列: run_id, workflow, ref, event, created_iso, jobs_waiting, jobs_blocked)
      并在末尾打印可直接粘给有权限者的 API 命令模板（curl -X POST …/actions/runs/<id>/cancel）
退出: 0 生成成功 | 3 读不到库
"""
import argparse
import os
import pathlib
import subprocess
import sys

# release-main-dispatch.yml is main-release.yml renamed (2026-11-06); runs queued
# under the old name are excluded too until none can remain.
DEFAULT_EXCLUDE = ("wheel-release.yml", "github-publish.yml", "release-tag.yml", "main-release.yml",
                   "release-main-dispatch.yml")

SQL = """
select r.id, r.workflow_id, r.ref, r.event,
       to_char(to_timestamp(r.created), 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as created_iso,
       count(*) filter (where j.status = 5) as jobs_waiting,
       count(*) filter (where j.status = 7) as jobs_blocked
from action_run r join action_run_job j on j.run_id = r.id
where r.status = 5
group by r.id, r.workflow_id, r.ref, r.event, r.created
having count(*) filter (where j.status = 6) = 0      -- ⛔ 有 running job 的 run 不动
order by r.id;
"""


def psql(sql: str) -> str:
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", os.environ.get("HYPRIAL_CI_DB_HOST", "ci-db-host"),
         f"sudo docker exec -i {os.environ.get('HYPRIAL_CI_DB_CONTAINER', 'hyprial-forgejo-postgres-1')} "
         f"psql -U forgejo -d forgejo -t -A -F '\t' -f -"],
        input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        print("CANCEL_PLAN_UNREADABLE:", (r.stderr or r.stdout).strip()[:160], file=sys.stderr)
        sys.exit(3)
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude-workflows", default=",".join(DEFAULT_EXCLUDE))
    a = ap.parse_args()
    exclude = {w.strip() for w in a.exclude_workflows.split(",") if w.strip()}

    rows = [line for line in psql(SQL).splitlines() if line.strip()]
    keep = [r for r in rows if r.split("\t")[1] not in exclude]
    dropped = [r for r in rows if r.split("\t")[1] in exclude]

    out = pathlib.Path(a.out)
    with out.open("w", encoding="utf-8") as fh:
        fh.write("run_id\tworkflow\tref\tevent\tcreated_iso\tjobs_waiting\tjobs_blocked\n")
        for r in keep:
            fh.write(r + "\n")

    print(f"cancel_plan rows={len(keep)} excluded_release_chain={len(dropped)} out={out}")
    if dropped:
        for r in dropped:
            print(f"  EXCLUDED {r.split(chr(9))[0]} {r.split(chr(9))[1]} {r.split(chr(9))[2]}")
    print("# 有权限者执行（示例，逐条或批处理）:")
    for r in keep[:3]:
        print(f"#   curl -sS -X POST -H 'Authorization: token <ADMIN_TOKEN>' "
              f"http://git.internal.hyprial.com:3000/api/v1/repos/HyprialOS/harness-bridge/actions/runs/{r.split(chr(9))[0]}/cancel")
    print(f"#   … 共 {len(keep)} 条，见 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
