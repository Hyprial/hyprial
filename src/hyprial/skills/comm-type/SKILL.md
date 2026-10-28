---
name: comm-type
description: >
  Decide what kind of communication this is, by asking what the recipient must do
  after reading it. Returns one label from a closed set, or no_match. Does not write
  the message. Use before choosing a message skeleton.
disable-model-invocation: true
---

# 判断沟通类型

只做一件事：判断这条沟通要求对方做什么。不写内容，不给改进意见。

## 分类的唯一依据

问一句：**对方读完要做什么？** 按对方的动作分类，不按内容主题分类。

## 七个标签

| 标签 | 判据：对方读完要做什么 | 反例（不是这一类） |
| --- | --- | --- |
| `ask` | 回答一个问句，或补一个我方拿不到的事实 | 只是告知进度 |
| `decide` | 在两个或以上选项里选择，并承担后果 | 我方能自己定 |
| `tell` | 不需要做任何事，知道了即可 | 需要他回一句确认 |
| `report` | 知道进度或结果，不需要动作 | 要他做决定 |
| `hand_over` | 接手这件事，此后由他推进 | 只是抄送他 |
| `refuse` | 知道边界变了：某条路已关闭，或某个承诺被撤回 | 只是推迟 |
| `incident` | 立刻采取或立刻停止某个动作 | 事后复盘 |

## 互斥检查

出标签前，用两句问一次：

```
这个标签说“对方要做 X”。还有别的标签也说“对方要做 X”吗？
有，就是分类没设计好，报 ambiguous，不得硬选。
```

## 不属于任何一类时

返回 `no_match`，并给一句“缺的是什么”。不得为凑一个标签而扩大它的定义。

## 输出

只输出三行，不得解释：

```
type: <ask|decide|tell|report|hand_over|refuse|incident|no_match>
recipient_action: <一句话，对方读完要做什么>
why_not_others: <一句话，最接近的那个标签为什么不是>
```

## 已知边界

```
tell 与 report 的分界：tell 是事件（某件事发生了），report 是过程（一段时间里做到哪）
refuse 与 decide 的分界：refuse 关闭一条路，decide 在已有的路里选
hand_over 与 tell 的分界：hand_over 之后对方要有下文，tell 没有
拿不准时，先看“要不要对方动手”。要动手，就不是 tell
```
