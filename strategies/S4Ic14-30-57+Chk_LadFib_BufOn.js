/**
 * @filename S4Ic14-30-57+Chk_LadFib_BufOn.js
 * @description فروش با تاییدیه‌ی سه‌گانه‌ی کامل ایچیموکو (14,30,57): قیمت زیر ابر + تنکان زیر کیجون + چیکو نزولی — سیگنال فقط در همان کندلی که هر سه شرط تازه هم‌راستا شده‌اند صادر می‌شود | حد سود ممنوع (حذف شد)، خروج فقط با حد ضرر پلکانی = فیبوناچی تعدیل‌شده (Adjusted-Fibonacci)، بافر فعال (enableSmartContinuation: true)
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
    useChikou: true
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

function customStrategy(data, index, breakPointsParam, ichimokuParam, trendLinesParam, refineEntryPrice) {
  if (index < 61) return null;

  if (!globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn || globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn.dataRef !== data) {
    globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn = { dataRef: data, wasBearish: false };
  }

  // اسنپ‌شات ایچیموکوی کندل قبلی برای جلوگیری از آینده‌نگری (lookahead bias)
  const __prevIchimoku = globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn.__lastIchimokuIndex === index - 1 ? globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn.__lastIchimoku : null;
  globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn.__lastIchimoku = ichimokuParam ? { ...ichimokuParam } : null;
  globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn.__lastIchimokuIndex = index;
  const st = globalThis.__state_S4Ic14_30_57_Chk_LadFib_BufOn;

  if (!__prevIchimoku || __prevIchimoku.kumoTop === null || __prevIchimoku.kumoTop === undefined) return null;
  if (!__prevIchimoku.tenkan || !__prevIchimoku.kijun) return null;

  const isBearishNow = __prevIchimoku.isPriceBelowCloud &&
                        !__prevIchimoku.isTenkanAboveKijun &&
                        __prevIchimoku.isChikouBullish === false;

  const justAligned = isBearishNow && !st.wasBearish;
  st.wasBearish = isBearishNow;
  if (!justAligned) return null;

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
