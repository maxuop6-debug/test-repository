/**
 * @filename S4Ic14-30-57_TLpP5-mT3-pR04_LadLine_BufOn.js
 * @description فروش با تایید ایچیموکو (14,30,57)، حد ضرر 0.4%، خط روند با pivotPeriod=5، minTouchPoints=3، precision=0.04 | اصلاح‌شده: حد سود حذف شد، حد ضرر پلکانی = خطی پویا (Dynamic-Linear)، بافر فعال (enableSmartContinuation: true)
 */

const stopLossInitial = 0.4;

const ANALYSIS_CONFIG = {
  entryType: "nextCandle",
  breakTolerance: 0.02,
  trendLines: {
    pivotPeriod: 5,
    minTouchPoints: 3,
    minCandleDistance: 3,
    precision: 0.04
  },
  ichimoku: {
    enabled: true,
    tenkanPeriod: 14,
    kijunPeriod: 30,
    senkouBPeriod: 57,
    useCloudFilter: true,
    useTKCross: true,
    useChikou: false
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

function customStrategy(data, index, breakPointsParam, ichimokuParam, trendLinesParam, refineEntryPrice) {
  if (index < 61) return null;

  if (!globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn || globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.dataRef !== data) {
    globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn = { dataRef: data, brokenLines: new Set() };
  }

  // اسنپ‌شات ایچیموکوی کندل قبلی برای جلوگیری از آینده‌نگری (lookahead bias)
  const __prevIchimoku = globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.__lastIchimokuIndex === index - 1 ? globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.__lastIchimoku : null;
  globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.__lastIchimoku = ichimokuParam ? { ...ichimokuParam } : null;
  globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.__lastIchimokuIndex = index;
  const brokenLines = globalThis.__state_S4Ic14_30_57_TLpP5_mT3_pR04_LadLine_BufOn.brokenLines;

  const activeLines = trendLinesParam || getTrendLines();
  if (activeLines.length === 0) return null;

  if (!__prevIchimoku || __prevIchimoku.kumoTop === null || __prevIchimoku.kumoTop === undefined) return null;
  if (!__prevIchimoku.tenkan || !__prevIchimoku.kijun) return null;
  if (!__prevIchimoku.isPriceBelowCloud || __prevIchimoku.isTenkanAboveKijun) return null;

  const upLines = activeLines.filter(line => {
    const isUp = line.type === 'primaryUp' || line.type === 'manualUp';
    const slope = line.slope || ((line.endPrice - line.startPrice) / (line.endIndex - line.startIndex));
    return isUp && slope > 0;
  });
  if (upLines.length === 0) return null;

  const breaks = getBreakPointsAtCandle(index);
  if (!breaks || breaks.length === 0) return null;

  const downBreaks = breaks.filter(b => b.direction === 'down');
  if (downBreaks.length === 0) return null;

  const TARGET = 0.12;
  let selectedLine = null;
  let bestDiff = Infinity;

  for (const breakInfo of downBreaks) {
    const line = upLines.find(l => l.id === breakInfo.lineId);
    if (!line) continue;
    if (brokenLines.has(line.id)) continue;

    const breakPrice = breakInfo.breakPrice;
    const lineValue = breakInfo.lineValueAtBreak;
    const diffPercent = ((lineValue - breakPrice) / lineValue) * 100;

    if (Math.abs(diffPercent - TARGET) < Math.abs(bestDiff - TARGET)) {
      bestDiff = diffPercent;
      selectedLine = line;
    }
  }

  if (!selectedLine) return null;
  brokenLines.add(selectedLine.id);

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