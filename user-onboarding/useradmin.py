#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""useradmin.py — k8s 租户用户管理（创建 / 删除 / 列表）

为一名用户创建【独立命名空间 + 受限 kubeconfig】，做到两条硬隔离:
    · 只能操作自己的命名空间        —— 靠 namespace 级 Role（RBAC）
    · 工作负载只能落在 worker 节点  —— 靠 infra 污点 + ValidatingAdmissionPolicy

⚠️ 架构要点：真正的隔离【不在本脚本里】，而是集群侧的声明式资源:
      enforce-pool-isolation.yaml   （准入策略：挡 toleration/nodeName/nodeSelector 绕过）
      Node taint node-role.kubernetes.io/infra:NoSchedule
   本脚本只是"开账号"的便捷工具。换成 Go / shell / Terraform 都不影响安全性 ——
   所以不要试图用本脚本自身去"限制"用户，它没有那个能力，也不该有。

依赖: kubernetes>=29 （见 requirements.txt；用 ./setup-venv.sh 准备）

用法:
    ./venv/bin/python useradmin.py create <username> [选项]
    ./venv/bin/python useradmin.py delete <username> [--yes]
    ./venv/bin/python useradmin.py list
    ./venv/bin/python useradmin.py verify <username>     # 复查既有用户的边界
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from pathlib import Path

try:
    from kubernetes import client, config as k8sconfig
    from kubernetes.client.rest import ApiException
except ImportError:  # pragma: no cover
    sys.exit(
        "缺少 kubernetes 库。先运行：\n"
        "    ./setup-venv.sh\n"
        "然后用 ./venv/bin/python 调用本脚本。"
    )

# ---------------------------------------------------------------- 常量
SA_NAME = "tenant-user"
ROLE_NAME = "tenant-developer"
TOKEN_SECRET_SUFFIX = "-token"
MANAGED_BY = "user-onboarding"
TENANT_LABEL = "tenant"
EXEMPT_LABEL = "pool-access"          # =privileged 的 ns 不受池隔离策略管辖
EXEMPT_VALUE = "privileged"
INFRA_KEY = "node-role.kubernetes.io/infra"

DEFAULT_SERVER = "https://114.67.232.32:6443"
DEFAULT_CA = "/etc/kubeasz/clusters/k8s-baremetal-01/ssl/ca.pem"

# 新用户的默认配额（可按业务在命令行覆盖；传 0 = 该维度不限制）
DEFAULT_QUOTA_CPU = "4"
DEFAULT_QUOTA_MEM = "8Gi"
DEFAULT_QUOTA_PODS = "20"
DEFAULT_QUOTA_STORAGE = "50Gi"
DEFAULT_LR_MAX_CPU = "4"
DEFAULT_LR_MAX_MEM = "8Gi"


# ---------------------------------------------------------------- 输出
class Log:
    @staticmethod
    def step(msg):  print(f"\033[36m==>\033[0m {msg}")
    @staticmethod
    def ok(msg):    print(f"  \033[32m✓\033[0m {msg}")
    @staticmethod
    def warn(msg):  print(f"  \033[33m!\033[0m {msg}")
    @staticmethod
    def err(msg):   print(f"  \033[31m✗\033[0m {msg}", file=sys.stderr)


# ---------------------------------------------------------------- 客户端
class Admin:
    """持有各类 API 客户端。"""

    def __init__(self, kubeconfig: str | None = None, context: str | None = None):
        try:
            if kubeconfig:
                k8sconfig.load_kube_config(config_file=kubeconfig, context=context)
            else:
                try:
                    k8sconfig.load_incluster_config()
                except k8sconfig.ConfigException:
                    k8sconfig.load_kube_config(context=context)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"无法加载 kube 配置: {exc}")

        self.core = client.CoreV1Api()
        self.rbac = client.RbacAuthorizationV1Api()
        self.authz = client.AuthorizationV1Api()
        self.apps = client.AppsV1Api()

    # --- 幂等：create 成功=created；409=已存在 → patch 收敛到期望态 ---
    @staticmethod
    def _ensure(kind: str, name: str, create, patch, body) -> str:
        try:
            create(body)
            Log.ok(f"{kind}/{name} 已创建")
            return "created"
        except ApiException as exc:
            if exc.status != 409:
                raise
            patch(name, body)
            Log.ok(f"{kind}/{name} 已更新（幂等收敛）")
            return "configured"


# ---------------------------------------------------------------- 资源定义
def ns_body(name: str, user: str, pod_security: str, kubeconfig_path: str | None = None):
    labels = {
        TENANT_LABEL: user,
        "managed-by": MANAGED_BY,
        # 刻意不打 pool-access=privileged → 自动纳入池隔离策略管辖
        "pod-security.kubernetes.io/enforce": pod_security,
        "pod-security.kubernetes.io/enforce-version": "latest",
        "pod-security.kubernetes.io/audit": pod_security,
        "pod-security.kubernetes.io/warn": pod_security,
    }
    # 把 kubeconfig 的落盘路径记在注解里 ——
    # 这样 `delete` 能据此清掉文件，不必要求操作者记得当初的 --out。
    annotations = {}
    if kubeconfig_path:
        annotations["user-onboarding/kubeconfig-path"] = kubeconfig_path
    return client.V1Namespace(
        metadata=client.V1ObjectMeta(name=name, labels=labels, annotations=annotations)
    )


def sa_body(ns: str, user: str):
    return client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(name=SA_NAME, namespace=ns, labels={TENANT_LABEL: user})
    )


