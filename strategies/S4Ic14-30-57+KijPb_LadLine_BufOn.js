/**
 * @filename S4Ic14-30-57+KijPb_LadLine_BufOn.js
 * @description فروش پول‌بک در روند نزولی ایچیموکو (14,30,57): وقتی روند کلی نزولی است (قیمت زیر ابر، تنکان زیر کیجون) ولی کندل تا نزدیکی خط کیجون‌سن بالا می‌رود و دوباره زیر آن بسته می‌شود | حد سود ممنوع (حذف شد)، خروج فقط با حد ضرر پلکانی = خطی پویا (Dynamic-Linear)، بافر فعال (enableSmartContinuation: true)
 */

const stopLossInitial = 0.4;

const ANALYSIS_CONFIG = {
  entryType: "nextCandle",
  breakTolerance: 0.02,
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

  // سیگنال فقط بر اساس آخرین کندل کاملاً بسته‌شده بررسی می‌شود (جلوگیری از آینده‌نگری/ریپینت)
  const sigIndex = index - 1;

  if (!globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn || globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn.dataRef !== data) {
    globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn = { dataRef: data, lastSignalIndex: -999 };
  }

  // اسنپ‌شات ایچیموکوی کندل قبلی برای جلوگیری از آینده‌نگری (lookahead bias)
  const __prevIchimoku = globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn.__lastIchimokuIndex === index - 1 ? globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn.__lastIchimoku : null;
  globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn.__lastIchimoku = ichimokuParam ? { ...ichimokuParam } : null;
  globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn.__lastIchimokuIndex = index;
  const st = globalThis.__state_S4Ic14_30_57_KijPb_LadLine_BufOn;

  if (!__prevIchimoku || __prevIchimoku.kumoTop === null || __prevIchimoku.kumoTop === undefined) return null;
  if (!__prevIchimoku.tenkan || !__prevIchimoku.kijun) return null;

  // زمینه‌ی کلی نزولی طبق ایچیموکو
  if (!__prevIchimoku.isPriceBelowCloud || __prevIchimoku.isTenkanAboveKijun) return null;

  const candle = data[sigIndex];
  const kijun = __prevIchimoku.kijun;
  const PULLBACK_TOLERANCE = 0.0015;

  // پول‌بک: کندل تا نزدیکی کیجون‌سن بالا می‌رود و دوباره زیر آن بسته می‌شود
  const distPercent = Math.abs(candle.high - kijun) / kijun;
  if (distPercent > PULLBACK_TOLERANCE) return null;
  if (candle.close >= kijun) return null;
  if (candle.close >= candle.open) return null;

  if (sigIndex - st.lastSignalIndex < 5) return null;
  st.lastSignalIndex = sigIndex;

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
