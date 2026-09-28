#!/usr/bin/env bash
# ============================================================================
# test-as-user.sh — 以【真实用户身份】做端到端验证
#
# 用用户拿到的 kubeconfig + 本地 kubectl 实际发请求，逐条核对权限边界：
#   正例应成功（ok）、反例应被拒（deny：RBAC Forbidden 或准入策略 Denied）
#
# 用法:
#   ./test-as-user.sh <kubeconfig> <namespace>
#
# 说明：本脚本【只读+自建自删】—— 所有测试资源都建在该 ns 内并在结束时清掉。
# ============================================================================
set -uo pipefail

KCFG="${1:?用法: test-as-user.sh <kubeconfig> <namespace>}"
NS="${2:?用法: test-as-user.sh <kubeconfig> <namespace>}"
KC="kubectl --kubeconfig $KCFG"
IMG="easzlab.io.local:5000/easzlab/pause:3.10"

PASS=0; FAIL=0
chk() { # chk <期望:ok|deny> <描述> <命令...>
  local want="$1" desc="$2"; shift 2
  local out rc got
  out=$("$@" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then got=ok
  elif printf '%s' "$out" | grep -qiE 'forbidden|denied|禁止|cannot|invalid'; then got=deny
  else got=err; fi
  if [ "$got" = "$want" ]; then
    printf '  \033[32m✓\033[0m %-50s [%s]\n' "$desc" "$got"; PASS=$((PASS+1))
  else
    printf '  \033[31m✗\033[0m %-50s 期望=%s 实际=%s\n' "$desc" "$want" "$got"; FAIL=$((FAIL+1))
    printf '      %s\n' "$(printf '%s' "$out" | head -2 | tr '\n' ' ')"
  fi
}
# 权限类断言：直接问鉴权层"能不能"，不依赖目标对象是否存在。
# 为什么不直接对对象操作：对象不存在时返回的是 404 而非 Forbidden，
# 那会同时掩盖"有权限"和"没权限"两种情况 —— 测不出真实权限。
chk_deny_perm() { # chk_deny_perm <描述> <verb> <resource> [ns]
  local desc="$1" verb="$2" res="$3" ns="${4:-}" r
  if [ -n "$ns" ]; then r=$($KC -n "$ns" auth can-i "$verb" "$res" 2>/dev/null)
  else r=$($KC auth can-i "$verb" "$res" 2>/dev/null); fi
  if [ "$r" = "no" ]; then
    printf '  \033[32m✓\033[0m %-50s [deny]\n' "$desc"; PASS=$((PASS+1))
  else
    printf '  \033[31m✗\033[0m %-50s 期望=deny 实际=%s\n' "$desc" "${r:-空}"; FAIL=$((FAIL+1))
  fi
}

pod() { cat <<EOF | $KC -n "$NS" apply -f - >/dev/null 2>&1
$1
EOF
}
podapply() { cat <<EOF | $KC -n "$NS" apply -f - 2>&1
$1
EOF
}

# 前置清理：让本脚本可重复运行（上一轮若中断会留下同名资源）
# 同样只按名字删，绝不 delete secret --all（会毁掉 SA token）
$KC -n "$NS" delete deploy   t-web t-over     --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete svc      t-svc t-hsvc     --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete cm       t-cm             --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete secret   t-sec            --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete job      t-job            --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete cronjob  t-cron           --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete pvc      t-pvc            --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete sa       t-sa             --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete pod --all                 --ignore-not-found >/dev/null 2>&1

echo "════ 身份与认证 ════"
chk ok "token 可用（whoami）" $KC auth whoami
chk ok "可读本 ns" $KC -n "$NS" get pods

echo
echo "════ 正例：日常开发操作 ════"
chk ok "建 Deployment" $KC -n "$NS" create deployment t-web --image=$IMG --replicas=2
chk ok "建 StatefulSet 前置：headless svc" $KC -n "$NS" create service clusterip t-hsvc --tcp=80:80
chk ok "建 ConfigMap" $KC -n "$NS" create configmap t-cm --from-literal=k=v
chk ok "建 Secret" $KC -n "$NS" create secret generic t-sec --from-literal=p=s3cr3t
chk ok "建 Service" $KC -n "$NS" create service clusterip t-svc --tcp=8080:80
chk ok "建 Job" $KC -n "$NS" create job t-job --image=$IMG
chk ok "建 CronJob" $KC -n "$NS" create cronjob t-cron --image=$IMG --schedule='*/5 * * * *'
chk ok "建 PVC（managed-nfs-storage）" pod "apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: t-pvc }
spec:
  storageClassName: managed-nfs-storage
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 1Gi } }"
chk ok "看自己配额（只读）" $KC -n "$NS" get resourcequota
chk ok "看自己 limitrange（只读）" $KC -n "$NS" get limitrange
chk ok "看 pod 日志" bash -c "$KC -n $NS logs deploy/t-web --tail=1 2>&1 || $KC -n $NS get pods >/dev/null"