def role_body(ns: str, user: str):
    """租户 Role —— 只在本 ns 内，且刻意不含任何提权/集群级权限。"""

    def rule(groups, resources, verbs):
        return client.V1PolicyRule(api_groups=groups, resources=resources, verbs=verbs)

    rw = ["get", "list", "watch", "create", "update", "patch", "delete", "deletecollection"]
    ro = ["get", "list", "watch"]
    return client.V1Role(
        metadata=client.V1ObjectMeta(name=ROLE_NAME, namespace=ns, labels={TENANT_LABEL: user}),
        rules=[
            # 核心资源
            rule([""], [
                "pods", "pods/log", "pods/exec", "pods/portforward", "pods/attach",
                "services", "endpoints", "configmaps", "secrets",
                "persistentvolumeclaims", "serviceaccounts", "events",
                "replicationcontrollers",
            ], rw),
            # 控制器
            rule(["apps"], ["deployments", "statefulsets", "daemonsets", "replicasets"], rw),
            # 批处理
            rule(["batch"], ["jobs", "cronjobs"], rw),
            # 网络
            rule(["networking.k8s.io"], ["ingresses", "networkpolicies"], rw),
            # 弹性伸缩
            rule(["autoscaling"], ["horizontalpodautoscalers"],
                 ["get", "list", "watch", "create", "update", "patch", "delete"]),
            # 用量指标
            rule(["metrics.k8s.io"], ["pods"], ["get", "list"]),
            # 配额/默认值【只读】—— 让用户能自查用量，但改不了（无法自行调高配额）
            rule([""], ["resourcequotas", "limitranges"], ro),
            #
            # 刻意不授予（防越权）:
            #   roles / rolebindings                 —— 防止 ns 内提权
            #   resourcequotas/limitranges 的写权限  —— 防止自行调高配额
            #   namespaces / nodes / persistentvolumes —— 集群级资源
        ],
    )


def rolebinding_body(ns: str, user: str):
    return client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=ROLE_NAME, namespace=ns, labels={TENANT_LABEL: user}),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io", kind="Role", name=ROLE_NAME
        ),
        subjects=[client.RbacV1Subject(kind="ServiceAccount", name=SA_NAME, namespace=ns)],
    )


def build_quota_hard(cpu: str, mem: str, pods: str, storage: str) -> dict:
    """构造 ResourceQuota 的 hard 字典。

    值 "0" 或空串表示【不限制】，该项直接不写进 hard ——
    而不是"限制为 0"（那会让该 ns 一个 Pod 都起不来）。

    四项全为 0/空 → 返回 {}，调用方据此跳过整个 ResourceQuota。
    """
    hard: dict[str, str] = {}
    if cpu not in ("", "0"):
        # requests 与 limits 同值：确保用户的 Pod 两类都能被配额核算
        hard["requests.cpu"] = hard["limits.cpu"] = cpu
    if mem not in ("", "0"):
        hard["requests.memory"] = hard["limits.memory"] = mem
    if pods not in ("", "0"):
        hard["pods"] = pods
    if storage not in ("", "0"):
        hard["requests.storage"] = storage
    return hard


def quota_body(ns: str, user: str, hard: dict):
    return client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(name="tenant-quota", namespace=ns, labels={TENANT_LABEL: user}),
        spec=client.V1ResourceQuotaSpec(hard=hard),
    )


def describe_quota(hard: dict) -> str:
    """把 hard 字典渲染成一行摘要，供日志/用户说明使用。"""
    if not hard:
        return "不限"
    parts = []
    if "pods" in hard:
        parts.append(f"Pod {hard['pods']} 个")
    if "requests.cpu" in hard:
        parts.append(f"CPU {hard['requests.cpu']}")
    if "requests.memory" in hard:
        parts.append(f"内存 {hard['requests.memory']}")
    if "requests.storage" in hard:
        parts.append(f"存储 {hard['requests.storage']}")
    return " / ".join(parts)


def limitrange_body(ns: str, user: str, max_cpu: str, max_mem: str):
    """LimitRange —— 默认创建（用 --no-limitrange 关闭）。

    它做两件事：
      1. 给未写 resources 的容器自动补 default/defaultRequest
         → Pod 不再是 BestEffort（BestEffort 在节点压力时最先被驱逐），
           也避免被配额以"缺少 requests/limits"为由拒绝
      2. 设单容器上限 max

    ⚠️ 与配额的关系：设了 CPU/内存【配额】却【没有】LimitRange 时，
    未写 resources 的 Pod 会被配额直接拒绝（配额无法核算用量）——
    这是租户最容易撞的墙。所以默认建它。
    """
    return client.V1LimitRange(
        metadata=client.V1ObjectMeta(name="tenant-defaults", namespace=ns,
                                     labels={TENANT_LABEL: user}),
        spec=client.V1LimitRangeSpec(limits=[
            client.V1LimitRangeItem(
                type="Container",
                default={"cpu": "500m", "memory": "512Mi"},
                default_request={"cpu": "100m", "memory": "128Mi"},
                max={"cpu": max_cpu, "memory": max_mem},
                min={"cpu": "10m", "memory": "16Mi"},
            ),
            client.V1LimitRangeItem(type="PersistentVolumeClaim", min={"storage": "1Gi"}),
        ]),
    )


# 配额维度 → hard 里的键。requests/limits 同值，保证两类都能被核算。
QUOTA_KEYS = {
    "cpu": ("requests.cpu", "limits.cpu"),
    "mem": ("requests.memory", "limits.memory"),
    "pods": ("pods",),
    "storage": ("requests.storage",),
}
QUOTA_NAME = "tenant-quota"
LIMITRANGE_NAME = "tenant-defaults"


def cur_quota_hard(admin: "Admin", ns: str) -> dict | None:
    """读现有配额 hard；不存在返回 None（区别于"存在但为空"）。"""
    try:
        q = admin.core.read_namespaced_resource_quota(QUOTA_NAME, ns)
        return dict(q.spec.hard or {})
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def cur_limitrange(admin: "Admin", ns: str):
    try:
        return admin.core.read_namespaced_limit_range(LIMITRANGE_NAME, ns)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def apply_quota(admin: "Admin", ns: str, user: str, desired: dict) -> str:
    """把 ns 的 tenant-quota 收敛到 desired，返回动作名。

    desired 为空 dict → 表示"不限制"，会【删除】整个 ResourceQuota。
    否则用 merge-patch 收敛：desired 里没有的键显式置 null 来移除 ——
    这一点很关键：默认的 strategic-merge 对 map 是【合并】而非替换，
    直接 patch 会把已删掉的旧键留在原地，导致"改了没生效"。
    """
    cur = cur_quota_hard(admin, ns)

    if not desired:
        if cur is None:
            return "unchanged"
        admin.core.delete_namespaced_resource_quota(QUOTA_NAME, ns)
        return "deleted"

    if cur is None:
        admin.core.create_namespaced_resource_quota(ns, quota_body(ns, user, desired))
        return "created"

    patch = {k: v for k, v in desired.items() if cur.get(k) != v}
    patch.update({k: None for k in cur if k not in desired})
    if not patch:
        return "unchanged"

    admin.core.patch_namespaced_resource_quota(
        QUOTA_NAME, ns, {"spec": {"hard": patch}},
        _content_type="application/merge-patch+json",
    )
    return "patched"


