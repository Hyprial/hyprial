# Zenoh / tailnet code-path inventory — 2026-10-27

Pinned head `ebb696e032c4478ce8b526dcf55f65678e86052c` / tree `4993c9c49246774f79e496f6a4d41c2b0e7d9984`. Contract SHA256 `bbe1d28c3d7edba8206698d3e79d6c721bddfbe19da743b652815f22769f2a27`; declared base `80a5baafa`. Phase 1 source analysis only; phase 2 waits for tailnet implementation landing and R&D notification.

Own-library source delta is measured against merged integration parent `040be17c7fd95b4c2e9515d61347647cf4f3b2c4`, not against all unrelated dev changes in the merge. Paths/line anchors below were checked against current source; Python enclosing function names are AST-resolved, not copied from old contract line numbers. Source bytes for all cited files were verified to match the pinned Git blob.

## Boundaries and negative findings

- New/modified owned device modules have **0 direct Casdoor/OrgFS/org_directory import dependencies**; read call chains also reveal no direct remote membership lookup. Local DeviceRecord is not an OrgFS directory read.
- Static exact-symbol search in daemon/biz/shell finds **0 direct consumers** of DeviceTransportClient, DeviceAuthorityConfig, stage_transport_credential, validate_enrollment_response, load_client_tls_material, read_transport_credential, commit_transport_credential. No production fixed-Authority wiring is established by these APIs' existence.
- Scoped new membership/control token scan in forwarding/org-network/OrgFS/Go internal source finds **0 matches** for membership-status, member-revoke, trustMembersForUpperLayers, membershipStore, tailnet-membership-v1, tailnet-control-v1. Python and Go explicitly still declare v5.
- Static source/symbol/import analysis only; aliases/reflection/external plugin/service implementation and live behavior are not proved. No tests/probes/full/CI actions; no production/config/token/daemon/raw-CI-log reads; no source/Git writes or publication.


## A own library

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/identity/impl/device/__init__.py:13` / `module-level / 导出binding/client/credential`; 导出binding/client/credential | 公共导出，无membership读取 → **上层经公共面注入已确认的sidecar事实，identity不反依赖daemon** | §1/3 |
| `src/hyprial/identity/interface.py:377` / `module-level / lazy facade`; lazy facade | 映射到device库；未见实际daemon调用方 → **保留公共面；不在facade新增目录discovery** | §1/3 |
| `src/hyprial/identity/impl/device/store.py:64` / `read_device_record`; read_device_record | 读本机device.json和device-identity.json；严格file_reader可注入 → **保留本地key/UUID检查，不视为leased active member** | §2/5 |
| `src/hyprial/identity/impl/device/transport_credential.py:158` / `stage_transport_credential`; stage_transport_credential | caller owner/UUID/generation + 本机record生成CSR → **调用方先核specific tailnet active/memberReady；UUID映射待定义** | §2/5 |
| `src/hyprial/identity/impl/device/transport_credential.py:862` / `_require_local_device`; local-device check | 安全读本机DeviceRecord精确比UUID；无目录查询 → **不能代替sidecar member检查；identity仍不反向依赖daemon** | §2/3.2 |
| `src/hyprial/identity/impl/device/transport_credential.py:607` / `_projection_from_mapping`; response projection | ownerUserId/policyIssuer/trustBundleId来自响应且仅作schema/type检查 → **固定Authority如何给这些字段赋权需Phase2明定，不能臆造Casdoor lookup** | §2/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:269` / `validate_enrollment_response`; validate enrollment | caller effective_owner/pinned CA + staged UUID/SPKI/本机record → **保持证书校验；CA bootstrap待定义；证书有效≠lease有效** | §2/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:667` / `_validate_certificate_projection`; certificate profile | CN/generation/SPKI/签名SAN/时间/profile → **保留旧profile；不得视为master持钥证明或租约** | §1/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:730` / `_verify_chain`; chain verifier | caller pinned bundle检验chain → **pin到fixed tailnet Authority的初始CA和transfer待协调，不从目录补洞** | §2/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:313` / `commit_transport_credential`; commit_transport_credential | 再次validate后本机key/projection/bundle持久提交 → **不是第二份membership；sidecar租约仍独立控制业务** | §2/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:416` / `read_transport_credential`; read_transport_credential | 本机bundle ID/content hash、SPKI、chain/时间 → **本地存在不授予active member身份** | §2/4 |
| `src/hyprial/identity/impl/device/transport_credential.py:478` / `retire_transport_credential`; local retire | 删除本机credential边界文件 → **不是Zenoh deny/member-revoke，交给完整退出事务协调** | §6 |
| `src/hyprial/identity/impl/device/transport_credential.py:572` / `transport_credential_status`; status projection | CN/binding/generation/截断SPKI/时间 → **继续脱敏，不透出authority/PEM/token/fullroster** | §2/8 |
| `src/hyprial/identity/impl/device/transport_storage.py:192` / `_read_safe_optional_file_beneath`; strict local reader | descriptor-relative owner/mode/no-follow本机读取 → **Python不能借此读sidecar membership.json** | §2/3.2 |
| `src/hyprial/identity/impl/device/binding/__init__.py:3` / `module-level / binding exports`; binding exports | 已有profile/JCS/SAN函数 → **保留协议，不新增signed roster** | §1 |
| `src/hyprial/identity/impl/device/binding/profile.py:23` / `module-level / BINDING_URI_NAMESPACE`; BINDING_URI_NAMESPACE | 固定SAN协议标识，非Casdoor endpoint；十字段含policyIssuer → **保持原字节，独立于配置issuer/Authority地址** | §1 + frozen profile |
| `src/hyprial/identity/impl/device/binding/jcs.py:27` / `canonical_bytes`; canonical_bytes | 十字段RFC8785编码 → **不拿它新造control签名协议** | §1 |
| `src/hyprial/identity/impl/device/binding/crypto.py:15` / `certificate_public_key`; certificate helpers | X.509 key/hash/signature typed边界 → **保持已有证书验证；非membership master认证** | §1/4 |
| `src/hyprial/identity/impl/device/binding/extensions.py:10` / `read_certificate_extensions`; extension helper | typed X.509扩展读取 → **保持profile语义，不发明roster协议** | §1 |
| `src/hyprial/identity/impl/device/client/__init__.py:3` / `module-level / client exports`; client exports | 导出显式配置client与TLS loader → **仅库公共面；固定Authority接线另行定义** | §3/4 |
| `src/hyprial/identity/impl/device/client/http.py:104` / `DeviceAuthorityConfig`; DeviceAuthorityConfig | caller HTTPS origin/server_name/CA/timeouts；无默认authority → **fixed Authority节点到HTTPS host/SNI/port/CA pin的适配未定义，不能直接设7448** | §2/4 |
| `src/hyprial/identity/impl/device/client/http.py:183` / `DeviceTransportClient`; enroll/renew/request_certificate/retire | 取staged UUID/CSR/generation；retire取bindingId；operationId replay → **active roster先决条件另接；credentialGeneration≠membership revision；HTTP retire≠member-revoke** | §2/3.2/6 |
| `src/hyprial/identity/impl/device/client/http.py:318` / `_exchange`; _exchange | socket HTTPS host/port+SNI；显式CA；可选client_tls → **control7448只有四种JSON-line op，无HTTPS enroll/renew/retire合同；CA/bootstrap待定** | §3/4 |
| `src/hyprial/identity/impl/device/client/http.py:616` / `_client_context`; _client_context | 显式trust_bundle，hostname校验，可选本机client chain/key → **定义首次认证/mTLS服务bootstrap，禁止Casdoor/OrgFS fallback** | §2/4 |
| `src/hyprial/identity/impl/device/client/http.py:486` / `_success_data`; retire success field | remoteEnforcementState仅非空str检查 → **不得作为ACL deny ACK或sidecar revoke receipt** | §6/7 |
| `src/hyprial/identity/impl/device/client/mtls.py:53` / `load_client_tls_material`; TLS material loader | 本机read credential+安全key/bundle bytes；secret repr=False → **保留私有传递，但材料存在不能绕过lease** | §2/4/8 |
| `src/hyprial/identity/impl/device/client/errors.py:31` / `TransportClientError`; typed boundary | 固定错误code/结果与异常隔离 → **保持脱敏/UNKNOWN结果语义；新membership错误映射要明确** | §3/8 |
| `src/hyprial/daemon/impl/configuration/network_profile.py:129` / `module-level / default issuer`; default issuer | OIDC profile与固定binding协议标识分离，own只改注记 → **登录/IAM业务可以保留；不是mTLS Authority发现** | §1/2 |

