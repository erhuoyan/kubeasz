#!/bin/bash
# 部署准入策略并立即验证（配错会阻断 Pod 创建，所以每步都验）
# 注意: kubectl 跑在 kubeasz 容器内 → 必须 -i（走 stdin 传 YAML），
#       且 -f 的路径是【容器内】路径。
K="docker exec -i kubeasz kubectl"
YAML=/tmp/enforce-pool-isolation.yaml

echo "############ 0) 部署前基线 ############"
$K get pods -A --no-headers 2>/dev/null | wc -l | xargs echo "  当前 Pod 总数:"

echo
echo "############ 1) 给平台命名空间打豁免标签 ############"
for ns in kube-system monitor openebs kb-system; do
  $K label ns "$ns" pool-access=privileged --overwrite 2>&1 | sed 's/^/  /'
done

echo
echo "############ 2) 应用准入策略（走 stdin）############"
cat "$YAML" | $K apply -f - 2>&1 | sed 's/^/  /'

echo
echo "############ 3) 策略状态 ############"
$K get validatingadmissionpolicy 2>&1 | sed 's/^/  /'
$K get validatingadmissionpolicybinding 2>&1 | sed 's/^/  /'

# VAP 更新后编译/下发需要一点时间，等它生效再测，否则会误判为"策略有误"
echo "  ── 等待策略生效 ──"
sleep 4

echo
echo "############ 4) 安全闸：普通 Pod 是否仍能创建（default）############"
$K -n default delete pod vcheck-normal --ignore-not-found >/dev/null 2>&1
out=$($K -n default run vcheck-normal --image=easzlab.io.local:5000/easzlab/pause:3.10 --restart=Never 2>&1)
if echo "$out" | grep -q "created"; then
  echo "  ✓ 普通 Pod 创建成功（策略未误杀）"
else
  echo "  ✗✗✗ 普通 Pod 被拒 —— 策略有误，立即回滚！"
  echo "$out" | sed 's/^/      /'
  $K delete validatingadmissionpolicybinding tenant-pool-guard-pods tenant-pool-guard-workloads tenant-pool-guard-cronjobs --ignore-not-found >/dev/null 2>&1
  $K delete validatingadmissionpolicy tenant-pool-guard-pods tenant-pool-guard-workloads tenant-pool-guard-cronjobs --ignore-not-found >/dev/null 2>&1
  echo "  == 已回滚 =="
  exit 1
fi

echo
echo "############ 5) 反例A：default 带 infra toleration（应被拒）############"
$K -n default apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: v1
kind: Pod
metadata:
  name: vcheck-toleration
spec:
  restartPolicy: Never
  tolerations:
    - key: node-role.kubernetes.io/infra
      operator: Exists
      effect: NoSchedule
  containers:
    - name: c
      image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
$K -n default delete pod vcheck-toleration --ignore-not-found >/dev/null 2>&1

echo
echo "############ 6) 反例B：default 里 nodeName 直绑 master-03（应被拒）############"
$K -n default apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: v1
kind: Pod
metadata:
  name: vcheck-nodename
spec:
  restartPolicy: Never
  nodeName: k8s-master-03
  containers:
    - name: c
      image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
$K -n default delete pod vcheck-nodename --ignore-not-found >/dev/null 2>&1

echo
echo "############ 7) 反例C：Deployment 模板带 toleration（应被拒）############"
$K -n default apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vcheck-deploy
spec:
  replicas: 1
  selector: { matchLabels: { app: vcheck } }
  template:
    metadata: { labels: { app: vcheck } }
    spec:
      nodeSelector:
        node-role.kubernetes.io/infra: ""
      tolerations:
        - key: node-role.kubernetes.io/infra
          operator: Exists
          effect: NoSchedule
      containers:
        - name: c
          image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
$K -n default delete deploy vcheck-deploy --ignore-not-found >/dev/null 2>&1

echo
echo "############ 8) 反例D：CronJob 模板带 toleration（应被拒）############"
$K -n default apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: vcheck-cron
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          tolerations:
            - key: node-role.kubernetes.io/infra
              operator: Exists
              effect: NoSchedule
          containers:
            - name: c
              image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
$K -n default delete cronjob vcheck-cron --ignore-not-found >/dev/null 2>&1

echo
echo "############ 9) 正例：平台 ns 豁免（kube-system 带 toleration 应通过）############"
$K -n kube-system apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: v1
kind: Pod
metadata:
  name: vcheck-exempt
spec:
  restartPolicy: Never
  nodeSelector:
    node-role.kubernetes.io/infra: ""
  tolerations:
    - key: node-role.kubernetes.io/infra
      operator: Exists
      effect: NoSchedule
  containers:
    - name: c
      image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
$K -n kube-system delete pod vcheck-exempt --ignore-not-found >/dev/null 2>&1

echo
echo "############ 10) 反例E：普通 Pod 能正常起来吗（default，无 toleration）############"
$K -n default apply -f - 2>&1 <<'EOF' | sed 's/^/  /'
apiVersion: v1
kind: Pod
metadata:
  name: vcheck-ok
spec:
  restartPolicy: Never
  containers:
    - name: c
      image: easzlab.io.local:5000/easzlab/pause:3.10
EOF
sleep 6
$K -n default get pod vcheck-ok -o wide 2>&1 | sed 's/^/  /'
$K -n default delete pod vcheck-ok --ignore-not-found --wait=false >/dev/null 2>&1

echo
echo "############ 11) 平台组件健康 ############"
$K get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed"' | sed 's/^/  /'
echo "  (上面为空 = 全部 Running)"
$K get pods -A --no-headers 2>/dev/null | wc -l | xargs echo "  Pod 总数:"
