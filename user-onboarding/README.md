# 用户接入工具 — 使用手册

给一名用户发一份 kubeconfig：**只能操作自己的命名空间**，且其工作负载**只能落在 worker 节点**。

```
工具目录   /data/ops-scripts/user-onboarding/
主程序     useradmin.py    （Python + kubernetes 客户端，已配好独立 venv）
管理入口   宿主机 114.67.232.32
```

---

## 目录

- [一、一页速查](#一一页速查)
- [二、它是什么，边界在哪](#二它是什么边界在哪)
- [三、安装与依赖](#三安装与依赖)
- [四、命令详解](#四命令详解)
  - [create 建用户](#create--建用户)
  - [quota 改配额](#quota--改配额创建后修改)
  - [list 查看](#list--查看所有用户)
  - [verify 复查](#verify--复查边界)
  - [delete 撤销](#delete--撤销)
- [五、配额体系](#五配额体系)
- [六、交接给用户](#六交接给用户)
- [七、常见任务](#七常见任务)
- [八、排错](#八排错)
- [九、约束与坑](#九约束与坑)
- [附录 A：隔离原理](#附录-a隔离原理)
- [附录 B：与 kubeasz 自带 kcfg-adm 的关系](#附录-b与-kubeasz-自带-kcfg-adm-的关系)
- [附录 C：文件清单](#附录-c文件清单)
- [附录 D：命令速查表](#附录-d命令速查表)

---

## 一、一页速查

全部在**宿主机**上执行：

```bash
cd /data/ops-scripts/user-onboarding
PY=./venv/bin/python

# 建用户（默认配额 4C / 8Gi / 20 Pod / 50Gi）
$PY useradmin.py create zhangsan --out /data/users/zhangsan.kubeconfig

# 看谁在用、用了多少
$PY useradmin.py list

# 改配额（只改传进去的项）
$PY useradmin.py quota zhangsan --quota-pods 50
$PY useradmin.py quota zhangsan --show                  # 只看

# 复查隔离边界（16 项断言）
$PY useradmin.py verify zhangsan

# 撤销（删 ns + token 失效 + 删 kubeconfig）
$PY useradmin.py delete zhangsan
```

> **`create` 会把配额收敛回命令行给定的值。**
> 所以**调整已有用户的配额请用 `quota`，不要重跑 `create`** ——
> 裸跑 `create` 会把配额重置成默认的 4C/8Gi/20/50Gi。

---

## 二、它是什么，边界在哪

### 做什么

一条 `create` 命令完成 8 件事，产出可直接交付的 kubeconfig：

| 资源 | 名称 | 作用 |
|---|---|---|
| Namespace | `<username>` | 该用户的唯一活动范围 |
| ServiceAccount | `tenant-user` | 身份 |
| Role | `tenant-developer` | 命名空间级权限（**不含** RBAC / Node / 集群级资源）|
| RoleBinding | `tenant-developer` | 绑定上述两者 |
| ResourceQuota | `tenant-quota` | 命名空间总量上限 |
| LimitRange | `tenant-defaults` | 单容器上限 + 自动补默认 resources |
| Secret | `<username>-token` | 长期不过期的 token |
| kubeconfig | `--out` 指定 | 自包含（内嵌 CA + token），可直接发人 |

### 两条硬保证

| 保证 | 靠什么实现 |
|---|---|
| 只能操作自己的命名空间 | namespace 级 `Role`/`RoleBinding` |
| 工作负载只能落 worker 节点 | infra 节点 taint **+ `enforce-pool-isolation.yaml`（准入策略）** |

### 不做什么（**重要**）

> **真正的隔离全在集群侧的声明式资源里，不在这个脚本里。**
> `useradmin.py` 只是"开账号"的便捷封装。改成 Go / shell / Terraform 都不影响安全性 ——
> 反过来也别指望改这个脚本就能"修改"隔离规则，它没有那个能力。

具体说：

- 它**不**创建节点、**不**改 RBAC 之外的东西
- 它**不**能阻止管理员（cluster-admin）做任何事
- 它**不**是唯一入口 —— 管理员随时可以直接 `kubectl` 操作

---

## 三、安装与依赖

**当前环境已部署完毕**，本节供换环境/重建时参考。

### 3.1 一次性部署（三步）

```bash
cd /data/ops-scripts/user-onboarding

# 1) 平台命名空间打豁免标签（否则它们的 Pod 会被准入策略拒绝）
docker exec -i kubeasz kubectl label ns kube-system monitor openebs kb-system \
  pool-access=privileged --overwrite

# 2) 部署准入策略（带安全闸：若误杀普通 Pod 会自动回滚并报错）
bash deploy-verify.sh

# 3) 装 Python 依赖（建独立 venv）
./setup-venv.sh
```

**为什么第 2 步要有安全闸**：准入策略写错会**阻断全集群**的 Pod 创建。
`deploy-verify.sh` 每步都验，一旦普通 Pod 被误拒立即回滚并退出非零。

### 3.2 宿主机需要什么（系统级）

| 依赖 | 版本 | 用途 | 本环境 |
|---|---|---|---|
| **Python** | **≥ 3.9** | 跑 `useradmin.py` | ✅ 3.12.11（`/usr/bin/python3`）|
| Python `venv` 模块 | 随 Python | 建隔离环境 | ✅ |
| **bash** | ≥ 4 | 跑各 `.sh` 脚本 | ✅ |
| **docker** | — | 调 kubeasz 容器里的 kubectl | ✅ |
| **ssh / scp** | — | `test-as-user.sh` 从远端取 kubeconfig | ✅ |
| `kubectl` | — | 仅 `test-as-user.sh` 需要，**跑在你本地机器上** | 宿主**没有**（在容器里）|

> **为什么是 Python ≥ 3.9**：脚本用了 `argparse.BooleanOptionalAction`（3.9 引入）
> 来支持 `--limitrange` / `--no-limitrange` 这对开关。
> 已在 **Python 3.11（容器内）** 与 **3.12（宿主）** 实测通过。

> **宿主没有 kubectl 是有意为之** —— 这正是选 Python 客户端的原因：
> `useradmin.py` 直接走 HTTP API，不依赖 kubectl。
> 只有 `test-as-user.sh`（模拟用户视角）需要本地 kubectl。

### 3.3 Python 依赖清单

#### 直接依赖（只有 1 个）

```
kubernetes >= 29.0.0        # k8s 官方 Python 客户端
```

`useradmin.py` 用到它的三个东西：

```python
from kubernetes import client            # 各类 API 客户端（CoreV1/Rbac/Admissionregistration/Authorization）
from kubernetes import config as k8sconfig  # 加载 kubeconfig / in-cluster 配置
from kubernetes.client.rest import ApiException  # 捕获 API 错误（404/409/SAR 等）
```

其余全是 Python **标准库**，无需安装：
`argparse` `base64` `os` `re` `sys` `time` `pathlib`。

#### 传递依赖（`kubernetes` 自动拉下来的，共 21 个包）

`pip install kubernetes` 会连带装上以下包。**你不需要手动指定它们**，列在这里是为了：

1. 内网环境要预下载
2. 出问题时知道是哪个包的问题

| 包 | 拉它进来的原因 | 本环境版本 |
|---|---|---|
| `kubernetes` | **直接依赖** | 36.0.3 |
| `certifi` | HTTPS 根证书（TLS 验证 API server） | 2026.7.22 |
| `urllib3` | 底层 HTTP 传输 | 2.8.0 |
| `requests` | REST 请求 | 2.34.2 |
| `requests-oauthlib` → `oauthlib` | OIDC/OAuth 认证 | 2.0.0 / 4.0.0 |
| `websocket-client` | `exec` / `portforward` / `attach`（流式连接） | 1.9.2 |
| `aiohttp` → `aiohappyeyeballs` `aiosignal` `attrs` `frozenlist` `multidict` `propcache` `yarl` | 异步客户端支持 | 3.14.3 等 |
| `PyYAML` | 解析 kubeconfig（YAML） | 6.0.3 |
| `python-dateutil` → `six` | 时间处理 | 2.9.0.post0 / 1.17.0 |
| `durationpy` | k8s duration 格式（如 `30s`） | 0.11 |
| `typing_extensions` | 类型标注兼容层 | 4.16.0 |
| `charset-normalizer` `idna` | requests 的编码/域名处理 | 3.5.1 / 3.20 |

> 说明：`aiohttp` 那一串是 `kubernetes` 的**可选依赖**（python 客户端内置 async 支持），
> 由 `kubernetes` 的 `Requires` 声明自动带入。实测 `pip show kubernetes` 的 Requires 为：
> `aiohttp, certifi, durationpy, python-dateutil, pyyaml, requests, requests-oauthlib, six, urllib3, websocket-client`

#### 完整清单（可直接用于内网预下载）

```
kubernetes==36.0.3
aiohappyeyeballs==2.7.1
aiohttp==3.14.3
aiosignal==1.4.0
attrs==26.1.0
certifi==2026.7.22
charset-normalizer==3.5.1
durationpy==0.11
frozenlist==1.8.0
idna==3.20
multidict==6.9.1
oauthlib==4.0.0
propcache==0.5.4
python-dateutil==2.9.0.post0
PyYAML==6.0.3
requests==2.34.2
requests-oauthlib==2.0.0
six==1.17.0
typing_extensions==4.16.0
urllib3==2.8.0
websocket-client==1.9.2
yarl==1.25.1
```

> 这是**实测的运行时版本组合**（可复现）。`requirements.txt` 里只写 `kubernetes>=29.0.0`
> 是为了让 pip 自行解析；要完全锁定就照上面写死。

### 3.4 安装方式

#### 方式 A：用脚本（推荐）

```bash
./setup-venv.sh
# 幂等：已存在则只校验依赖。默认走清华镜像（MIRROR= 可置空走 PyPI 官方）
MIRROR= ./setup-venv.sh          # 强制走 PyPI 官方源
VENV=/opt/useradmin-venv ./setup-venv.sh   # 换别的位置
```

完成后：

```bash
./venv/bin/python useradmin.py --help
```

#### 方式 B：手工装（不想用脚本 / 要集成到自己的环境）

```bash
cd /data/ops-scripts/user-onboarding

# 建 venv
python3 -m venv venv

# 装依赖（二选一）
./venv/bin/pip install -r requirements.txt                              # PyPI 官方
./venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple   # 国内镜像

# 或锁死版本（照 3.3 的完整清单）
./venv/bin/pip install kubernetes==36.0.3

# 验证
./venv/bin/python -c "import kubernetes; print(kubernetes.__version__)"
```

#### 方式 C：内网 / 离线安装

在**有网**的机器上预下载 wheel，拷进内网：

```bash
# ── 有网机器（Linux x86_64，与目标机同架构同 Python 版本）──
mkdir -p /tmp/whl
python3 -m pip download -d /tmp/whl -r requirements.txt
# 若目标机 Python 小版本不同（如 3.11 vs 3.12），指定解释器版本：
# python3 -m pip download -d /tmp/whl -r requirements.txt \
#   --python-version 311 --only-binary=:all: --platform manylinux2014_x86_64

# ── 目标机（内网）──
python3 -m venv venv
./venv/bin/pip install --no-index --find-links=/tmp/whl -r requirements.txt
```

> `--no-index` 保证**完全不联网**，只用本地 wheel —— 内网机器上这一步能成功就说明依赖齐了。

#### 方式 D：不用 venv（不推荐）

宿主 Python 受 **PEP 668**（`externally-managed-environment`）保护，直接
`pip install` 会被拒绝。硬要装系统级需显式绕过 —— **不建议**，会污染系统 Python：

```bash
python3 -m pip install --break-system-packages -r requirements.txt   # ← 有风险
```

### 3.5 各脚本的依赖对照

| 脚本 | 需要什么 | 在哪跑 |
|---|---|---|
| `useradmin.py` | **venv 里的 kubernetes** + Python ≥3.9 | 宿主机 |
| `setup-venv.sh` | `python3` + `venv` 模块 + **网络**（首次） | 宿主机 |
| `deploy-verify.sh` | `docker`（用容器里的 kubectl）| 宿主机 |
| `verify-quota.sh` | `docker` + venv | 宿主机 |
| `verify-doc.sh` | `docker` + venv（+ 可选 `kubectl`）| 宿主机 |
| `test-as-user.sh` | **`kubectl`** + `bash` + Python3（解析 JSON）| **你本地机器**（不是宿主）|

> `test-as-user.sh` 刻意用 `kubectl` 而非 Python 客户端 ——
> 它是「模拟用户」的验证工具，要贴近用户真实的使用方式。

### 3.6 升级与卸载

```bash
# 升级依赖
./venv/bin/pip install -U -r requirements.txt

# 看装了什么
./venv/bin/pip list

# 完全重来（venv 是独立目录，删掉即可，不影响系统）
rm -rf venv && ./setup-venv.sh
```

---

## 四、命令详解

### create — 建用户

```bash
./venv/bin/python useradmin.py create <username> [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--out <path>` | `./<username>.kubeconfig` | 输出路径；**父目录会自动创建** |
| `--namespace <ns>` | = username | 命名空间名 |
| `--quota-cpu <n>` | **4** | 命名空间 CPU 总量。**`0` = 不限制** |
| `--quota-mem <size>` | **8Gi** | 命名空间内存总量。`0` = 不限制 |
| `--quota-pods <n>` | **20** | Pod 数上限。`0` = 不限制 |
| `--quota-storage <size>` | **50Gi** | 存储总量。`0` = 不限制 |
| `--limitrange` / `--no-limitrange` | **建** | 见[第五节](#五配额体系) |
| `--limitrange-max-cpu <n>` | 4 | 单容器 CPU 上限 |
| `--limitrange-max-mem <size>` | 8Gi | 单容器内存上限 |
| `--pod-security <lvl>` | `baseline` | `privileged` / `baseline` / `restricted` |
| `--server <url>` | `https://114.67.232.32:6443` | 写进 kubeconfig 的 API 地址 |
| `--ca-file <path>` | 集群 CA | 换环境时改 |
| `--dry-run` | | 只打印，不产生变更 |
| `--allow-unprotected` | | 准入策略缺失也继续（**不建议**）|

**三种配额写法**：

```bash
# ① 用默认（4C / 8Gi / 20 Pod / 50Gi）
create zhangsan --out /data/users/zhangsan.kubeconfig

# ② 显式指定
create zhangsan --quota-cpu 8 --quota-mem 16Gi --quota-pods 40 --quota-storage 200Gi \
       --out /data/users/zhangsan.kubeconfig

# ③ 完全不限制
create zhangsan --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 \
       --no-limitrange --out /data/users/zhangsan.kubeconfig
```

**执行流程（8 步）**：

```
0/8 前置检查：池隔离准入策略是否在位   ← 缺失则中止（除非 --allow-unprotected）
1/8 Namespace
2/8 ServiceAccount
3/8 Role
4/8 RoleBinding
5/8 ResourceQuota + LimitRange
6/8 长期 token Secret
7/8 生成 kubeconfig（权限 600）
8/8 自检：16 项权限边界断言           ← 不通过则退出码 1，不要交付
```

**`create` 是幂等的**：重复执行会把所有资源（含配额）收敛到命令行给定的值。
若用户误删了自己的 token Secret（他有权删自己 ns 的 secret），重跑 `create` 即可恢复 ——
那个 Secret 是**手工创建**的，控制器不会自动重建。

---

### quota — 改配额（创建后修改）

```bash
./venv/bin/python useradmin.py quota <username> [选项]
```

**语义**（关键，与前一条命令不同）：

| 情况 | 行为 |
|---|---|
| 参数**没传** | **保持原值**（不是恢复默认）|
| 传 `0` 或空串 | **取消该维度限制**（从配额里删掉对应键）|
| 四维度全传 0 | **删除整个 ResourceQuota**（回到完全不限制）|
| 值没变化 | 报 `无变更`，不发无谓请求 |
| 用户不存在 | 明确报错，退出码 1 |

| 选项 | 说明 |
|---|---|
| `--quota-cpu/mem/pods/storage` | 要改的维度，`0` = 取消该维度限制 |
| `--limitrange` / `--no-limitrange` | 创建 / 删除 LimitRange；不传 = 保持现状 |
| `--limitrange-max-cpu/mem` | 单容器上限；不传 = **沿用现值**（不会悄悄改回默认）|
| `--show` | 只显示当前限制与用量，不做任何变更 |
| `--dry-run` | 显示会改成什么，不执行 |

**示例**：

```bash
# 只看
./venv/bin/python useradmin.py quota zhangsan --show

# 只把 Pod 上限从 20 调到 50（cpu/mem/storage 不动）
./venv/bin/python useradmin.py quota zhangsan --quota-pods 50

# 扩容多项
./venv/bin/python useradmin.py quota zhangsan --quota-cpu 8 --quota-mem 16Gi

# 取消存储限制（键真的从配额里消失）
./venv/bin/python useradmin.py quota zhangsan --quota-storage 0

# 整个取消限制
./venv/bin/python useradmin.py quota zhangsan \
    --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0

# 调单容器上限
./venv/bin/python useradmin.py quota zhangsan --limitrange-max-cpu 2 --limitrange-max-mem 4Gi

# 先预览
./venv/bin/python useradmin.py quota zhangsan --quota-pods 50 --dry-run
```

> **管理员也可以绕过工具直接改**（用户自己只有读权限）：
> ```bash
> docker exec -i kubeasz kubectl -n zhangsan patch resourcequota tenant-quota \
>   -p '{"spec":{"hard":{"pods":"40"}}}'
> ```

---

### list — 查看所有用户

```bash
./venv/bin/python useradmin.py list
```

```
NAMESPACE                TENANT           PHASE         PODS / QUOTA      CPU        MEM
zhangsan                 zhangsan         Active           3 / 20        600m/4   768Mi/8Gi
lisi                     lisi             Active           1 / 不限          不限        不限
```

`不限` 表示该维度没设配额。只列出本工具创建的用户（按 `managed-by=user-onboarding` 标签筛选）。

---

### verify — 复查边界

```bash
./venv/bin/python useradmin.py verify zhangsan
```

重跑 16 项断言：RBAC 正例（4 项）/  RBAC 反例（9 项）/ 准入策略（3 项）。

**什么时候用**：

- 交付用户前后各跑一次
- 改过 `enforce-pool-isolation.yaml` 之后
- 增删过 master 节点（`infraNodes` 需要同步）之后
- 定期巡检

---

### delete — 撤销

```bash
./venv/bin/python useradmin.py delete <username> [--yes] [--out <path>] [--keep-kubeconfig]
```

做两件事：

1. **删除命名空间** —— 连带其中所有资源，SA 随之消失 → **token 立即失效**（实测 HTTP 401）
2. **删除 kubeconfig 文件** —— 路径优先从命名空间注解读取，不用你记得当初的 `--out`

**安全设计**：

- 命名空间缺 `managed-by=user-onboarding` 标签时**拒绝直接删**，会先警告并要求确认
  （防止手建的命名空间被误删）
- 不带 `--yes` 会交互确认

> ⚠️ **会连带销毁数据。** 三个 StorageClass 的 `reclaimPolicy` 都是 `Delete`，
> 删命名空间 → PVC 删除 → 底层卷回收。**要留数据先备份。**
>
> 用户手上的 kubeconfig 即使保留也**无法再认证** —— 其 token 绑定的 SA 已随命名空间删除。

---

## 五、配额体系

### ResourceQuota `tenant-quota` —— 命名空间总量

默认：**CPU 4 / 内存 8Gi / Pod 20 / 存储 50Gi**

换算要点：`requests` 与 `limits` **同值**，所以用户的 Pod 必须两类都写（配 LimitRange 就不痛）。
上限是**命名空间内所有 Pod 之和**，不是单容器。

### LimitRange `tenant-defaults` —— 单容器边界

| 项 | 值 | 作用 |
|---|---|---|
| `default`（limits 默认值）| cpu 500m / mem 512Mi | 容器没写 `limits` 时**自动补** |
| `defaultRequest` | cpu 100m / mem 128Mi | 容器没写 `requests` 时**自动补** |
| `max` | cpu 4 / mem 8Gi | **单容器**上限 |
| `min` | cpu 10m / mem 16Mi | 单容器下限 |
| PVC `min.storage` | 1Gi | 单卷下限 |

**为什么默认建它**：设了 CPU/内存**配额**却**没有** LimitRange 时，未写 `resources` 的 Pod
会因"配额无法核算"被**直接拒绝** —— 这是租户最容易撞的墙。有了它，
`kubectl create deployment web --image=...` 这种不写 resources 的写法也能正常起来。
另外还避免了 **BestEffort** Pod（无 requests/limits 在节点压力时**最先被驱逐**）。

### 容量规划

**用户的 Pod 只能落在 worker**，所以配额要从 worker 的**可分配量**倒推（不是 VM 规格）：

| 节点 | 可分配 CPU | 可分配内存 |
|---|---|---|
| k8s-worker-01 | 7500m（7.5 核）| 13.85 GiB |
| k8s-worker-02 | 7500m（7.5 核）| 13.85 GiB |
| **合计** | **15 核** | **≈ 27.7 GiB** |

```
所有用户的 requests.cpu 之和   <  15 核
所有用户的 requests.memory 之和 <  27.7 GiB
```

**默认 4 核意味着最多同时容下 3 个用户**（3 × 4 = 12 核，第 4 个就超了）。
所以按业务实际规模发，别一律发 4 核：

| 场景 | 建议配额 | 依据 |
|---|---|---|
| 个人开发/测试 | **1 核 / 2Gi / 10 Pod** | 只跑跑 demo、调试 |
| 小业务 | **2 核 / 4Gi / 20 Pod** | 2–5 个轻量服务 |
| 中业务 | 4 核 / 8Gi / 30 Pod | 需先确认总账够用 |
| 生产业务 | 单独评估 + 配 PriorityClass | 别与开发环境抢资源 |

> 注意：**master（infra）节点的 7.5 核不参与用户调度** ——
> 那里只跑平台组件（监控、operator、DNS、provisioner 等），
> 且有污点 + 准入策略双重拦截。做容量规划时**不要**把 master 算进来。

---

## 六、交接给用户

`create` 跑完会打印一整段可直接转发的说明。要点：

```bash
export KUBECONFIG=./zhangsan.kubeconfig
kubectl get pods -o wide
kubectl create deployment web --image=easzlab.io.local:5000/easzlab/pause:3.10 --replicas=2
kubectl get pods -o wide        # NODE 列只会出现 k8s-worker-0X
```

### 必须提醒用户的四件事

| # | 事项 |
|---|---|
| 1 | **不要碰 master** —— 加 `toleration`/`nodeSelector`/`nodeName` 去够 master 会被 API 拒绝，这是设计而非故障 |
| 2 | **PVC 必须写 `storageClassName`** —— 集群**没有默认 SC**，不写会一直 Pending |
| 3 | **不要用 `type: LoadBalancer`** —— 裸金属无 LB 控制器，`EXTERNAL-IP` 会永久 `<pending>` |
| 4 | **存储选型** —— `managed-nfs-storage`（共享，推荐）/ `openebs-hostpath`（节点本地）。`openebs-lvmpv` 只存在于 master，**对用户不可用** |

### 交付前建议跑一次端到端

用**用户自己的 kubeconfig** 验证（51 条断言，贴近真实使用）：

```bash
# 在能访问公网 API 的机器上
scp root@114.67.232.32:/data/users/zhangsan.kubeconfig /tmp/zs.kubeconfig
cd /Users/gengyan15/coding/kubeasz/k8s-baremetal-01/user-onboarding
bash test-as-user.sh /tmp/zs.kubeconfig zhangsan
```

---

## 七、常见任务

### 新员工入职（标准流程）

```bash
cd /data/ops-scripts/user-onboarding
./venv/bin/python useradmin.py create zhangsan --out /data/users/zhangsan.kubeconfig
# 看输出末尾的「用户 zhangsan 已就绪」整段，转发给本人
```

### 给某业务扩容

```bash
./venv/bin/python useradmin.py quota zhangsan --quota-cpu 16 --quota-mem 32Gi --quota-pods 80
# 注意：别超过集群总量（见容量规划）
```

### 临时给某人放开限制

```bash
./venv/bin/python useradmin.py quota zhangsan \
    --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0
# 事情做完记得收回来：
./venv/bin/python useradmin.py quota zhangsan --quota-cpu 4 --quota-mem 8Gi --quota-pods 20
```

### 员工离职 / 项目结束

**删除会销毁数据**（三个 SC 的 `reclaimPolicy` 都是 `Delete`）。要留就先备份：

```bash
# 1) 先看有哪些卷、挂在哪个节点
docker exec -i kubeasz kubectl -n zhangsan get pvc -o wide

# 2) 备份数据（示例：把容器里的数据拷出来）
docker exec -i kubeasz kubectl -n zhangsan exec deploy/<name> -- \
  tar cz -C /data . > /data/backup/zhangsan-$(date +%F).tar.gz

# 3) 确认无误后撤销
./venv/bin/python useradmin.py delete zhangsan
```

> `managed-nfs-storage` 的卷落在**宿主** `/data/nfs-provisioner/` 下，
> 也可以在删除前直接 `cp` 出来。`openebs-hostpath` 的卷在**节点本地**
> （`/var/openebs/local/`），需登录对应 worker 取。

### 查某用户当前用量

```bash
./venv/bin/python useradmin.py quota zhangsan --show      # 工具视角
docker exec -i kubeasz kubectl -n zhangsan get resourcequota tenant-quota -o yaml
docker exec -i kubeasz kubectl -n zhangsan get pods -o wide
```

### 新增一台 worker 节点

用户侧**无需任何改动** —— Pod 会自动用上新节点。
但要确认基础设施的 DaemonSet 覆盖到它（`calico-node` / `node-local-dns` / `node-exporter` / `openebs-lvmpv-node` 应为 N/N）。

### 新增/改名 master 节点

**必须同步更新** `enforce-pool-isolation.yaml` 里 **3 处** `infraNodes` 变量
（Pod / 工作负载 / CronJob 三个策略各一处），然后重新 `apply`：

```bash
docker exec -i kubeasz kubectl apply -f enforce-pool-isolation.yaml
```

### 新增平台命名空间

**必须打豁免标签**，否则其 Pod 会被准入策略拒绝：

```bash
docker exec -i kubeasz kubectl label ns <新ns> pool-access=privileged --overwrite
```

这是 **fail-closed** 设计的代价：默认全部受管，平台 ns 逐个豁免。
好处是新增租户自动受保护，忘了打标签是**可见的失败**而不是静默的洞。

---

## 八、排错

### 用户说 `kubectl` 报 Unauthorized

token 失效了。最常见原因：用户自己 `kubectl delete secret --all` 把 `<user>-token` 删了。

**恢复**（幂等，会重建 Secret 并刷新 kubeconfig）：

```bash
./venv/bin/python useradmin.py create zhangsan --out /data/users/zhangsan.kubeconfig
# 把新的 kubeconfig 重新发给本人
```

### 用户说 Pod 一直 Pending

```bash
docker exec -i kubeasz kubectl -n zhangsan describe pod <pod> | sed -n '/Events/,$p'
```

| 事件关键字 | 原因 |
|---|---|
| `exceeded quota` | 配额用尽 → `useradmin.py quota zhangsan --show` 看余量 |
| `untolerated taint` | 不该出现；用户若在 master 上会遇到，检查是否有人手改过节点 |
| `didn't match Pod's node affinity` | 用户写了 `nodeSelector` 指向不存在的标签 |
| `No storage class` / 无 PV | PVC 没写 `storageClassName`，或写了 `openebs-lvmpv`（用户不可用）|
| `FailedScheduling` + 无其他原因 | worker 资源不足 → 看 `kubectl top nodes` |

### 用户说 PVC 一直 Pending

最常见是**没写 `storageClassName`**（集群无默认 SC）。其次是写了 `openebs-lvmpv`
（只存在于 master，用户的 Pod 到不了那里，`WaitForFirstConsumer` 永远不触发）。

### 用户说改不了东西 / Forbidden

这是**预期行为**。让他看交付说明末尾的"你【不能】做的事"清单。
如果确实需要某项集群级权限，由管理员评估后单独授权 —— **不要**直接把 `cluster-admin` 绑给他。

### `verify` 报某项失败

| 失败项 | 排查 |
|---|---|
| `正例：可在本 ns 创建 deployments` | Role/RoleBinding 是否还在：`kubectl -n <ns> get role,rolebinding` |
| `反例：不可访问 kube-system` 失败 | 有人给了该 SA 越权绑定 → 查 `kubectl get rolebinding,clusterrolebinding -A -o json` 里的 subject |
| `反例：带 infra 容忍的 Pod 被拒绝` 失败 | 准入策略没生效 → `kubectl get validatingadmissionpolicy`，重跑 `deploy-verify.sh` |
| `正例：普通 Pod 能正常调度` 失败 | worker 资源不足，或节点 NotReady |

### 想确认隔离是否真的生效（手工反例）

> 在**你自己的机器**上跑（**宿主机没有 kubectl**，它只在 kubeasz 容器里）。
> 先把用户 kubeconfig 拷到本地。

```bash
# 以用户身份建一个带 infra toleration 的 Pod，应被拒绝
kubectl --kubeconfig /tmp/zs.kubeconfig -n zhangsan apply -f - <<'EOF'
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
# 期望输出含「禁止容忍 infra 污点」
```

---

## 九、约束与坑

| # | 约束 | 说明 |
|---|---|---|
| 1 | **`create` 会把配额重置** | 调整已有用户请用 `quota`，别重跑 `create` |
| 2 | **删用户会毁数据** | 三个 SC 的 `reclaimPolicy` 都是 `Delete`；要留先备份 |
| 3 | **`infraNodes` 硬编码 3 处** | master 改名/扩容必须同步改策略文件，否则 `nodeName` 直绑那条校验会漏 |
| 4 | **新增平台 ns 要打豁免标签** | 否则其 Pod 被拒（fail-closed 的代价）|
| 5 | **别用 `kubectl delete secret --all`** | 会删掉 SA 的 token Secret，且控制器不会重建 → kubeconfig 永久失效 |
| 6 | **token 不过期** | 用的是 legacy SA token Secret（故意如此，适合"长期发给一个人"）。**撤销只能靠 `delete`**（删 ns → SA 消失 → token 立即失效）|
| 7 | **宿主没有 kubectl** | kubectl 只在 kubeasz 容器里；`useradmin.py` 走 API，不依赖 kubectl |
| 8 | **`openebs-lvmpv` 对用户不可用** | 它的 VG 只在 master 上，而用户去不了 master |
| 9 | **无默认 StorageClass** | PVC 必须显式写 `storageClassName` |
| 10 | **无 LoadBalancer** | 裸金属没 LB 控制器，别建 `type: LoadBalancer` |
| 11 | **本工具不做集群级授权** | 它只能开命名空间租户。要给运维/审计**集群级**权限（含全集群只读），用 kubeasz 自带的 `ezctl kcfg-adm` —— 见[附录 B](#附录-b与-kubeasz-自带-kcfg-adm-的关系) |
| 12 | **本工具的 kubeconfig 内嵌 CA** | 若执行过 `ezctl kca-renew`（换 CA），需重跑 `create` 刷新；用户的 **token 不受影响** —— 见 [B.5](#b5-kca-renew-会连带影响本工具) |

---

## 附录 A：隔离原理

### 为什么光靠污点不够

infra（master）节点的隔离靠 taint `node-role.kubernetes.io/infra:NoSchedule`，但：

1. **污点是公开信息** —— `kubectl describe node` 就能看到。用户写一条匹配的 `tolerations` 就能调度上去。
2. **`spec.nodeName` 直绑绕过调度器** —— 而 kubelet 只检查 `NoExecute` 污点、**不检查 `NoSchedule`**：

   ```go
   // pkg/kubelet/lifecycle/predicate.go:448-457
   // Check taint/toleration except for static pods
   // Kubelet is only interested in the NoExecute taint.
   return t.Effect == v1.TaintEffectNoExecute
   ```

   → 直绑到 master 的 Pod，kubelet 照跑。**这一条是最危险的绕过路径。**

### 所以加了准入层

`enforce-pool-isolation.yaml` = 3 个 ValidatingAdmissionPolicy + 3 个 Binding：

| 策略 | 覆盖对象 | 覆盖路径 |
|---|---|---|
| `tenant-pool-guard-pods` | Pod | `object.spec` |
| `tenant-pool-guard-workloads` | Deployment / StatefulSet / DaemonSet / ReplicaSet / ReplicationController / Job | `object.spec.template.spec` |
| `tenant-pool-guard-cronjobs` | CronJob | `object.spec.jobTemplate.spec.template.spec` |

每个策略 5 条校验：

1. 不得容忍 infra 污点（挡 toleration 绕过）
2. 不得用 `nodeSelector` 选中 infra
3. 不得用 `nodeName` 直绑 infra（**绕过调度器，最危险**）
4. 不得用 `nodeAffinity` 硬亲和选中 infra
5. 不得抢占 `system-node-critical` / `system-cluster-critical` 优先级类

**作用域（fail-closed）**：

```
命名空间默认【全部受管】 ──┬─→ pool-access != privileged → 受策略管辖（用户 ns）
                          └─→ pool-access == privileged → 豁免（平台 ns）
```

已豁免：`kube-system`、`monitor`、`openebs`、`kb-system`。

### 一个 CEL 陷阱（已踩过）

准入策略的 CEL 里**访问不存在的字段会直接报错**，而报错 = 校验失败 = 请求被拒：

```
expression '!(variables.infraKey in variables.s.nodeSelector)' resulted in error: no such key: nodeSelector
```

Pod 没有 `nodeSelector` 时就会撞上 → **全集群 Pod 创建被阻断**。
所以每个可能缺省的字段（`nodeSelector`/`nodeName`/`tolerations`/`priorityClassName`/`affinity`）
都必须用 `has()` 守卫：

```
!has(variables.s.nodeSelector) || !(variables.infraKey in variables.s.nodeSelector)
```

这也是 `deploy-verify.sh` 那个"安全闸"存在的原因。

---

## 附录 B：与 kubeasz 自带 `kcfg-adm` 的关系

`ezctl` 自带两个「Extra operation」，**与本工具并存，不要重复造轮子**：

```bash
ezctl kcfg-adm <cluster> <args>    # 管理人用的客户端 kubeconfig
ezctl kca-renew <cluster>          # 重建集群 CA 与全部证书（慎用）
```

**一句话区别**：`kcfg-adm` 管**集群级身份**，本工具管**命名空间级租户**。

### B.1 `kcfg-adm` 是什么

| 参数 | 含义 |
|---|---|
| `-A` | 新增客户端 kubeconfig（**自动签发新用户证书**）|
| `-D` | 删除（删 ClusterRoleBinding + 服务端证书文件）|
| `-L` | 列出用户及证书**到期时间** |
| `-e <N>h` | 证书有效期。**默认 `4800h`（200 天）** |
| `-t admin\|view` | `admin` → ClusterRole **`cluster-admin`**；`view` → 内置聚合角色 **`view`** |
| `-u <name>` | 用户名前缀（**会自动追加 `-YYYYMMDDHHMM` 时间戳**）|

```bash
ezctl kcfg-adm k8s-baremetal-01 -L                                # 列出现有用户+到期
ezctl kcfg-adm k8s-baremetal-01 -A -e 24h   -t admin -u oncall    # 24h 临时超管
ezctl kcfg-adm k8s-baremetal-01 -A -e 4800h -t view  -u auditor   # 只读观察者
ezctl kcfg-adm k8s-baremetal-01 -D -u oncall-202609281830         # 撤销（名字要带时间戳）
```

产物：`/etc/kubeasz/clusters/<cluster>/ssl/users/<user>.kubeconfig`

### B.2 逐项对比（均读源码/实测）

| 维度 | `ezctl kcfg-adm` | 本工具 `useradmin.py` |
|---|---|---|
| **身份类型** | X.509 客户端证书（CA 签发，`CN=<user>`、`O=k8s`）| ServiceAccount token（Secret）|
| **k8s 主体** | `User: <user>-<时间戳>` | `ServiceAccount: <ns>:tenant-user` |
| **权限载体** | **ClusterRoleBinding** | **RoleBinding**（namespace 级）|
| **权限范围** | `admin` = **整集群超管**<br>`view` = **所有**命名空间只读（13 条规则 / 48 种资源）| **仅自己那一个命名空间** |
| **命名空间隔离** | ❌ 无 | ✅ 硬隔离 |
| **工作负载落点隔离** | ❌ **完全没有** —— admin 可给 master 打掉污点 | ✅ 污点 + 准入策略双重拦截 |
| **资源配额** | ❌ 无 | ✅ ResourceQuota + LimitRange |
| **有效期** | ✅ **有**（`-e`，默认 200 天）| ❌ 无（不过期）|
| **撤销后立即失效** | ⚠️ 只收回权限，**不吊销身份**（见 B.3①）| ✅ 删 ns → SA 消失 → token 立即 401 |
| **kubeconfig 的 server** | `https://192.168.150.11:6443`（**master-01 内网 IP**）| `https://114.67.232.32:6443`（**公网**）|
| **需要 CA 私钥** | ✅ **需要**（`ca-key.pem`，因为要签新证书）| ❌ 不需要，只走 API |
| **运行位置** | kubeasz 容器内 `ezctl` | 宿主机 Python venv |
| **审计** | `-L` 读**服务端证书文件**的到期时间 | `list` 读 **API 实时**状态（配额用量）|

### B.3 三个必须知道的坑

**① 撤销 ≠ 吊销（证书无法吊销）**

`-D` 的实现：

```bash
CRB=$(... get clusterrolebindings -ojsonpath="{.items[?(@.subjects[0].name == '$USER_NAME')].metadata.name}") && \
bin/kubectl ... delete clusterrolebindings "$CRB" && \
/bin/rm -f "clusters/$1/ssl/users/$USER_NAME"*     # ← 只删【服务端】副本
```

**删的是服务器上的文件。用户手里那份内嵌证书和私钥依然有效** —— k8s 没有 CRL/OCSP，
签发出去的客户端证书**无法吊销**。

| | 含义 |
|---|---|
| `-D` 之后 | ✅ **权限**确实收回了（CRB 没了）|
| 但 | ⚠️ **身份**未被吊销。若管理员误重建同名 CRB，或该证书恰好匹配别的授权，它会**立刻复活** |
| 想彻底作废 | 只能**等它过期**，或走 `kca-renew` 换 CA |

本工具的 `delete` 无此问题：SA 随命名空间一起消失，token 立即失效（实测 HTTP 401）。

**② kubeconfig 指向内网 IP，不能直接对外交付**

```yaml
# roles/deploy/vars/main.yml
KUBE_APISERVER: "https://{{ groups['kube_master'][0] }}:{{ SECURE_PORT }}"
```

`groups['kube_master'][0]` = **192.168.150.11**（master-01 内网地址），
**既不是** VIP `192.168.150.100`，**也不是**公网 `114.67.232.32`。

→ 用 `kcfg-adm` 发出去的 kubeconfig **只能在 192.168.150.0/24 内使用**
（宿主、VM 内）。要给外部的人用，得手动改 server —— 或本工具（默认就写公网地址）。

**③ `-A` 需要 CA 私钥 —— 权限面更大**

```yaml
# add-custom-kubectl-kubeconfig.yml
shell: "cfssl gencert -ca=.../ca.pem -ca-key=.../ca-key.pem ... -profile=kcfg ..."
```

| | 能做什么 | 风险 |
|---|---|---|
| `kcfg-adm -A` | 用 **CA 私钥签发任意用户证书** | 能跑它的人 = **能伪造任意身份**（CN 随便填、O 填 `system:masters` 即超管）|
| `useradmin.py create` | 仅用 cluster-admin **API 权限**创建受 Role 约束的 SA | 改不了身份体系，只能绑**已有** Role |

**所以本工具的权限面更小**：它无法签发伪造身份的凭据。

### B.4 怎么选

| 需求 | 用 |
|---|---|
| 给运维**临时**放权，且**希望自动过期** | `kcfg-adm -A -t admin -e 24h` |
| 给审计/观察者**全集群只读** | `kcfg-adm -A -t view` ← **本工具没有等价物** |
| 给业务开发者**只能操作自己命名空间** | `useradmin.py create` |
| 需要**撤销即生效** | `useradmin.py delete` |
| 需要**资源配额** | `useradmin.py create --quota-*` |
| 需要**限制工作负载只落 worker** | `useradmin.py create` |
| kubeconfig 需**从公网可用** | `useradmin.py create` |
| 需要**长期不过期**的账号 | `useradmin.py create` |

**推荐分工**：

```
运维 / 审计（集群级、临时）   →  ezctl kcfg-adm      （善用 -e 到期）
业务用户（命名空间级、长期）  →  useradmin.py create
```

> ⚠️ **不要用 `kcfg-adm -t admin` 代替本工具** —— 那等于给业务用户发超管权限，
> 命名空间隔离、节点落点隔离、资源配额**全部失效**。

### B.5 `kca-renew` 会连带影响本工具

```bash
ezctl kca-renew <cluster>
# → ansible-playbook -e CHANGE_CA=true playbooks/96.update-certs.yml -t force_change_certs
```

**仅当 admin 凭据泄露时使用** —— 重建 CA 并重签全部证书，逐节点分发并重启组件。

对你这里的影响：

| 对象 | 是否受影响 |
|---|---|
| 用户的 **token** | ❌ 不受影响（SA token 由 apiserver 签，不依赖集群 CA）|
| 用户 kubeconfig 里**内嵌的 CA** | ✅ **受影响** —— 客户端会报证书验证失败 |
| 用 `kcfg-adm` 发的证书 | ✅ **全部失效**（旧 CA 签的）|
| 你本地那份 `config`（admin 证书）| ✅ **立即失效** |

**换完 CA 后要重新生成用户 kubeconfig**（幂等，会刷新内嵌 CA）：

```bash
for u in zhangsan lisi; do
  ./venv/bin/python useradmin.py create $u --out /data/users/$u.kubeconfig
done
```

> 这也解释了本手册里那个悬而未决的点：`CHANGE_CA: false`（复用旧 CA）省事，
> 但**若旧 CA 私钥曾泄露，复用等于没换锁** —— 这正是 `kca-renew` 存在的意义。

---

## 附录 C：文件清单

| 文件 | 作用 |
|---|---|
| `enforce-pool-isolation.yaml` | **安全设施本体**（3 策略 + 3 绑定）。改它 = 改隔离规则 |
| `useradmin.py` | 主程序：`create` / `quota` / `verify` / `list` / `delete` |
| `setup-venv.sh` | 建独立 venv 并装 `kubernetes` 客户端（宿主 pip 受 PEP 668 保护）|
| `requirements.txt` | 唯一依赖：`kubernetes>=29` |
| `test-as-user.sh` | 端到端验证：用**用户自己的 kubeconfig** 跑 51 条断言 |
| `verify-quota.sh` | 配额行为回归（12 场景：默认/指定/不限制/修改/取消/幂等）|
| `verify-doc.sh` | **文档回归**：把 README 各章的示例命令实跑一遍（18 项），防文档腐化 |
| `deploy-verify.sh` | 首次部署准入策略（带"误杀则自动回滚"安全闸）|
| `venv/` | Python 虚拟环境（可随时删掉重建：`./setup-venv.sh`）|

**用户数据**：`/data/users/<username>.kubeconfig`（权限 600）

---

## 附录 D：命令速查表

| 目的 | 命令 |
|---|---|
| 建用户（默认配额）| `./venv/bin/python useradmin.py create <u> --out /data/users/<u>.kubeconfig` |
| 建用户（自定义）| `... create <u> --quota-cpu 8 --quota-mem 16Gi --quota-pods 40` |
| 建用户（不限制）| `... create <u> --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0 --no-limitrange` |
| 列出所有用户 | `... list` |
| 看某用户配额 | `... quota <u> --show` |
| 改配额 | `... quota <u> --quota-pods 50` |
| 取消某维度限制 | `... quota <u> --quota-storage 0` |
| 全部取消限制 | `... quota <u> --quota-cpu 0 --quota-mem 0 --quota-pods 0 --quota-storage 0` |
| 复查边界 | `... verify <u>` |
| 撤销 | `... delete <u>` |
| 预览不执行 | 任意命令加 `--dry-run`（`quota` / `create` 支持）|
| 看用户 Pod 落哪 | `docker exec -i kubeasz kubectl -n <u> get pods -o wide` |
| 看用户用量 | `docker exec -i kubeasz kubectl -n <u> get resourcequota tenant-quota -o yaml` |
| 看隔离策略状态 | `docker exec -i kubeasz kubectl get validatingadmissionpolicy,binding` |
| 看豁免的 ns | `docker exec -i kubeasz kubectl get ns -L pool-access` |