## B inherited admission

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/org/network/ipc.py:68` / `_build_service`; service composition | 先_require_orgfs_runtime facade，再注入store/policy/map/derive callbacks → **候选/membership路径不构造OrgFS服务；业务管理service可保留** | §3.2/5/9 |
| `src/hyprial/daemon/impl/org/network/admission/__init__.py:14` / `_directory_keys`; directory keys | fs.members+list_devices+derived map计算allow → **删除Python allow权威；sidecar独占leased roster准入** | §2/5/9 |
| `src/hyprial/daemon/impl/org/network/admission/__init__.py:61` / `sync_directory_admission_for_app`; sync admission | supervisor.allow；失败清空/关闭child → **v6显式ops；projection失败停新业务，不目录fallback** | §3/5/9 |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py:109` / `_org_view`; org view | fs.members/list_devices筛current成员 → **network身份/候选改sidecar active/memberReady projection** | §2/3.2/5 |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py:190` / `_org_access`; org access | derived key、本机server、member加入时间/设备时间 → **改sidecar派生映射；业务目录可展示但不授予tailnet准入** | §2/5 |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py:286` / `build_access_snapshot`; build snapshot | 目录carry、allow_keys、冲突/reissue、bootstrap地址合并 → **删除准入carry/目录冲突换key；只leased active roster，禁隐式key replacement** | §2/5/9 |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py:362` / `with_bootstrap`; late bootstrap merge | 每tick读持久bootstrap补snapshot → **删除兼容dial候选；显式pin/v2 redeem/一次migration** | §3.2/5/9 |
| `src/hyprial/daemon/impl/org/network/access/actor.py:157` / `desired_allow`; desired_allow | 目录snapshot给重启sidecar的allow → **删除计算/回放，用sidecar会员/ready事件** | §2/3/9 |
| `src/hyprial/daemon/impl/org/network/access/actor.py:248` / `_execute`; execute/apply allow | directory rebuild→allow→after_allow→redial → **拆除coupling；sidecar主导lease，不Python维护tick刷新租约** | §4/5/9 |
| `src/hyprial/daemon/impl/org/network/ipc.py:642` / `start_peer_access_for_app`; start peer-access | 构actor需OrgFS facade；org变更请求rebuild → **network生命周期由membership projection驱动** | §5/9 |
| `src/hyprial/daemon/impl/org/network/ipc.py:624` / `_reissue_conflicting_identity`; directory conflict reissue | reissue local key后restart → **不得目录冲突隐式替换固定master/server/pin** | §2/9 |
| `src/hyprial/daemon/impl/application/netendpoints/forwarding.py:57` / `_start_forwarding_supervisor`; start supervisor | 注入desired_allow与installed→rebuild → **v6 sidecar/control先起，membership owner只sidecar** | §3/5/9 |
| `src/hyprial/daemon/impl/application/netendpoints/forwarding.py:343` / `_reconcile_forwarding_endpoints`; reconcile forwarding | publish/leave后directory admission同步 → **删除directory allow与fallback，显式ops/ready** | §3/5/9 |
| `src/hyprial/daemon/impl/forwarding/supervisor.py:766` / `_spawn`; restart allow replay | desired_allow→controller.allow → **删除回放，restart清lease并fresh refresh** | §2/4/9 |
| `src/hyprial/daemon/impl/forwarding/controller.py:75` / `__init__`; v5 constructor | up强制allow/allowAny，现无membership-status → **v6 exact shapes/requestId/capabilities；拒v5/旧allow** | §3/9 |
| `src/hyprial/daemon/impl/forwarding/controller.py:601` / `allow`; allow method | 发v5 keys/allowAny替换 → **改显式membership command/receipt，不allow-set** | §3/9 |
| `src/hyprial/daemon/impl/forwarding/authority.py:274` / `allow`; allow actor port | actor委托controller.allow → **承载v6显式操作及operationId，不重造准入权威** | §3/6/9 |

## C candidates/invite

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/org/network/ipc.py:487` / `directory_peers_for_app`; candidate entry | snapshot+bootstrap→dial_peers(liveness)，空view建OrgFS，异常返旧/目录peers → **全路径leased active roster非空地址，错即不新admit，不建OrgFS恢复候选** | §3.2/5/9 |
| `src/hyprial/daemon/impl/application/netendpoints/endpoints.py:535` / `_tailcat_directory_peers`; app seam | 委托directory_peers_for_app → **同一seam改sidecar候选** | §5/9 |
| `src/hyprial/daemon/impl/forwarding/slots/reconcile.py:160` / `reconcile_slot_bindings`; slots reconcile | app目录seam或模块函数取wanted→map-peer → **只sidecar候选；multi-tailnet不误关另一个有效membership** | §2/5/9 |
| `src/hyprial/daemon/impl/org/network/dialing/__init__.py:72` / `_membership`; direction sources | 目录members.added_at排序，unknown可dial，120s liveness fallback → **只(masterPublic,deviceId)较大方主动，已选候选可常规retry** | §5/9 |
| `src/hyprial/daemon/impl/org/network/service.py:604` / `directory_peers`; directory peers | snapshot入口及_directory_peers_of直接list_devices并补bootstrap → **network候选全部统一sidecar；不是删除业务目录** | §5/9 |
| `src/hyprial/daemon/impl/org/network/state.py:122` / `bootstrap_peers_path`; bootstrap peers store | 持久dial-only地址fallback → **移除；仅显式Authority pin+receipt/v2兑换** | §2/3.2/5 |
| `src/hyprial/daemon/impl/org/network/service.py:352` / `execute`; execute/join/_execute_step | v1 bearer→connect-inviter→OrgFS目录/ACL join→publish→connect-members → **v2 redeem无OrgFS前置；member→Zenoh→OrgFS；不实施旧注释的签名方案** | §1/3.2/5 |
| `src/hyprial/identity/impl/org_directory/invite.py:80` / `InviteLink`; InviteLink/encode/decode | v1 inviter address/token/workflow hash/expiry → **外层space metadata+精确InviteV2，拒旧邀请，一次token，无新签名** | §1/3.2/5 |
| `src/hyprial/daemon/impl/org/network/service.py:668` / `_publish_into`; publication | 读directory所有server派生client maps写directory → **sidecar member-announce自动派生；publication独立上层且保留per-org隔离** | §3.2/5/9 |
| `src/hyprial/daemon/impl/application/netendpoints/forwarding.py:401` / `_publish_org_directory_if_address_changed`; publication cadence | 地址digest及定时derived发布；仅skipped阻digest → **不以republish恢复准入，保留失败/skip不推进成功digest** | §5/9 |
| `src/hyprial/daemon/impl/org/network/ipc.py:451` / `publish_self_for_app`; publish wrapper | 异常log后返回published=[]/skipped=[] → **该形状与caller仅检skipped存在静态对接风险，Phase2协调structured error，不在本阶段修** | §9 |

