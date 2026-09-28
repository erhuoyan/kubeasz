#!/usr/bin/env bash
# ============================================================================
# setup-venv.sh — 为本工具准备独立的 Python 虚拟环境
#
# 为什么用 venv 而不是系统 pip:
#   · 宿主 python3 受 PEP 668 保护（externally-managed-environment），
#     直接 pip install 会被拒或被 --break-system-packages 破坏
#   · 独立目录便于删除、不污染系统；随本工具一起备份/迁移
#
# 幂等：已存在则只做依赖校验（--upgrade 可选）
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HERE/venv}"
REQ="$HERE/requirements.txt"
# 国内加速镜像（PyPI 官方可达时也可用 MIRROR= 置空）
MIRROR="${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "找不到 python3"
python3 -c 'import venv' 2>/dev/null || die "python3 缺少 venv 模块（Debian/Ubuntu: apt install python3-venv）"

if [ ! -d "$VENV" ]; then
  log "创建虚拟环境 $VENV"
  python3 -m venv "$VENV"
  ok "已创建"
else
  log "虚拟环境已存在：$VENV"
fi

PIP="$VENV/bin/pip"
log "安装依赖（$REQ）"
if [ -n "$MIRROR" ]; then
  "$PIP" install -q --upgrade pip -i "$MIRROR" >/dev/null 2>&1 || true
  "$PIP" install -q -r "$REQ" -i "$MIRROR"
else
  "$PIP" install -q --upgrade pip >/dev/null 2>&1 || true
  "$PIP" install -q -r "$REQ"
fi

log "校验"
"$VENV/bin/python" - <<'PY'
import kubernetes, sys
print("  ✓ kubernetes %s  (python %s)" % (kubernetes.__version__, sys.version.split()[0]))
PY

cat <<EOF

就绪。用法:

  $VENV/bin/python $HERE/useradmin.py create alice --quota-cpu 4 --quota-mem 8Gi
  $VENV/bin/python $HERE/useradmin.py delete alice
  $VENV/bin/python $HERE/useradmin.py list

或写个别名:
  alias useradmin='$VENV/bin/python $HERE/useradmin.py'
EOF