echo
echo "════ 反例：跨命名空间 ════"
chk deny "读 kube-system" $KC -n kube-system get pods
chk deny "读 default" $KC -n default get pods
chk deny "读 monitor" $KC -n monitor get pods
chk deny "在 kube-system 建 pod" $KC -n kube-system run t-evil --image=$IMG
chk deny "读 kube-system secret" $KC -n kube-system get secrets
chk deny "删 kube-system 资源" $KC -n kube-system delete pods --all
chk deny "在别人 ns 建 deployment" $KC -n default create deployment t-evil --image=$IMG
chk deny "pod exec 到别人 ns" $KC -n kube-system exec -it deploy/nonexistent -- sh

echo
echo "════ 反例：集群级资源 ════"
chk deny "get nodes" $KC get nodes
chk deny "get namespaces" $KC get namespaces
chk deny "get pv" $KC get pv
chk deny "get storageclasses" $KC get sc
chk deny "get clusterroles" $KC get clusterroles
chk deny "get clusterrolebindings" $KC get clusterrolebindings
chk deny "get 所有 ns 的 pod（-A）" $KC get pods -A
chk deny "get events -A" $KC get events -A
chk deny "看 CRD" $KC get crd
chk deny "看自定义资源 lvmnodes" $KC -n openebs get lvmnodes.local.openebs.io

echo
echo "════ 反例：ns 内提权 ════"
chk deny "建 Role" $KC -n "$NS" create role t-r --verb=get --resource=pods
chk deny "建 RoleBinding" $KC -n "$NS" create rolebinding t-rb --role=t-r --serviceaccount=$NS:tenant-user
chk_deny_perm "无 patch resourcequotas 权限" patch resourcequotas "$NS"
chk_deny_perm "无 delete resourcequotas 权限" delete resourcequotas "$NS"
chk_deny_perm "无 create limitranges 权限"  create limitranges  "$NS"
chk_deny_perm "无 delete limitranges 权限"  delete limitranges  "$NS"
# 建 ServiceAccount【有意允许】：工作负载常需要用专用 SA。
# 它之所以不构成提权 —— 用户没有 rolebindings 的创建权限，
# 无法把任何 Role/ClusterRole 绑到这个 SA 上；即便自建 token secret，
# 拿到的也只是自己那份受限权限。
chk ok   "建 ServiceAccount（允许，用于工作负载）" $KC -n "$NS" create serviceaccount t-sa
chk deny "建新 Namespace" $KC create ns t-sneak
chk deny "建 ClusterRoleBinding" $KC create clusterrolebinding t-crb --clusterrole=cluster-admin --serviceaccount=$NS:tenant-user
# 真正的提权路径：把高权限 ClusterRole 绑到自己的 SA —— 必须被拒
chk deny "把 cluster-admin 绑给自己的 SA" $KC -n "$NS" create rolebinding t-ca --clusterrole=cluster-admin --serviceaccount=$NS:tenant-user

echo
echo "════ 反例：绕过 infra 隔离 ════"
chk deny "Pod 带 infra toleration" podapply "apiVersion: v1
kind: Pod
metadata: { name: t-b1 }
spec:
  restartPolicy: Never
  tolerations: [{ key: node-role.kubernetes.io/infra, operator: Exists, effect: NoSchedule }]
  containers: [{ name: c, image: $IMG }]"
chk deny "Pod 用 nodeName 直绑 master" podapply "apiVersion: v1
kind: Pod
metadata: { name: t-b2 }
spec:
  restartPolicy: Never
  nodeName: k8s-master-01
  containers: [{ name: c, image: $IMG }]"
chk deny "Pod 用 nodeSelector 选 infra" podapply "apiVersion: v1
kind: Pod
metadata: { name: t-b3 }
spec:
  restartPolicy: Never
  nodeSelector: { node-role.kubernetes.io/infra: '' }
  containers: [{ name: c, image: $IMG }]"
chk deny "Deployment 模板带 toleration" podapply "apiVersion: apps/v1
kind: Deployment
metadata: { name: t-b4 }
spec:
  replicas: 1
  selector: { matchLabels: { app: b4 } }
  template:
    metadata: { labels: { app: b4 } }
    spec:
      tolerations: [{ key: node-role.kubernetes.io/infra, operator: Exists, effect: NoSchedule }]
      containers: [{ name: c, image: $IMG }]"
chk deny "nodeAffinity 硬亲和 infra" podapply "apiVersion: v1
kind: Pod
metadata: { name: t-b5 }
spec:
  restartPolicy: Never
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - { key: node-role.kubernetes.io/infra, operator: Exists }
  containers: [{ name: c, image: $IMG }]"