def apply_limitrange(admin: "Admin", ns: str, user: str,
                     enabled: bool, max_cpu: str, max_mem: str) -> str:
    """创建/更新（enabled=True）或删除（False）LimitRange。"""
    cur = cur_limitrange(admin, ns)
    if not enabled:
        if cur is None:
            return "unchanged"
        admin.core.delete_namespaced_limit_range(LIMITRANGE_NAME, ns)
        return "deleted"

    body = limitrange_body(ns, user, max_cpu, max_mem)
    if cur is None:
        admin.core.create_namespaced_limit_range(ns, body)
        return "created"

    # 比对新旧 max，避免无谓 patch
    old = {i.type: (i.max or {}) for i in (cur.spec.limits or [])}
    if old.get("Container") == {"cpu": max_cpu, "memory": max_mem}:
        return "unchanged"
    admin.core.patch_namespaced_limit_range(LIMITRANGE_NAME, ns, body,
                                            _content_type="application/merge-patch+json")
    return "patched"


def token_secret_body(ns: str, user: str, secret_name: str):
    """长期 token —— 用 legacy SA token Secret。

    为什么不用 TokenRequest（1.24+ 推荐）:
        TokenRequest 签发的 token 有【过期时间】（默认 1h），需要客户端定期轮换。
        而本场景是"把 kubeconfig 发给一个人长期使用"，轮换不现实。
        legacy Secret 由 kube-controller-manager 签发且不过期，正合适。
    """
    return client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            namespace=ns,
            labels={TENANT_LABEL: user},
            annotations={"kubernetes.io/service-account.name": SA_NAME},
        ),
        type="kubernetes.io/service-account-token",
    )


# ---------------------------------------------------------------- 工具函数
def wait_for_token(admin: Admin, ns: str, secret_name: str, timeout: int = 30) -> str:
    """等 controller-manager 把 token 填进 Secret。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            sec = admin.core.read_namespaced_secret(secret_name, ns)
            raw = (sec.data or {}).get("token")
            if raw:
                return base64.b64decode(raw).decode()
            last = "secret 存在但 token 字段为空"
        except ApiException as exc:
            if exc.status != 404:
                raise
            last = "secret 尚未创建"
        time.sleep(1)
    raise RuntimeError(f"token 未在 {timeout}s 内生成（{last}）—— "
                       f"检查 kube-controller-manager 的 service-account-token 控制器")


def sar_allowed(admin: Admin, user: str, verb: str, resource: str,
                namespace: str | None = None, group: str = "") -> bool:
    """以 user 的身份做鉴权判定（SubjectAccessReview）。"""
    spec = client.V1SubjectAccessReviewSpec(
        user=user,
        resource_attributes=client.V1ResourceAttributes(
            verb=verb, resource=resource, group=group, namespace=namespace
        ),
    )
    res = admin.authz.create_subject_access_review(client.V1SubjectAccessReview(spec=spec))
    return bool(res.status.allowed)


def render_kubeconfig(server, ca_b64, ns, user, token) -> str:
    return f"""apiVersion: v1
kind: Config
clusters:
  - name: k8s-baremetal-01
    cluster:
      server: {server}
      certificate-authority-data: {ca_b64}
contexts:
  - name: {ns}
    context:
      cluster: k8s-baremetal-01
      user: {user}
      namespace: {ns}
current-context: {ns}
users:
  - name: {user}
    user:
      token: {token}
