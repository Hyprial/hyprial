# dev-01 账号、首次设置与模型服务

> 历史模块设计与场景记录，保留当时的说明。当前页面、术语和后端接线请查 [统一方法对账](../../../docs/design/gui-v0.1-method-plan-2026-11-13.md)，不按本文旧状态表继续补方法。

## J10 模型服务管理（T1）

- 组件：`src/features/connections/impl/ModelServiceSection.tsx`，模型 `impl/model-services-view-model.ts`，样式 `impl/connections.css`。
- 组合：Card、Field、Button、Dialog、Notice、EmptyState，全部来自 `components/primitives`。
- 会话：`modelServices`（含 `isDefault`、`method`）与 `modelAddMode`；动作 `model-add`、`model-set-default`。
- 行为：J1 完成后显示“模型服务 A · 已连接 · 默认 · Agent 1”；添加服务后默认唯一且可切换；接入方式只有账号授权与 API Key，互斥；额度说明不含数字；失败保留字段可重试；密钥只存在组件 state，提交后清空，不写入会话。
- 失败时保留服务名称、提供方与接入方式；API Key 一经提交（无论成功或失败）立即清空，重试必须重新输入；账号授权方式没有密钥字段。
- 取消有输入时弹 Dialog：标题“放弃添加模型服务？”，动作“继续填写”“丢弃并返回”。
- 开发场景 `remaining-scenarios.ts` 通过 hash `j10=add-failure` 触发失败分支；产品构建不含该分支。
- J10 浏览器合同已纳入 `npm run verify`。正式模式从“开始”经“设置 → 打开首次设置”进入“账号与设备”；账号服务不可达时停留在该步骤，不显示模型服务成功状态。

## DEV-UI-001（记录，不是审批闸）

Dialog 与 States 首次进入 fixture 主路径：`ModelServiceSection` 使用 Dialog（放弃输入确认）与 EmptyState（空态）；该区块已随模型服务面板删除。对应组件回归由 `tests/primitives.test.tsx` 与 `tests/modules/dev01/j10-model-services.test.tsx` 覆盖；`docs/component-catalog.zh-CN.md` 同步实际接入范围。

## 边界

- 不接真实后端；正式构建无模型服务时显示暂不可用，0 业务请求、0 存储写入。
- 本文件只描述 J10；J4、J8、J9 见后续提交。
