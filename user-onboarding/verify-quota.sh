#!/bin/bash
# 验证：默认受限 + 创建时可指定 + 创建后可修改
cd /data/ops-scripts/user-onboarding
P=./venv/bin/python
K="docker exec -i kubeasz kubectl"
h() { $K -n "$1" get resourcequota tenant-quota -o jsonpath='{.spec.hard}' 2>&1; echo; }

echo "############ 1) 默认创建 → 应带默认配额 ############"
$P useradmin.py create u1 --out /data/users/u1.kubeconfig 2>&1 | sed -n '/5\/8/,/6\/8/p' | sed 's/^/  /'
echo "  实际 hard: $(h u1)"
echo "  LimitRange:"; $K -n u1 get limitrange tenant-defaults -o jsonpath='{.spec.limits[0].max}' 2>&1; echo
echo "  用户说明里的配额行:"
$P useradmin.py create u1 --out /data/users/u1.kubeconfig 2>&1 | grep "资源配额" | sed 's/^/    /'

echo
echo "############ 2) 创建时显式指定 ############"
$P useradmin.py create u2 --quota-cpu 8 --quota-mem 16Gi --quota-pods 40 --quota-storage 200Gi \
   --out /data/users/u2.kubeconfig 2>&1 | sed -n '/5\/8/,/6\/8/p' | sed 's/^/  /'
echo "  实际 hard: $(h u2)"

echo
echo "############ 3) 创建时显式「不限制」（全 0）############"
$P useradmin.py create u3 --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 \
   --no-limitrange --out /data/users/u3.kubeconfig 2>&1 | sed -n '/5\/8/,/6\/8/p' | sed 's/^/  /'
echo "  实际 quota/limitrange:"; $K -n u3 get resourcequota,limitrange 2>&1 | sed 's/^/    /'

echo
echo "############ 4) 创建后修改：只改 pods（其余应保持不变）############"
echo "  改前: $(h u1)"
$P useradmin.py quota u1 --quota-pods 50 2>&1 | sed 's/^/  /'
echo "  改后: $(h u1)   ← cpu/mem/storage 应仍是 4/8Gi/50Gi"

echo
echo "############ 5) 创建后修改：取消某一维度（关键：键必须真的消失）############"
$P useradmin.py quota u1 --quota-storage 0 2>&1 | grep -E "·|ResourceQuota" | sed 's/^/  /'
echo "  改后: $(h u1)   ← 应无 requests.storage"

echo
echo "############ 6) 创建后修改：全部取消 → 整个 ResourceQuota 应被删除 ############"
$P useradmin.py quota u1 --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 --no-limitrange 2>&1 | grep -E "ResourceQuota|LimitRange" | sed 's/^/  /'
echo "  现在的 quota:"; $K -n u1 get resourcequota,limitrange 2>&1 | sed 's/^/    /'

echo
echo "############ 7) 再改回来：从「无配额」恢复为受限 ############"
$P useradmin.py quota u1 --quota-cpu 2 --quota-mem 4Gi --quota-pods 10 2>&1 | grep -E "ResourceQuota" | sed 's/^/  /'
echo "  改后: $(h u1)"

echo
echo "############ 8) --show 只读展示 ############"
$P useradmin.py quota u2 --show 2>&1 | sed 's/^/  /'

echo
echo "############ 9) --dry-run 不产生变更 ############"
before=$(h u2)
$P useradmin.py quota u2 --quota-cpu 99 --dry-run 2>&1 | sed 's/^/  /'
after=$(h u2)
[ "$before" = "$after" ] && echo "  ✓ dry-run 未改动（仍为 $after）" || echo "  ✗ dry-run 竟然改了！$before → $after"

echo
echo "############ 10) 幂等：重复设置相同值应报「无变更」############"
$P useradmin.py quota u2 --quota-cpu 8 2>&1 | grep -E "无变更|无指定" | sed 's/^/  /'

echo
echo "############ 11) 对不存在的用户操作 ############"
$P useradmin.py quota nosuchuser --quota-pods 5 2>&1 | sed 's/^/  /'

echo
echo "############ 12) create 幂等：对已改过配额的 u1 重跑 create ############"
echo "  重跑前: $(h u1)"
$P useradmin.py create u1 --quota-cpu 4 --quota-mem 8Gi --quota-pods 20 --quota-storage 50Gi \
   --out /data/users/u1.kubeconfig 2>&1 | sed -n '/5\/8/,/6\/8/p' | sed 's/^/  /'
echo "  重跑后: $(h u1)   ← 应回到默认四项"

echo
echo "############ 清理 ############"
for u in u1 u2 u3; do $P useradmin.py delete $u --yes >/dev/null 2>&1; done
echo "  已清理"
$P useradmin.py list 2>&1 | sed 's/^/  /'