preferences: {{}}
"""


# ---------------------------------------------------------------- create
def cmd_create(args) -> int:
    user, ns = args.username, args.namespace or args.username
    out = Path(args.out or f"./{user}.kubeconfig")
    secret_name = f"{user}{TOKEN_SECRET_SUFFIX}"

    Log.step(f"目标: 用户={user}  命名空间={ns}  API={args.server}")
    if args.dry_run:
        Log.warn("DRY-RUN：不产生任何变更")

    admin = Admin(args.kubeconfig, args.context)

    # ---- 前置：确认池隔离策略在位 --------------------------------
    # 没有它，用户可以用 toleration/nodeName 把 Pod 塞进 master。
    Log.step("0/8 前置检查：池隔离准入策略")
    policy_ok = True
    try:
        vap_api = client.AdmissionregistrationV1Api()
        names = {p.metadata.name for p in vap_api.list_validating_admission_policy().items}
        need = {"tenant-pool-guard-pods", "tenant-pool-guard-workloads"}
        missing = need - names
        if missing:
            policy_ok = False
            Log.warn(f"缺少准入策略 {sorted(missing)} —— 用户可能绕过污点进入 master！")
            Log.warn("请先执行: kubectl apply -f enforce-pool-isolation.yaml")
        else:
            Log.ok("池隔离策略已就位")
    except ApiException as exc:
        policy_ok = False
        Log.warn(f"无法查询准入策略（{exc.status}）—— 跳过检查")
    if not policy_ok and not args.allow_unprotected:
        Log.err("已中止。确认风险后可用 --allow-unprotected 强制继续。")
        return 2

    # ---- 1) Namespace -------------------------------------------
    Log.step("1/8 命名空间")
    if not args.dry_run:
        Admin._ensure("Namespace", ns,
                      lambda b: admin.core.create_namespace(b),
                      lambda n, b: admin.core.patch_namespace(n, b),
                      ns_body(ns, user, args.pod_security, str(out.resolve())))
    else:
        print(f"    [dry-run] Namespace/{ns}")

    # ---- 2) ServiceAccount --------------------------------------
    Log.step("2/8 ServiceAccount")
    if not args.dry_run:
        Admin._ensure("ServiceAccount", SA_NAME,
                      lambda b: admin.core.create_namespaced_service_account(ns, b),
                      lambda n, b: admin.core.patch_namespaced_service_account(n, ns, b),
                      sa_body(ns, user))

    # ---- 3) Role ------------------------------------------------
    Log.step("3/8 Role（仅本 ns；不含 RBAC / Node / Namespace 权限）")
    if not args.dry_run:
        Admin._ensure("Role", ROLE_NAME,
                      lambda b: admin.rbac.create_namespaced_role(ns, b),
                      lambda n, b: admin.rbac.patch_namespaced_role(n, ns, b),
                      role_body(ns, user))

    # ---- 4) RoleBinding -----------------------------------------
    Log.step("4/8 RoleBinding")
    if not args.dry_run:
        Admin._ensure("RoleBinding", ROLE_NAME,
                      lambda b: admin.rbac.create_namespaced_role_binding(ns, b),
                      lambda n, b: admin.rbac.patch_namespaced_role_binding(n, ns, b),
                      rolebinding_body(ns, user))

    # ---- 5) Quota + LimitRange ----
    Log.step("5/8 配额与限制")
    hard = build_quota_hard(args.quota_cpu, args.quota_mem,
                            args.quota_pods, args.quota_storage)
    if args.dry_run:
        print(f"    [dry-run] ResourceQuota/{QUOTA_NAME} hard={hard or '(不建：不限制)'}")
        print(f"    [dry-run] LimitRange/{LIMITRANGE_NAME}"
              + (f" max={args.limitrange_max_cpu}/{args.limitrange_max_mem}"
                 if args.limitrange else "  (不建)"))
    else:
        act = apply_quota(admin, ns, user, hard)
        Log.ok(f"ResourceQuota/{QUOTA_NAME}: {act}"
               + (f"  {describe_quota(hard)}" if hard else "  （不限制）"))
        lact = apply_limitrange(admin, ns, user, args.limitrange,
                                args.limitrange_max_cpu, args.limitrange_max_mem)
        Log.ok(f"LimitRange/{LIMITRANGE_NAME}: {lact}"
               + (f"  max={args.limitrange_max_cpu}/{args.limitrange_max_mem}"
                  if args.limitrange else "  （不创建）"))

        if hard and not args.limitrange:
            Log.warn("设了 CPU/内存配额但未建 LimitRange：租户的 Pod 必须【显式写 resources】，"
                     "否则会被配额拒绝。要自动补默认值就加 --limitrange")

    # ---- 6) token Secret ----------------------------------------
    Log.step("6/8 长期 token Secret")
    token = "<dry-run>"
    if not args.dry_run:
        Admin._ensure("Secret", secret_name,
                      lambda b: admin.core.create_namespaced_secret(ns, b),
                      lambda n, b: admin.core.patch_namespaced_secret(n, ns, b),
                      token_secret_body(ns, user, secret_name))
        try:
            token = wait_for_token(admin, ns, secret_name)
            Log.ok(f"token 已就绪（{len(token)} 字节）")
        except RuntimeError as exc:
            Log.err(str(exc))
            return 1

    # ---- 7) kubeconfig ------------------------------------------
    Log.step(f"7/8 生成 kubeconfig → {out}")
    if args.dry_run:
        print(f"    [dry-run] 将写入 {out}")
    else:
        try:
            ca_pem = Path(args.ca_file).read_bytes()
        except OSError as exc:
            Log.err(f"读不到 CA 文件 {args.ca_file}: {exc}")
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        content = render_kubeconfig(args.server, base64.b64encode(ca_pem).decode(),
                                   ns, user, token)
        # 先以 0600 创建再写入，避免权限窗口
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(out, 0o600)
        Log.ok("已写入（权限 600）")

    # ---- 8) 自检 -------------------------------------------------
    Log.step("8/8 自检：验证权限边界")
    if args.dry_run:
        print("    [dry-run] 跳过")
    else:
        if not verify_user(admin, ns, verbose=True):
            Log.err("自检未通过 —— 请检查 Role 与准入策略，然后再交付用户")
            return 1

    if hard:
        parts = []
        if "pods" in hard:
            parts.append(f"Pod {hard['pods']} 个")
        if "requests.cpu" in hard:
            parts.append(f"CPU {hard['requests.cpu']}")
        if "requests.memory" in hard:
            parts.append(f"内存 {hard['requests.memory']}")
        if "requests.storage" in hard:
            parts.append(f"存储 {hard['requests.storage']}")
        quota_note = " / ".join(parts)
    else:
        quota_note = "不限"

    print_user_guide(user, ns, out, args.server, quota_note,
                     limitrange=args.limitrange,
                     lr_max=f"{args.limitrange_max_cpu}/{args.limitrange_max_mem}"
                            if args.limitrange else None)
    return 0


# ---------------------------------------------------------------- verify
def verify_user(admin: Admin, ns: str, verbose: bool = True) -> bool:
    """验证隔离边界。返回 True 表示全部符合预期。"""
    subject = f"system:serviceaccount:{ns}:{SA_NAME}"
    passed = failed = 0

    def check(desc, cond, fatal=True):
        nonlocal passed, failed
        if cond:
            if verbose: Log.ok(desc)
            passed += 1
        else:
            if verbose:
                (Log.err if fatal else Log.warn)(desc)
            failed += 1

    def expect_allow(desc, *, fatal=True, timeout=15, **sar_kwargs):
        """正向断言（期望允许），带重试。sar_kwargs 原样转给 sar_allowed。

        kube-apiserver 的 RBAC authorizer 走 informer 缓存：RoleBinding 刚创建时
        缓存尚未同步，第一次 SubjectAccessReview 可能返回 denied。这是【最终一致】
        而非配置错误 —— 必须重试到 timeout，不能直接判失败。
        （反向断言无需重试：缓存滞后只会造成假拒绝，不会造成假允许。）
        """
        deadline = time.time() + timeout
        allowed = False
        while True:
            allowed = sar_allowed(admin, subject, **sar_kwargs)
            if allowed or time.time() >= deadline:
                break
            time.sleep(1)
        check(desc, allowed, fatal=fatal)

    if verbose: Log.step(f"RBAC（以 {subject} 身份）")
    # ⚠️ resource 必须带对的 API group：
    #   pods / pvc / quota        → core（group=""）
    #   deployments / statefulsets → apps
    #   忘带 group 时 SAR 查询的是 core 组里同名资源（不存在）→ 必然 denied，
    #   会被误读成"权限没生效"。所以一律用关键字参数写清楚。
    expect_allow("正例：可在本 ns 创建 deployments",
                 verb="create", resource="deployments", namespace=ns, group="apps")
    expect_allow("正例：可在本 ns 创建 statefulsets",
                 verb="create", resource="statefulsets", namespace=ns, group="apps")
    expect_allow("正例：可在本 ns 读 pods",
                 verb="get", resource="pods", namespace=ns)
    expect_allow("正例：可在本 ns 建 PVC",
                 verb="create", resource="persistentvolumeclaims", namespace=ns)
    # 反例：跨 ns
    check("反例：不可访问 kube-system",
          not sar_allowed(admin, subject, verb="get", resource="pods", namespace="kube-system"))
    check("反例：不可访问 default",
          not sar_allowed(admin, subject, verb="get", resource="pods", namespace="default"))
    # 反例：集群级
    check("反例：不可查看 nodes",
          not sar_allowed(admin, subject, verb="get", resource="nodes"))
    check("反例：不可列出 namespaces",
          not sar_allowed(admin, subject, verb="list", resource="namespaces"))
    check("反例：不可查看 persistentvolumes",
          not sar_allowed(admin, subject, verb="get", resource="persistentvolumes"))
    check("反例：不可查看 storageclasses",
          not sar_allowed(admin, subject, verb="get", resource="storageclasses",
                          group="storage.k8s.io"))
    # 反例：ns 内提权
    check("反例：不可创建 Role（防提权）",
          not sar_allowed(admin, subject, verb="create", resource="roles",
                          namespace=ns, group="rbac.authorization.k8s.io"))
    check("反例：不可创建 RoleBinding",
          not sar_allowed(admin, subject, verb="create", resource="rolebindings",
                          namespace=ns, group="rbac.authorization.k8s.io"))
    check("反例：不可修改 ResourceQuota",
          not sar_allowed(admin, subject, verb="patch", resource="resourcequotas",
                          namespace=ns))

    # 准入层：策略按【命名空间】匹配，所以用 admin 在用户 ns 里造的探针
    # 同样会被策略校验 —— 足以证明该 ns 受管辖。
    #
    # ⚠️ 探针的两条工程约束（踩过坑）：
    #   1. Pod 名必须【唯一】—— 用固定名时，上一轮的 Pod 可能仍在优雅删除中
    #      （默认 30s），此时 create 会撞 409 Conflict，被误判成"调度失败"。
    #   2. 删除必须 grace_period_seconds=0 —— 否则 Pod 带 deletionTimestamp 残留，
    #      影响下一轮；namespace 里也会堆积陈旧对象。
    if verbose: Log.step("准入策略（infra 池隔离）")
    suffix = f"{os.getpid()}-{int(time.time()) % 100000}"

    def force_del(name):
        try:
            admin.core.delete_namespaced_pod(
                name, ns, grace_period_seconds=0, body=client.V1DeleteOptions())
        except ApiException:
            pass

    def cleanup_stale_probes():
        """清掉本轮之前遗留的探针（按前缀匹配，只删本工具造的）。"""
        try:
            pods = admin.core.list_namespaced_pod(ns)
        except ApiException:
            return
        for p in pods.items:
            nm = p.metadata.name
            if nm.startswith("onboarding-") and ("probe" in nm or "verify" in nm):
                force_del(nm)

    cleanup_stale_probes()

    # --- 反例：带 infra 容忍的 Pod 必须被拒 ---
    deny_pod = f"onboarding-verify-deny-{suffix}"
    body = client.V1Pod(
        metadata=client.V1ObjectMeta(name=deny_pod, namespace=ns),
        spec=client.V1PodSpec(
            restart_policy="Never",
            node_selector={INFRA_KEY: ""},
            tolerations=[client.V1Toleration(key=INFRA_KEY, operator="Exists",
                                             effect="NoSchedule")],
            containers=[client.V1Container(
                name="c", image="easzlab.io.local:5000/easzlab/pause:3.10")],
        ),
    )
    denied = False
    try:
        admin.core.create_namespaced_pod(ns, body)
        # 没被拒 → 说明策略缺失；立即清掉并判失败
        force_del(deny_pod)
    except ApiException as exc:
        msg = (exc.body or "") + str(exc.reason or "")
        denied = ("禁止" in msg) or ("tenant-pool-guard" in msg) or exc.status in (400, 422)
    check("反例：带 infra 容忍的 Pod 被拒绝", denied)

    # --- 正例：无容忍的普通 Pod 能调度，且落在 worker ---
    ok_pod = f"onboarding-verify-ok-{suffix}"
    worker = None
    created = False
    try:
        admin.core.create_namespaced_pod(ns, client.V1Pod(
            metadata=client.V1ObjectMeta(name=ok_pod, namespace=ns),
            spec=client.V1PodSpec(
                restart_policy="Never",
                containers=[client.V1Container(
                    name="c", image="easzlab.io.local:5000/easzlab/pause:3.10")],
            ),
        ))
        created = True
        # 首次调度通常 <10s；给 40s 容忍镜像拉取等慢路径
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                p = admin.core.read_namespaced_pod(ok_pod, ns)
            except ApiException as exc:
                if exc.status == 404:      # 被删了（不该发生）
                    break
                raise
            if p.spec.node_name:
                worker = p.spec.node_name
                break
            time.sleep(1)
    except ApiException as exc:
        Log.warn(f"探针 Pod 创建失败: {exc.status} {exc.reason}")
        if exc.body:
            print(f"      {str(exc.body)[:200]}")
    finally:
        if created:
            force_del(ok_pod)

    check("正例：普通 Pod 能正常调度", worker is not None, fatal=False)
    if worker:
        check("正例：且落在 worker 节点（非 master）",
              worker.startswith("k8s-worker"), fatal=False)
        if verbose:
            print(f"      调度到: {worker}")

    if verbose:
        print()
        print(f"  自检结果: 通过 {passed} 项，失败 {failed} 项")
    return failed == 0


def cmd_verify(args) -> int:
    ns = args.namespace or args.username
    admin = Admin(args.kubeconfig, args.context)
    return 0 if verify_user(admin, ns) else 1


# ---------------------------------------------------------------- delete
def cmd_delete(args) -> int:
    user, ns = args.username, args.namespace or args.username
    admin = Admin(args.kubeconfig, args.context)

    # 确认是本工具建的，防止误删手建命名空间
    recorded_kcfg: str | None = None
    try:
        got = admin.core.read_namespace(ns)
        recorded_kcfg = ((got.metadata.annotations or {})
                         .get("user-onboarding/kubeconfig-path"))
    except ApiException as exc:
        if exc.status != 404:
            raise
        Log.warn(f"命名空间 {ns} 不存在（继续清理 kubeconfig 文件）")
        got = None

    if got is not None:
        labels = got.metadata.labels or {}
        if labels.get("managed-by") != MANAGED_BY and not args.yes:
            Log.warn(f"命名空间 {ns} 缺 managed-by={MANAGED_BY} 标签（labels={labels}）")
            if input("  仍要继续删除？输入 yes 确认: ").strip() != "yes":
                print("已取消")
                return 1
        elif not args.yes:
            Log.warn(f"即将删除命名空间 {ns} —— 其中所有资源（含 PVC 及其底层数据卷）都会被销毁")
            if input("  输入 yes 确认: ").strip() != "yes":
                print("已取消")
                return 1

        Log.step(f"删除命名空间 {ns}")
        admin.core.delete_namespace(ns)
        Log.ok("已发出删除请求（PVC 清理是异步的，ns 可能短暂处于 Terminating）")

    # 优先用 ns 注解里记录的路径；再叠加 --out 与默认路径
    cands = {Path(f"./{user}.kubeconfig")}
    if args.out:
        cands.add(Path(args.out))
    if recorded_kcfg:
        cands.add(Path(recorded_kcfg))
    for cand in cands:
        if cand.is_file():
            if args.keep_kubeconfig:
                Log.warn(f"保留 kubeconfig: {cand}（其中 token 已随 SA 删除而失效）")
            else:
                cand.unlink()
                Log.ok(f"已删除 {cand}")

    print("\n权限已撤销。用户手上的 kubeconfig 即使保留也【无法再认证】——")
    print("其 token 绑定的 ServiceAccount 随命名空间一起被删除。")
    return 0


# ---------------------------------------------------------------- quota（创建后修改）
def cmd_quota(args) -> int:
    """查看 / 修改已有用户的配额与 LimitRange。

    语义（关键）：
      · 【只改动显式传入的维度】—— 没传的维度保持原值。这样调整单项不必重述全部。
      · 传 0（或空串）表示【取消该维度限制】（从 hard 里删除该键）。
      · 四个维度全部为 0 → 整个 ResourceQuota 被删除（回到"不限制"）。
      · --show 只读展示，不做任何变更。
    """
    user, ns = args.username, args.namespace or args.username
    admin = Admin(args.kubeconfig, args.context)

    # 确认用户存在（本工具的 ns 才有 managed-by 标签）
    try:
        got = admin.core.read_namespace(ns)
    except ApiException as exc:
        if exc.status == 404:
            Log.err(f"命名空间 {ns} 不存在。先用 create 建用户，或检查 --namespace。")
            return 1
        raise

    def show():
        """每次都重新读 —— 修改后再展示必须反映最新状态，不能用变更前读到的缓存。"""
        cur_now = cur_quota_hard(admin, ns)
        lr_now = cur_limitrange(admin, ns)
        print(f"用户 {user}（ns {ns}）当前限制：")
        if cur_now is None:
            print("  ResourceQuota : 不存在（该 ns 资源不受限）")
        else:
            print(f"  ResourceQuota : {describe_quota(cur_now)}")
            for k in sorted(cur_now):
                print(f"                    {k} = {cur_now[k]}")
        if lr_now is None:
            print("  LimitRange    : 不存在")
        else:
            for item in lr_now.spec.limits or []:
                if item.type == "Container":
                    print(f"  LimitRange    : 单容器 max={item.max}, "
                          f"default={item.default}, defaultRequest={item.default_request}")
                else:
                    print(f"  LimitRange    : {item.type} min={item.min}")
        try:
            q = admin.core.read_namespaced_resource_quota(QUOTA_NAME, ns)
            print(f"  当前用量      : {q.status.used or {}}")
        except ApiException:
            pass

    cur = cur_quota_hard(admin, ns)
    lr = cur_limitrange(admin, ns)

    if args.show:
        show()
        return 0

    # 计算新的 hard：以现有为基线，只覆盖显式传入的维度
    new_hard = dict(cur or {})
    changed = []
    for dim, val in (("cpu", args.quota_cpu), ("mem", args.quota_mem),
                     ("pods", args.quota_pods), ("storage", args.quota_storage)):
        if val is None:
            continue                      # 未指定 → 保持原值
        keys = QUOTA_KEYS[dim]
        if val in ("", "0"):
            for k in keys:
                if k in new_hard:
                    del new_hard[k]
                    changed.append(f"{dim}=取消限制")
        else:
            for k in keys:
                if new_hard.get(k) != val:
                    new_hard[k] = val
                    changed.append(f"{dim}={val}")

    # LimitRange：--limitrange / --no-limitrange / 仅调 max
    lr_enabled = args.limitrange
    if lr_enabled is None:
        lr_enabled = lr is not None        # 未指定 → 保持现状
    lr_max_cpu = args.limitrange_max_cpu or DEFAULT_LR_MAX_CPU
    lr_max_mem = args.limitrange_max_mem or DEFAULT_LR_MAX_MEM
    if lr_enabled and lr is not None and args.limitrange_max_cpu is None \
            and args.limitrange_max_mem is None:
        # 未显式给 max → 沿用现有值，避免把用户设过的上限悄悄改回默认
        for item in lr.spec.limits or []:
            if item.type == "Container" and item.max:
                lr_max_cpu = item.max.get("cpu", lr_max_cpu)
                lr_max_mem = item.max.get("memory", lr_max_mem)

    if args.dry_run:
        print(f"[dry-run] 用户 {user}（ns {ns}）")
        print(f"  ResourceQuota 目标: {new_hard or '(删除：不限制)'}")
        print(f"  LimitRange   目标: {'max=%s/%s' % (lr_max_cpu, lr_max_mem) if lr_enabled else '（删除）'}")
        return 0

    if not changed and (lr is not None) == lr_enabled \
            and not (lr_enabled and (args.limitrange_max_cpu or args.limitrange_max_mem)):
        Log.ok("无变更（未指定任何要修改的维度）")
        show()
        return 0

    Log.step(f"更新 用户={user}  ns={ns}")
    act = apply_quota(admin, ns, user, new_hard)
    Log.ok(f"ResourceQuota: {act}"
           + (f"  {describe_quota(new_hard)}" if new_hard else "  （已删除 → 不限制）"))
    lact = apply_limitrange(admin, ns, user, lr_enabled, lr_max_cpu, lr_max_mem)
    Log.ok(f"LimitRange: {lact}"
           + (f"  max={lr_max_cpu}/{lr_max_mem}" if lr_enabled else "  （已删除）"))

    for c in changed:
        print(f"    · {c}")
    print()
    show()
    return 0


# ---------------------------------------------------------------- list
def cmd_list(args) -> int:
    admin = Admin(args.kubeconfig, args.context)
    ns_list = admin.core.list_namespace(label_selector=f"managed-by={MANAGED_BY}")
    if not ns_list.items:
        print("（尚无本工具创建的用户）")
        return 0
    print(f"{'NAMESPACE':24s} {'TENANT':16s} {'PHASE':12s} {'PODS':>5s} / {'QUOTA':<5s} {'CPU':>8s} {'MEM':>10s}")
    for n in sorted(ns_list.items, key=lambda x: x.metadata.name):
        name = n.metadata.name
        labels = n.metadata.labels or {}
        pods = used = "-"
        cpu = mem = "-"
        try:
            pl = admin.core.list_namespaced_pod(name)
            pods = str(len(pl.items))
        except ApiException:
            pass
        has_quota = False
        try:
            q = admin.core.read_namespaced_resource_quota("tenant-quota", name)
            has_quota = True
            hard, u = q.status.hard or {}, q.status.used or {}
            used = hard.get("pods", "-")
            cpu = f"{u.get('requests.cpu','0')}/{hard.get('requests.cpu','-')}"
            mem = f"{u.get('requests.memory','0')}/{hard.get('requests.memory','-')}"
        except ApiException:
            pass
        if not has_quota:
            # 未设配额（默认）—— 只显示实际用量，无上限
            used = cpu = mem = "不限"
        print(f"{name:24s} {labels.get(TENANT_LABEL,'-'):16s} {n.status.phase:12s} "
              f"{pods:>5s} / {used:<5s} {cpu:>8s} {mem:>10s}")
    return 0


# ---------------------------------------------------------------- 用户说明
def print_user_guide(user, ns, out: Path, server: str, quota_note: str = "不限",
                     limitrange: bool = True, lr_max: str | None = None):
    # 「资源限制」那段的措辞取决于是否真的设了配额 —— 别写死，否则与实际不符
    if quota_note == "不限":
        res_note = (" 资源限制   : 本命名空间没有配额上限。但 worker 节点是共享的，\n"
                    "              请自觉给容器写 resources.requests/limits（例如 100m/256Mi 起）。\n"
                    "              不写 limits 的容器在节点内存吃紧时【最先被内核 OOM 杀掉】，\n"
                    "              还可能拖垮同节点上别人的 Pod。")
    else:
        res_note = (f" 资源限制   : 本命名空间配额 — {quota_note}。超出会被 API 拒绝。\n"
                    "              用 `kubectl get resourcequota tenant-quota -o yaml` 自查余量。\n"
                    + (f"              单容器上限 {lr_max}；未写 resources 的容器会自动获得\n"
                       "              requests 100m/128Mi、limits 500m/512Mi 的默认值。"
                       if limitrange else
                       "              ⚠️ 本命名空间未设 LimitRange：Pod 必须【显式写 resources】，\n"
                       "                 否则会因配额无法核算而被拒绝。"))

    print(f"""
