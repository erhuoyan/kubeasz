#!/usr/bin/env bash
# ============================================================================
# verify-doc.sh — 核对 README 里写的命令是否真的可用（防文档腐化）
#
# 做什么：把文档各章节里的示例命令**实跑一遍**，逐条核对结果。
#         改完 useradmin.py 或 README 之后跑它，能立刻发现"文档说的和代码做的不一致"。
#
# 用法:
#   ./verify-doc.sh <用户侧 kubeconfig>    # 用于「八、排错」里的手工反例一节
#   ./verify-doc.sh                        # 跳过需要用户 kubeconfig 的那一节
#
# 用户侧 kubeconfig 从哪来:
#   scp root@114.67.232.32:/data/users/<某用户>.kubeconfig /tmp/u.kubeconfig
#   （本脚本会自己建临时用户，所以随便给一份【该集群】的用户 kubeconfig 即可）
#
# 环境变量:
#   KUBECTL   默认 'docker exec -i kubeasz kubectl'
#
# 副作用：临时创建/删除名为 zhangsan、zhangsan2 的两个用户，结束时清理。
#         **不要**在有真实同名用户的生产集群上跑。
# ============================================================================
set -uo pipefail

USER_KCFG="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY=./venv/bin/python
KUBECTL="${KUBECTL:-docker exec -i kubeasz kubectl}"
read -r -a K <<< "$KUBECTL"
U1=zhangsan
U2=zhangsan2
OUTDIR="${OUTDIR:-/tmp/useradmin-docverify}"

PASS=0; FAIL=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
skip(){ printf '  \033[36m-\033[0m %s\n' "$*"; }

[ -x "$PY" ] || { echo "找不到 $PY —— 先跑 ./setup-venv.sh" >&2; exit 1; }
command -v "${K[0]}" >/dev/null 2>&1 || { echo "找不到命令 ${K[0]}" >&2; exit 1; }

cleanup() {
  for u in "$U1" "$U2"; do "$PY" useradmin.py delete "$u" --yes >/dev/null 2>&1; done
  "${K[@]}" label ns "$U1" pool-access- >/dev/null 2>&1 || true
  rm -rf "$OUTDIR"
}
trap cleanup EXIT

# 前置：清掉可能残留的同名用户，保证可重复运行
cleanup; sleep 12
mkdir -p "$OUTDIR"

echo "══ 一、一页速查 ══"
"$PY" useradmin.py create "$U1" --out "$OUTDIR/$U1.kubeconfig" >/dev/null 2>&1 \
  && ok "create（默认配额）" || bad "create（默认配额）"
[ -f "$OUTDIR/$U1.kubeconfig" ] && ok "kubeconfig 已生成" || bad "kubeconfig 缺失"
"$PY" useradmin.py list | grep -q "$U1" && ok "list 含新用户" || bad "list"

h() { "${K[@]}" -n "$1" get resourcequota tenant-quota -o jsonpath='{.spec.hard}' 2>/dev/null; }
"$PY" useradmin.py quota "$U1" --quota-pods 50 >/dev/null 2>&1
[ "$("${K[@]}" -n "$U1" get resourcequota tenant-quota -o jsonpath='{.spec.hard.pods}' 2>/dev/null)" = "50" ] \
  && ok "quota --quota-pods 50 生效" || bad "quota 改 pods 未生效"
"$PY" useradmin.py quota "$U1" --show | grep -q "Pod 50 个" && ok "quota --show" || bad "quota --show"
"$PY" useradmin.py verify "$U1" | grep -q "通过 16 项，失败 0 项" && ok "verify 16/16" || bad "verify"

echo
echo "══ 三、安装与依赖 ══"
"$PY" -c "import kubernetes; print(kubernetes.__version__)" >/dev/null 2>&1 \
  && ok "kubernetes 客户端可导入（$( "$PY" -c 'import kubernetes;print(kubernetes.__version__)' )）" \
  || bad "kubernetes 不可导入"
"$PY" - <<'PYEOF' && ok "Python ≥3.9（BooleanOptionalAction 可用）" || bad "Python 版本过低"
import argparse, sys
assert sys.version_info >= (3, 9), sys.version
assert hasattr(argparse, "BooleanOptionalAction")
PYEOF

