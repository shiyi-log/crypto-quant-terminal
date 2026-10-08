#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  多模型对比扫描
#
#  依次训练不同的 FreqAI 模型类，每个模型用独立 identifier，
#  完成后产物落在 user_data/models/<identifier>/ 下，便于横向对比。
#
#  内置的「等待空闲」逻辑让它可以在已有任务运行时启动，
#  它会等当前训练结束后再继续，不会互相抢占资源。
#
#  用法:
#    ./run_model_sweep.sh                 # 跑全部
#    TIMERANGE=20250601-20261008 ./run_model_sweep.sh
#    MODELS="PyTorchMLPRegressor" ./run_model_sweep.sh
# ══════════════════════════════════════════════════════════════
set -uo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate

TIMERANGE="${TIMERANGE:-20250101-20261008}"
STRATEGY="${STRATEGY:-FreqaiFundingStrategy1h7d}"
BASE_CONFIG="user_data/config_universe15.json"

# 模型 -> identifier 短名
MODELS="${MODELS:-XGBoostRegressor PyTorchMLPRegressor PyTorchTransformerRegressor}"

label_for() {
  case "$1" in
    XGBoostRegressor) echo "xgb" ;;
    LightGBMRegressor) echo "lgb" ;;
    PyTorchMLPRegressor) echo "mlp" ;;
    PyTorchTransformerRegressor) echo "trf" ;;
    *) echo "$(echo "$1" | tr '[:upper:]' '[:lower:]')" ;;
  esac
}

gen_config() {
  local conf="$1" ident="$2" model="$3"
  python3 - "$conf" "$ident" "$model" "$BASE_CONFIG" <<'PY'
import json, sys
conf, ident, model, base = sys.argv[1:5]
cfg = json.load(open(base))
cfg['freqai']['identifier'] = ident
cfg['bot_name'] = ident

# 不同模型类的训练超参不同，不能共用一套
if model.startswith('XGBoost'):
    cfg['freqai']['model_training_parameters'] = {
        "n_estimators": 300, "learning_rate": 0.05, "max_depth": 6,
        "subsample": 0.8, "colsample_bytree": 0.8, "random_state": 1,
    }
elif model.startswith('LightGBM'):
    cfg['freqai']['model_training_parameters'] = {
        "n_estimators": 300, "learning_rate": 0.05, "num_leaves": 31,
        "subsample": 0.8, "colsample_bytree": 0.8, "random_state": 1,
    }
else:
    # PyTorch 系列：优化器与训练轮次参数
    cfg['freqai']['model_training_parameters'] = {
        "n_epochs": 12, "learning_rate": 0.0008, "batch_size": 64,
    }
json.dump(cfg, open(conf, 'w'), indent=4)
PY
}

mkdir -p logs
SUMMARY="logs/model_sweep_summary.txt"
: >"$SUMMARY"

for MODEL in $MODELS; do
  SLUG="$(label_for "$MODEL")"
  IDENT="universe15-${SLUG}-1h-7d"
  CONF="user_data/config_${IDENT}.json"

  echo ""
  echo "════════════════════════════════════════════════════"
  echo " 模型: $MODEL"
  echo " identifier: $IDENT"
  echo "════════════════════════════════════════════════════"

  # 等待已有训练任务结束，避免资源抢占与进度文件互相覆盖
  WAITED=0
  while pgrep -f "freqtrade backtesting" >/dev/null 2>&1; do
    if [ "$WAITED" = "0" ]; then echo "  已有训练在运行，等待其结束..."; fi
    WAITED=$((WAITED + 1))
    sleep 20
  done
  [ "$WAITED" != "0" ] && echo "  前序任务已结束，继续。"

  if [ -d "user_data/models/$IDENT/backtesting_predictions" ]; then
    N=$(ls "user_data/models/$IDENT/backtesting_predictions" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$N" -gt 0 ]; then
      echo "  ✅ 已有 $N 个预测，跳过"
      echo "$MODEL | $IDENT | 已存在($N)" >>"$SUMMARY"
      continue
    fi
  fi

  gen_config "$CONF" "$IDENT" "$MODEL"
  echo "  开始训练..."

  START=$(date +%s)
  python run_backtest_task.py --config "$CONF" --strategy "$STRATEGY" \
    --freqaimodel "$MODEL" --timerange "$TIMERANGE" 2>&1 | tail -3
  RC=$?
  ELAPSED=$(( $(date +%s) - START ))

  echo "$MODEL | $IDENT | rc=$RC | ${ELAPSED}s" >>"$SUMMARY"
  echo "  完成，用时 ${ELAPSED}s (rc=$RC)"

  # 归档：把最新回测结果写成摘要 + 逐笔明细（含决策依据）
  ZIP="$(ls -t user_data/backtest_results/*.zip 2>/dev/null | head -1)"
  if [ -n "$ZIP" ]; then
    echo "  归档中: $(basename "$ZIP") → $IDENT"
    python archive_run.py --zip "$ZIP" --identifier "$IDENT" 2>&1 | sed 's/^/    /' || \
      echo "    ⚠ 归档失败（可稍后手动运行: python archive_run.py --zip <zip> --identifier $IDENT）"
  fi
done

echo ""
echo "════════════════════════════════════════════════════"
echo " 全部模型训练完成"
cat "$SUMMARY"
echo "════════════════════════════════════════════════════"