## D startup/Go

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/forwarding/policy.py:12` / `module-level / version constant`; version constant | Python v5 → **v6且无v5 fallback** | §3 |
| `src/hyprial/daemon/impl/forwarding_config.py:101` / `daemon_forwarding_environment`; up environment | 要求matching TCP Zenoh listener，up带allow/allowAny/tcp target → **private Unix+membershipStore/owner/deviceId/controlPort，无Zenoh probe** | §2/3.1/5 |
| `src/hyprial/daemon/impl/forwarding_config.py:308` / `environment`; automatic environment | 将已绑定target转tcp listen/up配置 → **sidecar/control先于Zenoh，私有Unix配置** | §3.1/5/9 |
| `src/hyprial/daemon/impl/application/wiring/transport.py:331` / `_startup_open_transport`; startup open | 自动预留TCP监听，先open Zenoh → **sidecar→control→member→private Unix Zenoh→OrgFS** | §5/7/9 |
| `src/hyprial/daemon/impl/application/wiring/transport.py:963` / `_startup_compose_runtime_bridge`; late sidecar start | graph.transport已构造才启动forwarding → **移除Zenoh先起依赖，分别报告五层ready** | §5/9 |
| `src/hyprial/daemon/impl/transport/zenoh/config.py:135` / `ZenohConfig`; ZenohConfig/to_json/build | 只有endpoint/scouting/retry/lease/sharedmemory，无TLS/ACL字段 → **0552明确真正mTLS/ACL配置/deny ACK；过渡flag不等于已实现** | §6/7 |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py:156` / `__init__`; zenoh.open | 直接open(config.build)，重建保存原config → **Phase2定义ACL接入/更新ACK/重建保留策略，不在此发明配置键** | §6/7 |
| `sidecar/hyprial-tailcat/internal/proto/message.go:27` / `Version/parseUp/ParseCommand`; Version/parseUp/ParseCommand | Go仍v5+allow capability/命令，TCP up → **v6 exact request/result/errors，1MiB/new capabilities，拒旧allow/any** | §3/4/9 |
| `sidecar/hyprial-tailcat/internal/engine/engine.go:190` / `func (e *Engine) Up(ctx context.Context, cfg UpConfig) (serverPublic, clientPublic string, err error)`; Engine.Up/Allow/admits | Python allow set定义route准入，tunnel AllowClient全true → **sidecar持久membership/lease独占，只有control可绕成员门** | §2/3/4/9 |
| `sidecar/hyprial-tailcat/internal/engine/engine.go:693` / `func (e *Engine) acceptInbound(listener net.Listener, inboundTarget string)`; acceptInbound/handleExposure | actual derived key在allow才TCP proxy，exposure可any → **私有Unix、leased active、localServer derived映射，production any拒绝** | §2/3.1/7 |
| `sidecar/hyprial-tailcat/internal/engine/clients.go:52` / `func (e *Engine) acquireClientLocked(address string) (*clientLease, error)`; acquireClientLocked/dial/handoff | master ClientPrivate+target serverPublic派生clientKey，server pool close旧DERP后换代 → **原不变量保留并给control/mesh/service共用；绝不master直接注册DERP** | §1/4/9 |