============================================================================
 用户 {user} 已就绪
============================================================================
 kubeconfig : {out}
 命名空间   : {ns}   （你只能操作这一个命名空间）
 API 地址   : {server}
 资源配额   : {quota_note}

 默认调度   : 你的 Pod 会自动落在 worker 节点（k8s-worker-01/02）。
              infra 节点（master）只跑平台组件，且有准入策略强制拦截 ——
              不要尝试用 toleration / nodeSelector / nodeName 去够到它，
              会被 API 直接拒绝。

 存储       : openebs-lvmpv 只存在于 master，对你【不可用】。请用:
                storageClassName: managed-nfs-storage    # 共享、ReadWriteMany
                storageClassName: openebs-hostpath       # 节点本地、ReadWriteOnce
              （集群未设默认 SC，PVC 不写 storageClassName 会一直 Pending）

 Service    : 只用 ClusterIP 或 NodePort。不要用 type: LoadBalancer ——
              本集群是裸金属、没有 LB 控制器，EXTERNAL-IP 会永久 <pending>。

{res_note}

 常用命令:
   export KUBECONFIG={out}
   kubectl get pods -o wide
   kubectl apply -f my-app.yaml
   kubectl logs -f deploy/my-app
   kubectl exec -it deploy/my-app -- sh

 快速试用:
   kubectl create deployment web --image=easzlab.io.local:5000/easzlab/pause:3.10 --replicas=2
   kubectl get pods -o wide        # NODE 列只会出现 k8s-worker-0X

 你【不能】做的事（返回 Forbidden 属正常）:
   · 访问别人的命名空间或 kube-system
   · 查看/修改节点、PV、StorageClass 等集群级资源
   · 创建 Role/RoleBinding（防止在命名空间内提权）
   · 修改自己命名空间的 ResourceQuota（只读）
