import type { ModelKeySaveResult, ModelKeyStatusCheck, SetupSnapshot } from './types';

/** A save may have been dispatched; it is neither a rejection nor a confirmed write. */
export function unknownModelKeySaveResult(): ModelKeySaveResult {
  return {
    status: 'unknown',
    message: '保存请求可能已发出，结果未确认，不会自动重试。请先检查状态，确认前不要重复保存。',
  };
}

/** Read-only projection of existing setup status; never attributes a key to this attempt. */
export function modelKeyStatusCheck(
  snapshot: Pick<SetupSnapshot, 'keySaved' | 'pendingAction'>,
): ModelKeyStatusCheck {
  if (snapshot.pendingAction === 'save') {
    return {
      status: 'checked',
      keySaved: snapshot.keySaved === true ? true : snapshot.keySaved === false ? false : null,
      message: '上次保存仍未完成，无法确认本次输入是否已保存，旧 Key 也不视为已确认。',
    };
  }
  if (snapshot.keySaved === true) {
    return {
      status: 'checked',
      keySaved: true,
      message: '本机已保存某个 Key，但无法确认是否为本次输入，不视为保存成功。',
    };
  }
  if (snapshot.keySaved === false) {
    return {
      status: 'checked',
      keySaved: false,
      message: '本机未保存 Key，本次保存未确认。请重新输入并保存。',
    };
  }
  return {
    status: 'checked',
    keySaved: null,
    message: '未能确认 Key 保存状态，请检查本机服务。',
  };
}