## E OrgFS/HTTP

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/application/wiring/transport.py:494` / `_startup_compose_delivery_plane`; OrgFS transport bind | Zenoh presence供supplier_online/holder candidates → **业务presence与tailnet准入候选分离；先member/private transport** | §5/7 |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py:428` / `_create_mesh_sync`; mesh construction | 注入directory supplier_owner、member_mode → **managed Unix一致policy；严格访问准入由Zenoh ACL，不倒置依赖** | §7 |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py:634` / `_supplier_owner_for_space`; supplier owner/index | org目录及trash索引node→owner；unknown开放利于join → **sidecar projection负责peer identity；unknown例外纳入显式transition flag** | §3.2/7 |
| `src/hyprial/daemon/impl/orgfs/mesh.py:280` / `_supplier_member_allowed`; supplier gate | resolver空/异常/None放行，已知owner验_member_mode → **0551managed Unix flag，0552实际ACL+恢复required gates，保留内容校验** | §7 |
| `src/hyprial/daemon/impl/orgfs/network/ingress.py:289` / `_requester_allowed`; requester gate | author格式+_member_mode → **transition只绕member gate仍验格式；严格入口消费Zenoh ACL** | §7 |
| `src/hyprial/daemon/impl/orgfs/network/sync.py:501` / `_sync_from_once`; sync/log/blob guards | 501/589/698调用_supplier_member_allowed → **所有sync/log/blob入口一致policy，不能遗漏旁路** | §7 |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py:513` / `_receive`; live update/recovery | receive调用_supplier_member_allowed → **同policy覆盖live/recovery，保留commit/author/hash** | §7 |
| `src/hyprial/daemon/impl/orgfs/network/queries.py:208` / `_document_page`; document/stream/sync/frontier query | 208/328/415/650复用_requester_allowed → **同policy覆盖四类请求，业务完整性约束不删** | §7 |
| `src/hyprial/daemon/impl/orgfs/webserver/ipc.py:147` / `DirectoryNodeKeyResolver`; DirectoryNodeKeyResolver | org.network IPC directory映射clientPublicByServer[localServer]→owner；非Casdoor直查 → **改私有sidecar leased active projection，用实际derived peer key；失败无identity** | §3.2/7/9 |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:451` / `serve`; resolver creation | PROXY v2默认装DirectoryNodeKeyResolver → **替换创建点并保留HTTP principal授权，不能只改resolver类** | §7 |
| `src/hyprial/daemon/impl/org/network/policy.py:69` / `OrgFsDerivedPolicy`; OrgFsDerivedPolicy | 业务管理roles/grants来自OrgFS/Casbin → **合法上层管理权限保留；不再反向授予tailnet成员/CA信任** | §7 |

## F removal/migration

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/org/network/service.py:378` / `leave`; leave/remove/delete/process leaves | 目录删除/leave_request/unmap，移Casdoor/OrgFS，无explicit revoke → **fixed roster targets+确认→OrgFS receipt→Zenoh deny ACK→same-op member-revoke，禁误删authority** | §4/6 |
| `src/hyprial/daemon/impl/org/network/service.py:859` / `_remove_member_and_devices`; remove helper | Casdoor first→_remove_orgfs_member，返回[]，寄望allow reconcile → **业务变更不能叫network revoke，持久阶段/backup/未知结果恢复** | §6 |
| `src/hyprial/daemon/impl/org/network/casdoor/membership.py:200` / `_remove_orgfs_member`; OrgFS primitive | 从directory/ACL space移member → **仅upper-layer receipt阶段，不等于sidecar撤权** | §6 |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:500` / `_apply`; read/plan/apply | startup/backstop/user查Casdoor，removed仅_remove_orgfs_member → **信号→同durable op，未显式确认pending-confirmation，非authority移他人pending-authority** | §4/6/9 |
| `src/hyprial/daemon/impl/org/network/ipc.py:240` / `ipc_org_leave`; destructive IPC entries | leave/remove/delete直接service，无固定roster目标确认输入 → **统一显式--yes/interactive确认与durable operation，不从无TTY推断** | §6 |
| `src/hyprial/daemon/impl/org/network/ipc.py:540` / `start_casdoor_membership_sync`; start sync/webhook callbacks | 启动actor/startup+backstop，webhook→user_changed/reconcile → **同撤权workflow，不能业务confirmed-members cache替代本次destructive确认** | §6 |
| `src/hyprial/daemon/impl/org/network/casdoor/webhook.py:189` / `_handle`; signal handler | 认证loopback后发用户/组织变更trigger → **保留signal用途，不当master/member身份或隐式revoke授权** | §6/7 |
| `src/hyprial/identity/impl/org_directory/models.py:194` / `from_record`; DirectoryDevice.from_record | strict parser仍要求clientPublicByServer且拒master=derived → **不全局compat放宽；仅一次offline migration parser可缺map，authority导入空derived，nonauthority只pin** | §5/9 |
| `src/hyprial/identity/impl/device/models.py:34` / `DeviceRecord`; DeviceRecord | device_id旧node id，device_uid可空独立P1 UUID4hex → **Member.deviceId是opaque，UUID映射须另定义，不哈希/截断/改名自造** | §2/5 |
| `src/hyprial/daemon/impl/network/tailcat/keys.py:150` / `_provision`; local key issue | genkey建master/server，保留或uuid4.hex生成device_uid → **本机identity来源保留，active membership必须显式init/re-enroll** | §2/5 |

## G privacy

| Actual path:line / function; related coverage | Current source → Phase2 consumer | Contract |
| --- | --- | --- |
| `src/hyprial/daemon/impl/org/network/status/view.py:30` / `device_view`; device_view/network_view | addressPresent替代secret address → **保持脱敏；private TailnetStatus.members不进普通log/公共diag** | §2/3.2/8 |
| `src/hyprial/daemon/impl/application/netendpoints/forwarding.py:182` / `_forwarding_status_json`; forwarding status | 聚合running/endpoints/exposures/telemetry → **分开sidecar/control/member/zenoh/orgfs facts，只安全摘要** | §2/5/8 |

## Field provenance and remaining interface gaps

| Fact | Current source | Phase2 unresolved contract |
| --- | --- | --- |
| owner/effective_owner | Caller/staged, local DeviceRecord; its model documents profile preferred_username | Sidecar active-row owner linkage; retain login/IAM business purposes without using IAM as continuous network membership. |
| ownerUserId | HTTP response field → exact projection → signed SAN; nonempty text only | No ownerUserId in Member schema. Fixed Authority issuer mapping must be explicitly defined; no assumed Casdoor lookup. |
| policyIssuer | HTTP response signed claim; no default profile comparison | Fixed Authority policy identifier/pin semantics; not proof of active membership. |
| deviceUid vs deviceId | Local P1 UUID4hex/staged/cert vs separate legacy node ID/opaque Member key | No schema mapping defined. Do not equate, hash, truncate, rename or silently change published protocol fields. |
| authority_origin | Actual API is DeviceAuthorityConfig.origin; caller-supplied DNS HTTPS host with exact SNI and explicit CA | Fixed Authority pin gives master/server/address/controlPort. Control7448 is JSON-line with only roster/announce/revoke/redeem; no HTTPS enroll/renew/retire, SNI/port/service adapter or first-client-auth bootstrap is defined. |
| trustBundleId / pinned PEM | Response ID; caller PEM; local ID/content binding hash and chain verification | Initial authenticated CA distribution, rotation/re-pin/authority transfer and relation to fixed node remain undefined. Local hash consistency does not identify the right CA. |
| credentialGeneration / X.509 validity | Authority credential projection + clock/profile verification | Separate from membership revision and process-local lease. A valid stored certificate cannot reopen expired business gates. |
| binding URI | Fixed BINDING_URI_NAMESPACE + existing v1 digest | Protocol identifier, not a Casdoor/Authority endpoint. Preserve bytes and current JCS/SAN semantics; no new roster signing scheme. |

## Read call chains

1. `slots.reconcile → app._tailcat_directory_peers → directory_peers_for_app → snapshot/with_bootstrap → dial_peers`; missing snapshot reaches `_build_service → OrgFS runtime`; error paths can return directory/snapshot candidates.
2. `start_peer_access → actor._execute → build_access_snapshot → fs.members/list_devices → _apply_allow → supervisor/controller`; separate supervisor relaunch replays desired_allow.
3. `DeviceTransportClient.enroll/renew → request_certificate → _perform → _exchange → explicitly configured HTTPS socket`; credential validation/commit/read are separate identity-local APIs, with no production consumer found in scoped symbol search.
4. `OrgFS.bind_transport → mesh construction → supplier_owner_for_space/device_owner_index → supplier gate`; requester gate is reused by document/stream/sync/frontier query paths.
5. `DirectoryNodeKeyResolver → org.network IPC → directory derived key for localServer → owner`; HTTP/business principal authorization remains retained after identity-source change.
6. `leave/remove/delete/process leave/Casdoor sync → business metadata changes`; no explicit Zenoh deny ACK/member-revoke receipt is present.

## Contract §9 differences and safeguards

- `src/hyprial/daemon/impl/upgrade/steps/directory_record.py` is absent in this exact tree. Current regular DirectoryDevice parser still requires derived maps; do not globally relax it for admission. A separate one-time offline migration parser is the only permitted missing-map exception.
- No exact republish/recover-admission warning matched checked directory/snapshot/forwarding files. Normal periodic publication remains present. Do not invent a forced-republish removal target that is absent.
- Per-org publication isolation exists. Outer publish_self_for_app failure returns empty published/skipped arrays while caller only checks skipped before advancing digest: record this static alignment risk, without implementing a fix in Phase1.
- Migration must be offline before network-dependent OrgFS, authority-only complete roster import, non-authority pin-only import, unambiguous explicit authority selection, source digest and accept/reject receipt, plus Python receipt preventing silent re-import after sidecar store loss. This is not recurring directory fallback.
- OrgFS transport access consumes Zenoh ACL in strict 0552, but business management/Casdoor/HTTP permissions and author/path/commit/hash/CRDT remain. 0551 trustMembersForUpperLayers is a private-Unix transition with acknowledged missing ACL/inter-org isolation, not strict acceptance.
- Required exit ordering: confirmed fixed roster targets → OrgFS receipt → Zenoh deny ACK → explicit confirmed member-revoke → authority revision/lease-bounded receipt. 0551 missing-ACL placeholder is not an ACK for 0552. Off-authority discovery of other-member removal remains pending-authority; unconfirmed discovery remains pending-confirmation.
- Keep fixed Authority/master/server/derived distinctions and shared per-server pool. Do not add election, MAC/signature/challenge/incarnation protocol, directory admission fallback or invented UUID/CA mappings.

## Exact owned source delta

| Status vs 040be17 | Path |
| --- | --- |
| M | `src/hyprial/daemon/impl/configuration/network_profile.py` |
| M | `src/hyprial/identity/impl/device/__init__.py` |
| A | `src/hyprial/identity/impl/device/binding/__init__.py` |
| A | `src/hyprial/identity/impl/device/binding/crypto.py` |
| A | `src/hyprial/identity/impl/device/binding/extensions.py` |
| A | `src/hyprial/identity/impl/device/binding/jcs.py` |
| A | `src/hyprial/identity/impl/device/binding/profile.py` |
| A | `src/hyprial/identity/impl/device/client/__init__.py` |
| A | `src/hyprial/identity/impl/device/client/errors.py` |
| A | `src/hyprial/identity/impl/device/client/http.py` |
| A | `src/hyprial/identity/impl/device/client/mtls.py` |
| M | `src/hyprial/identity/impl/device/store.py` |
| A | `src/hyprial/identity/impl/device/transport_credential.py` |
| A | `src/hyprial/identity/impl/device/transport_storage.py` |
| M | `src/hyprial/identity/interface.py` |

## Source fingerprints

These 55 source/contract file byte sets were independently checked by the coordinator against the pinned source head before publication. This table is source provenance, not raw execution output or a runtime/CI verdict.

| Source path | Bytes | SHA256 | Git blob |
| --- | ---: | --- | --- |
| `docs/design/tailcat-cycle-fix-contract-2026-10-27.md` | 30732 | `bbe1d28c3d7edba8206698d3e79d6c721bddfbe19da743b652815f22769f2a27` | `07fdaab3398cf5c33942eae8c4df3deb16836ea9` |
| `sidecar/hyprial-tailcat/internal/engine/clients.go` | 6063 | `ba6c8b46c5fa07956bc551528a3f3a0cae92568926398a061059fae5e272d6f7` | `79a514297f16ff1ebf49f32248c74b47593115e6` |
| `sidecar/hyprial-tailcat/internal/engine/engine.go` | 24019 | `072b5aa30d7c02bc1fcb5ec0a8d217b641e47764390d957808649b65a3ce6fd0` | `8bec4deea938c6b72538ff2ba6c877134058aa04` |
| `sidecar/hyprial-tailcat/internal/proto/message.go` | 28481 | `a8ba8bb3ccc5eb40c607646fa84400e713bbcd95a5fac48114e6221e7bc2e6d4` | `3d9238d44c119b0ae6cf65ad6db336b9462f71ad` |
| `src/hyprial/daemon/impl/application/netendpoints/endpoints.py` | 22890 | `aaa17a2bccc9db32022e58c2b8da0725d4d4c378b1a1c232003d9731f1e82b3d` | `8c07fe752ae35a2ae5f8fffdf8133fc713beeed9` |
| `src/hyprial/daemon/impl/application/netendpoints/forwarding.py` | 20226 | `354337ac54658c08456aa589ab31798df1990516c6a33cada6a1495cf266971c` | `6da91a5326a66dce928a5a0c5a9ffa087964268e` |
| `src/hyprial/daemon/impl/application/wiring/transport.py` | 42985 | `bde9772c96767673f17d48f630c3512e4308f75c79537190a0dacabd66782a3e` | `d5d71f0378580f5ab90fce55bf398e7e95fcbeff` |
| `src/hyprial/daemon/impl/configuration/network_profile.py` | 14640 | `3e35991bf7a1c706dd1934a6169463b18fbb80866184f63c769130ceb5d68d8d` | `73aa28c9afeeb36868c34ef02e9bcc1ff973ae46` |
| `src/hyprial/daemon/impl/forwarding/authority.py` | 13048 | `68efefafa500281555fc45291a0cbb055b9834a781828f354631f13ee5183b24` | `9a24c5e5fe855c181c7af7cf50c06b3bd564552b` |
| `src/hyprial/daemon/impl/forwarding/controller.py` | 39758 | `dc18207c106d88ebc126ef28c87a199f40583e2ac9fc49949787ba68cde00233` | `54bfc1315d32450cdd78bc3a6059982ff3cb7c7f` |
| `src/hyprial/daemon/impl/forwarding/policy.py` | 5950 | `56ff1156d0aa11b4eff03abd9e1843ee3d71913da26eeb164057ce2c35c1238d` | `ed1182cd942754f9d6937613e7c7b3264971e621` |
| `src/hyprial/daemon/impl/forwarding/slots/reconcile.py` | 9615 | `cfd7bc243742287c436cee99ddf2c9848e6b3cebd5e425bcffafd4ee34eeeabc` | `d0348683c0241b6bc73b1b471ae05cc0f17b2477` |
| `src/hyprial/daemon/impl/forwarding/supervisor.py` | 36191 | `4753030a7f7b8babae5904f4f2ba94b48014c51704d78ec51bd18b5fa265713b` | `956a1ac27df6c18fef73b547562876acf32d1311` |
| `src/hyprial/daemon/impl/forwarding_config.py` | 13000 | `ae9c61729f9849b701309bffbfec263aa7ebda9300f396789f4704351d2bff40` | `06af4795c60a9df160d079e6c625ce94cfc85e79` |
| `src/hyprial/daemon/impl/network/tailcat/keys.py` | 8917 | `214e2170aaae2d0c911cd2aab0d833642828042e882bd7e6357e8e000282fc60` | `a6a669acf07ce91489e99bef566491f226203db6` |
| `src/hyprial/daemon/impl/org/network/access/actor.py` | 15227 | `bd217a7501c7fed80e9dacce16210bcdc0f35e8e84ebfe518e92a5848ac5fb1b` | `a52cc562f7838a0c5d63de187b27101ab2c8415a` |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py` | 14878 | `3d34e78a861bc6eb14816414dab63a329d08917a2ad7145ee39e9af7b6514f4a` | `17ae02c6c0bca2169d978bf60f326d5ea5618368` |
| `src/hyprial/daemon/impl/org/network/admission/__init__.py` | 4190 | `d6ac35be9e0c142f3ecd120937add41fdd78938be0bac9fa46c04de3bca6c682` | `fa8bbee4d6411e382aa1a8222363d235da4dfa6a` |
| `src/hyprial/daemon/impl/org/network/casdoor/membership.py` | 12974 | `0bb21081428399fb5676da4c64a25f37d377b96b4a4595e9b46d1ecf05798722` | `11c2f5584232050ee19c8157f2987466f4ce1757` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py` | 21223 | `18e3280bb2c94ccb61720a50d9d1807ca88f1f81d19d6908392d4098cdf13bb7` | `22be18f4338d95d435b07ffc84210e520b847a6b` |
| `src/hyprial/daemon/impl/org/network/casdoor/webhook.py` | 8745 | `39b19f9a659e9db074d59f4641a6fe9468a6dae22b7453ff2d3b49df5aba475e` | `6449f47128bb8c82e19a1c3385211e21c9e44068` |
| `src/hyprial/daemon/impl/org/network/dialing/__init__.py` | 8821 | `70a5bf0e35738b028d07ce725451a07fb9141cef176ffed83774db5600e6e0ba` | `858066ca77ae15206d7c899d5fee1b0d4f73e06d` |
| `src/hyprial/daemon/impl/org/network/ipc.py` | 25120 | `cbcea8a61240b59e49405fb4bdb52f139aba3e336d912b102cdf18fdb7e32325` | `31f134aee9468a9a00c78a83efef71acd81a0271` |
| `src/hyprial/daemon/impl/org/network/policy.py` | 3860 | `07ab5c9298002076aeb055750b80ba1c90217ef63e966c074d335112e79985a4` | `041ac79e2711a04bce76edec5e28aa16f67fae8b` |
| `src/hyprial/daemon/impl/org/network/service.py` | 38065 | `0e86aff81b93d96e8b4abb3ceabcf1a38aa7c42a9980c3d25c9dbb2302b2f8c6` | `0cfe62a5d6672893598af143bf1118055cacbb36` |
| `src/hyprial/daemon/impl/org/network/state.py` | 13366 | `e960dbb18385c1a4ebc2cf0ce9c024064a12c6e851f52a6e85b4b49550ad27bf` | `d729f69b1247abfc0f68b9d2acaebae319812eb9` |
| `src/hyprial/daemon/impl/org/network/status/view.py` | 6559 | `40efd82f1802d0b44931759c19b34bf69f5a84d5fed048eac4e94eab87c81c2d` | `0f9aa2ae88d0c953c4ad6f9af2370ae43d09dd56` |
| `src/hyprial/daemon/impl/orgfs/composition/directory.py` | 40873 | `33ef20daf6dd70f003ea6683f32abac8c8f439f7ad69ea08757220c7d6b05229` | `ab78e2a53f21c8c8c90defacaeb8a7dcc7f2a9eb` |
| `src/hyprial/daemon/impl/orgfs/composition/lifecycle.py` | 30284 | `4b1225b19994df2310c492bbd2924fc2f1b06694502b3c0931d632d5242bba58` | `208d0c759745b28e3b369287ace32e7f6359d3f2` |
| `src/hyprial/daemon/impl/orgfs/mesh.py` | 16316 | `22b1a15f0f504e54ec480cd58ad362281bc7a0f548dd572e43d34dcd5be9ae0a` | `468c0d797ba387c2df7ce3044a9a6536041b5747` |
| `src/hyprial/daemon/impl/orgfs/network/ingress.py` | 10556 | `4c77d9ca8c6b2bb87b976177398d4e6729dffef6524b2be822ce6abf91ed9340` | `126ea6dada0ff1a52c046057695a050410081ad7` |
| `src/hyprial/daemon/impl/orgfs/network/queries.py` | 30949 | `5c9351e27ca9a738e4d7b0901460954745baff45c6161dca2985ef44f46fb8a8` | `a77328b702f6030dad3396cce97ad9d5c164b1bb` |
| `src/hyprial/daemon/impl/orgfs/network/recovery.py` | 34751 | `592db7758bc5cdca9fd5927f67fbd6d92f5fe06d51cf99e2d62cbaed3e1ced1d` | `5f0cba95b82ee0896e22caec5392d3e20aa2b0af` |
| `src/hyprial/daemon/impl/orgfs/network/sync.py` | 28153 | `5642fbf64f7d0ef785fdc8886db2f6428b801c4cc9f374cd458ea144397d0f42` | `65d75c1c0baf3054a940404c3b0a9063e2cb6339` |
| `src/hyprial/daemon/impl/orgfs/webserver/ipc.py` | 8192 | `f5c75a9ed1b2e69fd6d2f688ac66fb3c03c1bacd7546cd8eb88f4c9b9a88b180` | `4d192ab616464127d31813ffeb0ca8c5b8cc97e8` |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py` | 17804 | `82cd149cc88a50674f7e3e9a46c84dad0091a5dfa21af4e7de0ee3c1787d2431` | `7dc998e4d69e06aa74e39826b5d9dd1197b6479e` |
| `src/hyprial/daemon/impl/transport/zenoh/config.py` | 9458 | `b59fe0bfeb118343323d851619863c19a2041296b0c8a63cf98e42e364f96226` | `9609ad46e10456337fa9f240d549bdf90e3ea59e` |
| `src/hyprial/daemon/impl/transport/zenoh/transport.py` | 39823 | `fb055085271f1597b8f5e4afcf7a0125c8c0e8dcf670ed2d8639b40f4b6af7a0` | `d864fc708fb62a0335bc863a04847955d8376465` |
| `src/hyprial/identity/impl/device/__init__.py` | 2882 | `8b794405e07c155dde4027275fd6dcf0c37cd8740e122d71ec1d4dc33a266441` | `24ee739cc604313f521154ba31796bf5ed279ea6` |
| `src/hyprial/identity/impl/device/binding/__init__.py` | 768 | `7d2debd0c29314373bee5cab6dd96b94b2b13006055d0fb4f79f67b25b082eb0` | `0d542dc61b364a57e98d05f67ec78c2f0e468613` |
| `src/hyprial/identity/impl/device/binding/crypto.py` | 2496 | `9696090f9d08c2e48b070debcec029c96529e3488d0eeb636863ec5f9021631d` | `d8f31dce53473771c089ecaec2d7c0211557ff0b` |
| `src/hyprial/identity/impl/device/binding/extensions.py` | 749 | `778e3b38e3eb4a0daef6820f8baef15930bc964a500c45d4838c412cad2ccea3` | `b6072d66cf6defc139355dbb3442c2ebfde76017` |
| `src/hyprial/identity/impl/device/binding/jcs.py` | 2353 | `070a730355b51a6c832522158d7cd3831c4ed3386860ecd910b39365ce280e41` | `b390587c288ddd0a16e2feb361a6b5b204e0329f` |
| `src/hyprial/identity/impl/device/binding/profile.py` | 5855 | `40c345f51bfb13fb913e78b22b9f3b2f52fd5c50ad27f7f7f395563fbbe83b96` | `3a4bc5f597b3bfca41a6220062ce446f4715d1a5` |
| `src/hyprial/identity/impl/device/client/__init__.py` | 1078 | `1275c7aa1b4daff874c8a08c8d3b848919b973c9d3ed33cb66345625b3644729` | `1b12fbca92ba7c3a4bf9db2227ff9f7c73ec8288` |
| `src/hyprial/identity/impl/device/client/errors.py` | 3465 | `41cc833ac4716ad02bf7258f9b1f2c03ec06d9b3299eb28ff8a0e1f4117183a2` | `0e575de01941f191f633782053a69721cdeb751f` |
| `src/hyprial/identity/impl/device/client/http.py` | 26263 | `81ef082da9c171303b0f720991928816c0e24d5691bdf31b744cec8e3c2b0e33` | `09d9fa0328faf42c589adfd0f9a764df1b15ad71` |
| `src/hyprial/identity/impl/device/client/mtls.py` | 3147 | `9675526d2232c240f98017ec7b704a67abd53030e4ffe0ac65d44a3559f38ff7` | `1a29a5174f938c57a76c42a9186c02a1647d70c5` |
| `src/hyprial/identity/impl/device/models.py` | 6807 | `f072c18639c82501bc6ac34c47d4a6a0f182696c3b11ed8260c9e22fe0444258` | `b0bec3f2a6802c787c9c9ec4980f68f99b5154d1` |
| `src/hyprial/identity/impl/device/store.py` | 3994 | `6f672f97f324323c2ad815d51ac97795b94d2b23d544e97686acd96cda1ab554` | `358302c1afc4a72ea471dec9d7d0547c61358ab5` |
| `src/hyprial/identity/impl/device/transport_credential.py` | 36217 | `0f2dba2d5f898633e8c79a9b0e70350bac51c54e1be95e447d47005f22306820` | `853ac2d062f1da2a0301f3bab9274c99463d075e` |
| `src/hyprial/identity/impl/device/transport_storage.py` | 11994 | `f24183a84d68a22f3dc3e9ed055ce63fff0d66bd11a4bf3a0c108ec3006775a2` | `9ce884c321b26ebd66b1a681178b2f0a03606eb4` |
| `src/hyprial/identity/impl/org_directory/invite.py` | 12069 | `5955d79a26c8ec215a857930899bd8f1a5bbd4dd51799208eee8630167f5e996` | `e93ef740a28efec27ce1087f4bc0d4d30f5887a5` |
| `src/hyprial/identity/impl/org_directory/models.py` | 22974 | `b82c66c04d5fbdd16f1ecd0686484c336e20a05decb63b3ae7821249308776ce` | `c5da4628827ac0a26a19609c0bfa7fdf183c4ae2` |
| `src/hyprial/identity/interface.py` | 69639 | `a3544b08e2ba16a5ef6a712651fa13d1645e409d1a2f2239562ee1153f241de4` | `0e126469d3eb923785320aebf648ceb184e7774c` |
