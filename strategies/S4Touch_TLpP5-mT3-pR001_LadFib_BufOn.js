/**
 * @filename S4Touch_TLpP5-mT3-pR001_LadFib_BufOn.js
 * @description فروش در برخورد و بازگشت قیمت از یک خط روند نزولی (مقاومت) — ورود روی لمس و بازگشت خط، نه شکست آن. pivotPeriod=5، minTouchPoints=3، precision=0.001 | حد سود ممنوع (حذف شد)، خروج فقط با حد ضرر پلکانی = فیبوناچی تعدیل‌شده (Adjusted-Fibonacci)، بافر فعال (enableSmartContinuation: true)
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
  { movePercent: 0.4, stopLossPercent: -0.2 },
  { movePercent: 2.4, stopLossPercent: -1.9 },
  { movePercent: 4.2, stopLossPercent: -3.5 },
  { movePercent: 6.1, stopLossPercent: -5.0 },
  { movePercent: 8.0, stopLossPercent: -6.6 },
  { movePercent: 9.9, stopLossPercent: -8.2 },
  { movePercent: 11.8, stopLossPercent: -9.9 },
  { movePercent: 13.7, stopLossPercent: -11.6 },
  { movePercent: 15.6, stopLossPercent: -13.4 },
  { movePercent: 17.6, stopLossPercent: -15.3 },
  { movePercent: 19.6, stopLossPercent: -17.2 },
  { movePercent: 21.6, stopLossPercent: -19.1 },
  { movePercent: 23.7, stopLossPercent: -21.1 },
  { movePercent: 25.8, stopLossPercent: -23.1 },
  { movePercent: 27.9, stopLossPercent: -25.1 },
  { movePercent: 30.0, stopLossPercent: -27.2 },
  { movePercent: 32.2, stopLossPercent: -29.3 },
  { movePercent: 34.4, stopLossPercent: -31.4 },
  { movePercent: 36.6, stopLossPercent: -33.5 },
  { movePercent: 38.8, stopLossPercent: -35.6 },
  { movePercent: 41.0, stopLossPercent: -37.7 },
  { movePercent: 43.3, stopLossPercent: -39.8 },
  { movePercent: 45.6, stopLossPercent: -41.9 },
  { movePercent: 47.9, stopLossPercent: -44.1 },
  { movePercent: 50.2, stopLossPercent: -46.2 },
];

function customStrategy(data, index, breakPointsParam, _ichimokuUnused, trendLinesParam, refineEntryPrice) {
  if (index < 61) return null;

  // سیگنال فقط بر اساس آخرین کندل کاملاً بسته‌شده بررسی می‌شود (جلوگیری از آینده‌نگری/ریپینت)
  const sigIndex = index - 1;

  if (!globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadFib_BufOn || globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadFib_BufOn.dataRef !== data) {
    globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadFib_BufOn = { dataRef: data, lastTouchIndex: {} };
  }
  const st = globalThis.__state_S4Touch_TLpP5_mT3_pR001_LadFib_BufOn;

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
