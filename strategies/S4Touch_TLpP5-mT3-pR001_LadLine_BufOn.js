/**
 * @filename S4Touch_TLpP5-mT3-pR001_LadLine_BufOn.js
 * @description فروش در برخورد و بازگشت قیمت از یک خط روند نزولی (مقاومت) — ورود روی لمس و بازگشت خط، نه شکست آن. pivotPeriod=5، minTouchPoints=3، precision=0.001 | حد سود ممنوع (حذف شد)، خروج فقط با حد ضرر پلکانی = خطی پویا (Dynamic-Linear)، بافر فعال (enableSmartContinuation: true)
 */

const stopLossInitial = 0.4;

const ANALYSIS_CONFIG = {
  entryType: "nextCandle",
  breakTolerance: 0.02,
  trendLines: {
    pivotPeriod: 5,
    minTouchPoints: 3,
    minCandleDistance: 3,
    precision: 0.001
  },
  enableSmartContinuation: true
};

const stopLossStages = [
  { movePercent: 0.4, stopLossPercent: -0.3 },
  { movePercent: 2.4, stopLossPercent: -2.0 },
  { movePercent: 4.4, stopLossPercent: -3.7 },
  { movePercent: 6.4, stopLossPercent: -5.3 },
  { movePercent: 8.4, stopLossPercent: -7.0 },
  { movePercent: 10.4, stopLossPercent: -8.7 },
  { movePercent: 12.4, stopLossPercent: -10.3 },
  { movePercent: 14.4, stopLossPercent: -12.0 },
  { movePercent: 16.4, stopLossPercent: -13.7 },
  { movePercent: 18.4, stopLossPercent: -15.3 },
  { movePercent: 20.4, stopLossPercent: -17.0 },
  { movePercent: 22.4, stopLossPercent: -18.7 },
  { movePercent: 24.4, stopLossPercent: -20.3 },
  { movePercent: 26.4, stopLossPercent: -22.0 },
  { movePercent: 28.4, stopLossPercent: -23.7 },
  { movePercent: 30.4, stopLossPercent: -25.3 },
  { movePercent: 32.4, stopLossPercent: -27.0 },
  { movePercent: 34.4, stopLossPercent: -28.7 },
  { movePercent: 36.4, stopLossPercent: -30.3 },
  { movePercent: 38.4, stopLossPercent: -32.0 },
  { movePercent: 40.4, stopLossPercent: -33.7 },
  { movePercent: 42.4, stopLossPercent: -35.3 },
  { movePercent: 44.4, stopLossPercent: -37.0 },
  { movePercent: 46.4, stopLossPercent: -38.7 },
  { movePercent: 48.4, stopLossPercent: -40.3 },
];

function customStrategy(data, index, breakPointsParam, _ichimokuUnused, trendLinesParam, refineEntryPrice) {
  if (index < 61) return null;

  // سیگنال فقط بر اساس آخرین کندل کاملاً بسته‌شده بررسی می‌شود (جلوگیری از آینده‌نگری/ریپینت)
  const sigIndex = index - 1;

  if (!globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadLine_BufOn || globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadLine_BufOn.dataRef !== data) {
    globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadLine_BufOn = { dataRef: data, lastTouchIndex: {} };
  }
  const st = globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadLine_BufOn;

  const activeLines = trendLinesParam || getTrendLines();
  if (activeLines.length === 0) return null;

  // خطوط نزولی (مقاومت) — به دنبال «برخورد و بازگشت»، نه شکست
  const downLines = activeLines.filter(line => {
    const isDown = line.type === 'primaryDown' || line.type === 'manualDown';
    const slope = line.slope || ((line.endPrice - line.startPrice) / (line.endIndex - line.startIndex));
    return isDown && slope < 0;
  });
  if (downLines.length === 0) return null;

  const TOUCH_TOLERANCE = 0.0015;
  const candle = data[sigIndex];

  let bestLine = null;
  let bestDiff = Infinity;

  for (const line of downLines) {
    if (isTrendLineBroken(line, sigIndex)) continue;
    if (sigIndex < line.startIndex) continue;

    const lineValue = calculateTrendLineValue(line, sigIndex);
    if (!lineValue || lineValue <= 0) continue;

    const distPercent = Math.abs(candle.high - lineValue) / lineValue;
    if (distPercent > TOUCH_TOLERANCE) continue;

    // شرط بازگشت: کندل نزولی و بسته‌شدن زیر خط (یعنی خط را نشکسته)
    if (candle.close >= candle.open) continue;
    if (candle.close >= lineValue) continue;

    const lastIdx = st.lastTouchIndex[line.id];
    if (lastIdx !== undefined && sigIndex - lastIdx < 5) continue;

    if (distPercent < bestDiff) {
      bestDiff = distPercent;
      bestLine = line;
    }
  }

  if (!bestLine) return null;
  st.lastTouchIndex[bestLine.id] = sigIndex;

  const entryPrice = data[index].open;
  const stopLoss = entryPrice * (1 + 0.004);

  return {
    signal: 'SELL',
    price: entryPrice,
    stopLoss: stopLoss,
    useStagedStopLoss: true,
    stopLossStages: stopLossStages
  };
}