============================================================================""")


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="useradmin.py",
        description="k8s 租户用户管理：独立命名空间 + 受限 kubeconfig",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="真正的隔离由集群侧的 enforce-pool-isolation.yaml + node taint 提供；"
               "本工具只是开账号。",
    )
    p.add_argument("--kubeconfig", help="管理端 kubeconfig（默认 ~/.kube/config 或 in-cluster）")
    p.add_argument("--context", help="管理端 context")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="创建用户")
    c.add_argument("username")
    c.add_argument("--namespace")
    c.add_argument("--server", default=DEFAULT_SERVER)
    c.add_argument("--ca-file", default=DEFAULT_CA)
    c.add_argument("--out", help="输出 kubeconfig 路径，默认 ./<username>.kubeconfig")
    # 默认受限；传 0 表示该维度不限制
    c.add_argument("--quota-cpu", default=DEFAULT_QUOTA_CPU, metavar="N",
                   help=f"命名空间 CPU 总量上限，0=不限制（默认 {DEFAULT_QUOTA_CPU}）")
    c.add_argument("--quota-mem", default=DEFAULT_QUOTA_MEM, metavar="SIZE",
                   help=f"命名空间内存总量上限，0=不限制（默认 {DEFAULT_QUOTA_MEM}）")
    c.add_argument("--quota-pods", default=DEFAULT_QUOTA_PODS, metavar="N",
                   help=f"Pod 数上限，0=不限制（默认 {DEFAULT_QUOTA_PODS}）")
    c.add_argument("--quota-storage", default=DEFAULT_QUOTA_STORAGE, metavar="SIZE",
                   help=f"存储总量上限，0=不限制（默认 {DEFAULT_QUOTA_STORAGE}）")
    c.add_argument("--limitrange", action=argparse.BooleanOptionalAction, default=True,
                   help="创建 LimitRange：给未写 resources 的容器自动补默认值并限制单容器上限。"
                        "默认创建；用 --no-limitrange 关闭。"
                        "（注意：设了 CPU/内存配额却无 LimitRange 时，"
                        "未写 resources 的 Pod 会被配额拒绝）")
    c.add_argument("--limitrange-max-cpu", default=DEFAULT_LR_MAX_CPU, metavar="N",
                   help=f"单容器 CPU 上限，默认 {DEFAULT_LR_MAX_CPU}")
    c.add_argument("--limitrange-max-mem", default=DEFAULT_LR_MAX_MEM, metavar="SIZE",
                   help=f"单容器内存上限，默认 {DEFAULT_LR_MAX_MEM}")
    c.add_argument("--pod-security", default="baseline",
                   choices=["privileged", "baseline", "restricted"])
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--allow-unprotected", action="store_true",
                   help="即使池隔离准入策略缺失也继续（不建议）")
    c.set_defaults(func=cmd_create)

    q = sub.add_parser("quota", help="查看 / 修改【已有】用户的配额与 LimitRange",
                       description="只改动显式传入的维度；传 0 表示取消该维度限制；"
                                   "四维度全为 0 则删除整个 ResourceQuota（回到不限制）。")
    q.add_argument("username")
    q.add_argument("--namespace")
    q.add_argument("--quota-cpu", default=None, metavar="N",
                   help="CPU 总量上限，0=取消该维度限制")
    q.add_argument("--quota-mem", default=None, metavar="SIZE",
                   help="内存总量上限，0=取消该维度限制")
    q.add_argument("--quota-pods", default=None, metavar="N",
                   help="Pod 数上限，0=取消该维度限制")
    q.add_argument("--quota-storage", default=None, metavar="SIZE",
                   help="存储总量上限，0=取消该维度限制")
    q.add_argument("--limitrange", action=argparse.BooleanOptionalAction, default=None,
                   help="保持/创建/删除 LimitRange（--limitrange / --no-limitrange）")
    q.add_argument("--limitrange-max-cpu", default=None, metavar="N",
                   help="单容器 CPU 上限（未指定则沿用现值）")
    q.add_argument("--limitrange-max-mem", default=None, metavar="SIZE",
                   help="单容器内存上限（未指定则沿用现值）")
    q.add_argument("--show", action="store_true", help="只显示当前限制与用量，不做变更")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_quota)

    v = sub.add_parser("verify", help="复查既有用户的边界")
    v.add_argument("username")
    v.add_argument("--namespace")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("delete", help="撤销用户权限")
    d.add_argument("username")
    d.add_argument("--namespace")
    d.add_argument("--out", help="额外要删除的 kubeconfig 路径")
    d.add_argument("--keep-kubeconfig", action="store_true")
    d.add_argument("--yes", "-y", action="store_true")
    d.set_defaults(func=cmd_delete)

    ls = sub.add_parser("list", help="列出本工具创建的用户")
    ls.set_defaults(func=cmd_list)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # 用户名/命名空间必须是合法 DNS-1123 label —— 否则 API 会拒，提前给出清晰报错
    if args.cmd in ("create", "delete", "verify", "quota"):
        for field, val in (("username", args.username),
                           ("namespace", getattr(args, "namespace", None))):
            if val and not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", val):
                sys.exit(f"错误: {field}={val!r} 不是合法 DNS-1123 label"
                         f"（只允许小写字母/数字/连字符，且首尾为字母数字）")
    try:
        return args.func(args)
    except ApiException as exc:
        Log.err(f"Kubernetes API 错误: {exc.status} {exc.reason}")
        body = (exc.body or "").strip()
        if body:
            print(f"    {body[:600]}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
