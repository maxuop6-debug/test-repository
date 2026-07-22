/**
 * @filename B4Ic14-30-57+KijPb_LadFib_BufOff.js
 * @description خرید پول‌بک در روند صعودی ایچیموکو (14,30,57): وقتی روند کلی صعودی است (قیمت بالای ابر، تنکان بالای کیجون) ولی کندل تا نزدیکی خط کیجون‌سن پایین می‌آید و دوباره بالای آن بسته می‌شود | حد سود ممنوع (حذف شد)، خروج فقط با حد ضرر پلکانی = فیبوناچی تعدیل‌شده (Adjusted-Fibonacci)، بافر غیرفعال (enableSmartContinuation: false)
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
  enableSmartContinuation: false
};

const stopLossStages = [
  { movePercent: 0.4, stopLossPercent: 0.2 },
  { movePercent: 2.4, stopLossPercent: 1.9 },
  { movePercent: 4.2, stopLossPercent: 3.5 },
  { movePercent: 6.1, stopLossPercent: 5.0 },
  { movePercent: 8.0, stopLossPercent: 6.6 },
  { movePercent: 9.9, stopLossPercent: 8.2 },
  { movePercent: 11.8, stopLossPercent: 9.9 },
  { movePercent: 13.7, stopLossPercent: 11.6 },
  { movePercent: 15.6, stopLossPercent: 13.4 },
  { movePercent: 17.6, stopLossPercent: 15.3 },
  { movePercent: 19.6, stopLossPercent: 17.2 },
  { movePercent: 21.6, stopLossPercent: 19.1 },
  { movePercent: 23.7, stopLossPercent: 21.1 },
  { movePercent: 25.8, stopLossPercent: 23.1 },
  { movePercent: 27.9, stopLossPercent: 25.1 },
  { movePercent: 30.0, stopLossPercent: 27.2 },
  { movePercent: 32.2, stopLossPercent: 29.3 },
  { movePercent: 34.4, stopLossPercent: 31.4 },
  { movePercent: 36.6, stopLossPercent: 33.5 },
  { movePercent: 38.8, stopLossPercent: 35.6 },
  { movePercent: 41.0, stopLossPercent: 37.7 },
  { movePercent: 43.3, stopLossPercent: 39.8 },
  { movePercent: 45.6, stopLossPercent: 41.9 },
  { movePercent: 47.9, stopLossPercent: 44.1 },
  { movePercent: 50.2, stopLossPercent: 46.2 },
];

function customStrategy(data, index, breakPointsParam, ichimokuParam, trendLinesParam, refineEntryPrice) {
  if (index < 61) return null;

  // سیگنال فقط بر اساس آخرین کندل کاملاً بسته‌شده بررسی می‌شود (جلوگیری از آینده‌نگری/ریپینت)
  const sigIndex = index - 1;

  if (!globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff || globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff.dataRef !== data) {
    globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff = { dataRef: data, lastSignalIndex: -999 };
  }

  // اسنپ‌شات ایچیموکوی کندل قبلی برای جلوگیری از آینده‌نگری (lookahead bias)
  const __prevIchimoku = globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff.__lastIchimokuIndex === index - 1 ? globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff.__lastIchimoku : null;
  globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff.__lastIchimoku = ichimokuParam ? { ...ichimokuParam } : null;
  globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff.__lastIchimokuIndex = index;
  const st = globalThis.__state_B4Ic14_30_57_KijPb_LadFib_BufOff;

  if (!__prevIchimoku || __prevIchimoku.kumoTop === null || __prevIchimoku.kumoTop === undefined) return null;
  if (!__prevIchimoku.tenkan || !__prevIchimoku.kijun) return null;

  // زمینه‌ی کلی صعودی طبق ایچیموکو
  if (!__prevIchimoku.isPriceAboveCloud || !__prevIchimoku.isTenkanAboveKijun) return null;

  const candle = data[sigIndex];
  const kijun = __prevIchimoku.kijun;
  const PULLBACK_TOLERANCE = 0.0015;

  // پول‌بک: کندل تا نزدیکی کیجون‌سن پایین می‌آید و دوباره بالای آن بسته می‌شود
  const distPercent = Math.abs(candle.low - kijun) / kijun;
  if (distPercent > PULLBACK_TOLERANCE) return null;
  if (candle.close <= kijun) return null;
  if (candle.close <= candle.open) return null;

  if (sigIndex - st.lastSignalIndex < 5) return null;
  st.lastSignalIndex = sigIndex;

  const entryPrice = data[index].open;
  const stopLoss = entryPrice * (1 - 0.004);

  return {
    signal: 'BUY',
    price: entryPrice,
    stopLoss: stopLoss,
    useStagedStopLoss: true,
    stopLossStages: stopLossStages
  };
}
