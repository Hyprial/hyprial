export type DataMode = 'fixture';

export const FIXTURE_BADGE = '开发数据';

export const FIXTURE_BOUNDARY_NOTE = '暂不可用';

export const BACKEND_NOT_WIRED =
  'not implemented: hyprial backend not wired';

export function unsupportedNotice(action: string): string {
  return `${action}暂不可用。`;
}

export const SEND_NOTICE = '消息未发送成功。';