chk deny "抢占 system-cluster-critical" podapply "apiVersion: v1
kind: Pod
metadata: { name: t-b6 }
spec:
  restartPolicy: Never
  priorityClassName: system-cluster-critical
  containers: [{ name: c, image: $IMG }]"
chk deny "CronJob 模板带 toleration" podapply "apiVersion: batch/v1
kind: CronJob
metadata: { name: t-b7 }
spec:
  schedule: '*/5 * * * *'
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          tolerations: [{ key: node-role.kubernetes.io/infra, operator: Exists, effect: NoSchedule }]
          containers: [{ name: c, image: $IMG }]"

echo
echo "════ 配额（仅当该 ns 设了 ResourceQuota 时才测）════"
if $KC -n "$NS" get resourcequota tenant-quota >/dev/null 2>&1; then
  # 注意：replicas=100 只在【有配额】时安全；无配额时它会真的建 100 个 Pod。
  podapply "apiVersion: apps/v1
kind: Deployment
metadata: { name: t-over }
spec:
  replicas: 100
  selector: { matchLabels: { app: over } }
  template:
    metadata: { labels: { app: over } }
    spec:
      containers: [{ name: c, image: $IMG }]" >/dev/null 2>&1
  sleep 8
  used=$($KC -n "$NS" get resourcequota tenant-quota -o jsonpath='{.status.used.pods}' 2>/dev/null)
  hard=$($KC -n "$NS" get resourcequota tenant-quota -o jsonpath='{.status.hard.pods}' 2>/dev/null)
  if [ -n "$hard" ]; then
    if [ -n "$used" ] && [ "$used" -le "$hard" ]; then
      printf '  \033[32m✓\033[0m %-50s [%s/%s]\n' "配额生效：副本被卡在上限" "$used" "$hard"; PASS=$((PASS+1))
    else
      printf '  \033[31m✗\033[0m %-50s used=%s hard=%s\n' "配额未生效" "$used" "$hard"; FAIL=$((FAIL+1))
    fi
    nf=$($KC -n "$NS" get events --field-selector reason=FailedCreate --no-headers 2>/dev/null | grep -c 'exceeded quota' || true)
    printf '  \033[32m✓\033[0m %-50s [%s 条]\n' "ReplicaSet 报 exceeded quota" "${nf:-0}"; PASS=$((PASS+1))
  else
    printf '  \033[36m-\033[0m %-50s [配额存在但不限 pods]\n' "配额检查（跳过）"
  fi
else
  printf '  \033[36m-\033[0m %-50s [该 ns 未设配额]\n' "配额检查（跳过）"
fi

echo
echo "════ 正例：Pod 实际落在 worker ════"
sleep 6
$KC -n "$NS" get pods -o wide --no-headers 2>&1 | awk '{printf "    %-32s %-10s %s\n", $1, $3, $7}'
onmaster=$($KC -n "$NS" get pods -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(sum(1 for p in d['items'] if (p.get('spec') or {}).get('nodeName','').startswith('k8s-master')))")
if [ "${onmaster:-1}" = "0" ]; then
  printf '  \033[32m✓\033[0m %-50s [%s]\n' "落在 master 上的 Pod 数（应为 0）" "$onmaster"; PASS=$((PASS+1))
else
  printf '  \033[31m✗\033[0m %-50s [%s]\n' "有 Pod 落在 master！" "$onmaster"; FAIL=$((FAIL+1))
fi

echo
echo "════ 清理测试资源 ════"
# ⚠️ 只按【名字】删测试建的东西，绝不用 `delete secret --all`：
#    用户对 secrets 有写权限，`--all` 会把 SA 的 token Secret（<user>-token）一起删掉，
#    而那个 Secret 是【手工创建】的，kube-controller-manager 不会自动重建
#    → 已发出的 kubeconfig 会永久失效（只剩 token 文件却认证不了）。
#    恢复办法：重跑 `useradmin.py create <user>`（幂等，会重建 Secret 并刷新 kubeconfig）。
$KC -n "$NS" delete deploy   t-web t-over          --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete svc      t-svc t-hsvc          --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete cm       t-cm                  --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete secret   t-sec                 --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete job      t-job                 --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete cronjob  t-cron                --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete pvc      t-pvc                 --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete sa       t-sa                  --ignore-not-found >/dev/null 2>&1
$KC -n "$NS" delete pod --all                      --ignore-not-found >/dev/null 2>&1
echo "  已清理（保留 SA token Secret）"

echo
echo "════════════════════════════════════════════════"
printf "  通过 %d 项，失败 %d 项\n" $PASS $FAIL
[ $FAIL -eq 0 ] && echo "  ✓ 全部符合预期" || echo "  ✗ 存在不符合预期的项"
exit $(( FAIL > 0 ? 1 : 0 ))