echo
echo "══ 四、命令详解 ══"
"$PY" useradmin.py quota "$U1" --quota-storage 0 >/dev/null 2>&1
h "$U1" | grep -qv "requests.storage" && ok "取消 storage 限制（键已消失）" || bad "取消 storage 限制"
"$PY" useradmin.py quota "$U1" --limitrange-max-cpu 2 --limitrange-max-mem 4Gi >/dev/null 2>&1
"${K[@]}" -n "$U1" get limitrange tenant-defaults -o jsonpath='{.spec.limits[0].max}' 2>/dev/null | grep -q '2' \
  && ok "改单容器上限" || bad "改单容器上限"
"$PY" useradmin.py quota "$U1" --quota-pods 50 --dry-run | grep -q "dry-run" && ok "--dry-run" || bad "--dry-run"

"$PY" useradmin.py create "$U2" --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 \
  --no-limitrange --out "$OUTDIR/$U2.kubeconfig" >/dev/null 2>&1
"${K[@]}" -n "$U2" get resourcequota 2>&1 | grep -q "No resources" \
  && ok "完全不限制写法（不建 ResourceQuota）" || bad "完全不限制写法"

echo
echo "══ 七、常见任务 ══"
"$PY" useradmin.py quota "$U1" --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 >/dev/null 2>&1 \
  && ok "临时放开限制" || bad "临时放开限制"
"$PY" useradmin.py quota "$U1" --quota-cpu 4 --quota-mem 8Gi --quota-pods 20 >/dev/null 2>&1 \
  && ok "收回限制" || bad "收回限制"
"${K[@]}" label ns "$U1" pool-access=privileged --overwrite >/dev/null 2>&1 \
  && ok "打豁免标签命令" || bad "打豁免标签"
"${K[@]}" label ns "$U1" pool-access- >/dev/null 2>&1

echo
echo "══ 八、排错：手工反例 ══"
if [ -n "$USER_KCFG" ] && [ -f "$USER_KCFG" ]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    skip "跳过：本机没有 kubectl（该节需在用户机器上跑）"
  else
    # 用临时用户自己的 kubeconfig 发请求（策略按 ns 匹配，会照常拦截）
    scp -q -o BatchMode=yes "$USER_KCFG" "$OUTDIR/u.kubeconfig" 2>/dev/null || cp "$USER_KCFG" "$OUTDIR/u.kubeconfig"
    out=$(kubectl --kubeconfig "$OUTDIR/u.kubeconfig" -n "$U1" apply -f - 2>&1 <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: bypass-test }
spec:
  restartPolicy: Never
  tolerations:
    - { key: node-role.kubernetes.io/infra, operator: Exists, effect: NoSchedule }
  containers:
    - { name: c, image: easzlab.io.local:5000/easzlab/pause:3.10 }
EOF
)
    if printf '%s' "$out" | grep -q "禁止容忍 infra 污点"; then
      ok "手工反例被正确拒绝"
    else
      bad "手工反例未按预期拒绝"
      printf '%s\n' "$out" | head -3 | sed 's/^/      /'
    fi
  fi
else
  skip "跳过：未提供用户 kubeconfig（用法见脚本头部说明）"
fi

echo
echo "══ 九、约束与坑 ══"
"${K[@]}" get validatingadmissionpolicy --no-headers 2>/dev/null | grep -q tenant-pool-guard \
  && ok "池隔离策略在位（约束 11 的前提）" || bad "池隔离策略缺失"
"${K[@]}" get ns -L pool-access --no-headers 2>/dev/null | grep -q privileged \
  && ok "存在豁免命名空间（约束 4 的前提）" || bad "未见豁免命名空间"
# 默认 SC 的标记是 NAME 列的 "(default)" 后缀（也可用注解判断）。
# ⚠️ 别用 awk '{print $3}' —— 第 3 列是 RECLAIMPOLICY(Delete)，与"是否默认"无关。
if "${K[@]}" get sc -o name 2>/dev/null | grep -q .; then
  if "${K[@]}" get sc -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
n=[i['metadata']['name'] for i in d['items']
   if (i['metadata'].get('annotations') or {}).get('storageclass.kubernetes.io/is-default-class')=='true']
sys.exit(0 if n else 1)
"; then
    bad "存在默认 StorageClass（约束 9 已过时，需改文档）"
  else
    ok "无默认 StorageClass（与约束 9 一致）"
  fi
else
  skip "集群无 StorageClass（约束 9 不适用）"
fi

echo
echo "════════════════════════════════════════"
printf "  通过 %d 项，失败 %d 项\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && echo "  ✓ README 里的命令与实际一致" || echo "  ✗ 文档与实现不符，请修正"
exit $(( FAIL > 0 ? 1 : 0 ))
