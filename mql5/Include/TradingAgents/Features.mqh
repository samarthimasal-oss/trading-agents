//+------------------------------------------------------------------+
//| Features.mqh - causal closed-bar features for the native engine  |
//|                                                                  |
//| Mirrors the Python feature set closely: EMA/RSI/ATR/ADX/MACD/BB, |
//| ROC, volume z-score, confirmed swings, BOS/CHoCH, sweeps, FVG,   |
//| order blocks, premium/discount, UTC sessions and prior-day       |
//| levels.  Every value at bar i uses bars i and older only.        |
//+------------------------------------------------------------------+
#ifndef TA_FEATURES_MQH
#define TA_FEATURES_MQH

#include <TradingAgents/Types.mqh>

#define TA_PERCENTILE_WINDOW 96
#define TA_VOLUME_WINDOW     20

struct TADayState
{
   bool     valid;
   long     day_id;
   double   open;
   double   high;
   double   low;
   double   close;
   double   prev_high;
   double   prev_low;
   double   prev_close;
   bool     has_prev;
   double   asia_high;
   double   asia_low;
   bool     asia_final;
   double   london_high;
   double   london_low;
   bool     london_final;
   int      last_session;
};

class TAFeatures
{
private:
   string            m_symbol;
   ENUM_TIMEFRAMES   m_tf;
   int               m_requested;
   int               m_len;
   int               m_utc_offset;
   datetime          m_last_processed;

   MqlRates          m_rates[];
   double            m_ema12[];
   double            m_ema20[];
   double            m_ema34[];
   double            m_ema50[];
   double            m_ema200[];
   double            m_rsi[];
   double            m_atr[];
   double            m_adx[];
   double            m_di_plus[];
   double            m_di_minus[];
   double            m_macd_main[];
   double            m_macd_signal[];
   double            m_bb_mid[];
   double            m_bb_upper[];
   double            m_bb_lower[];

   int               m_h_ema12;
   int               m_h_ema20;
   int               m_h_ema34;
   int               m_h_ema50;
   int               m_h_ema200;
   int               m_h_rsi;
   int               m_h_atr;
   int               m_h_adx;
   int               m_h_macd;
   int               m_h_bands;

   TADayState        m_day;
   double            m_last_swing_high;
   double            m_last_swing_low;
   bool              m_has_swing_high;
   bool              m_has_swing_low;
   int               m_structure_direction;

   double            m_ob_bull_top[TA_MAX_ZONES];
   double            m_ob_bull_bottom[TA_MAX_ZONES];
   int               m_ob_bull_count;
   double            m_ob_bear_top[TA_MAX_ZONES];
   double            m_ob_bear_bottom[TA_MAX_ZONES];
   int               m_ob_bear_count;
   double            m_fvg_bull_top[TA_MAX_ZONES];
   double            m_fvg_bull_bottom[TA_MAX_ZONES];
   int               m_fvg_bull_count;
   double            m_fvg_bear_top[TA_MAX_ZONES];
   double            m_fvg_bear_bottom[TA_MAX_ZONES];
   int               m_fvg_bear_count;

   TASnapshot        m_snapshot;
   TASnapshot        m_history[];
   int               m_history_count;
   bool              m_ready;

   int SessionFromHour(const int hour) const
   {
      if(hour >= 0 && hour < 7)
         return TA_SESSION_ASIA;
      if(hour >= 7 && hour < 12)
         return TA_SESSION_LONDON;
      if(hour >= 12 && hour < 16)
         return TA_SESSION_OVERLAP;
      if(hour >= 16 && hour < 21)
         return TA_SESSION_NEWYORK;
      return TA_SESSION_OFF;
   }

   void ResetState()
   {
      m_day.valid = false;
      m_day.day_id = 0;
      m_day.open = 0.0;
      m_day.high = 0.0;
      m_day.low = 0.0;
      m_day.close = 0.0;
      m_day.prev_high = 0.0;
      m_day.prev_low = 0.0;
      m_day.prev_close = 0.0;
      m_day.has_prev = false;
      m_day.asia_high = 0.0;
      m_day.asia_low = 0.0;
      m_day.asia_final = false;
      m_day.london_high = 0.0;
      m_day.london_low = 0.0;
      m_day.london_final = false;
      m_day.last_session = TA_SESSION_OFF;

      m_last_swing_high = 0.0;
      m_last_swing_low = 0.0;
      m_has_swing_high = false;
      m_has_swing_low = false;
      m_structure_direction = 0;
      m_ob_bull_count = 0;
      m_ob_bear_count = 0;
      m_fvg_bull_count = 0;
      m_fvg_bear_count = 0;
      m_last_processed = 0;
      m_ready = false;
      m_history_count = 0;
      ArrayResize(m_history, 0);
   }

   void AddZone(double &tops[], double &bottoms[], int &count,
                const double top, const double bottom)
   {
      if(top <= bottom)
         return;
      if(count >= TA_MAX_ZONES)
      {
         for(int i = 0; i < TA_MAX_ZONES - 1; i++)
         {
            tops[i] = tops[i + 1];
            bottoms[i] = bottoms[i + 1];
         }
         count = TA_MAX_ZONES - 1;
      }
      tops[count] = top;
      bottoms[count] = bottom;
      count++;
   }

   void CompactBull(double &tops[], double &bottoms[], int &count,
                    const double close, const bool strict_low, const double low)
   {
      int out = 0;
      for(int i = 0; i < count; i++)
      {
         bool keep = (close >= bottoms[i]);
         if(strict_low)
            keep = keep && (low > bottoms[i]);
         if(keep)
         {
            tops[out] = tops[i];
            bottoms[out] = bottoms[i];
            out++;
         }
      }
      count = out;
   }

   void CompactBear(double &tops[], double &bottoms[], int &count,
                    const double close, const bool strict_high, const double high)
   {
      int out = 0;
      for(int i = 0; i < count; i++)
      {
         bool keep = (close <= tops[i]);
         if(strict_high)
            keep = keep && (high < tops[i]);
         if(keep)
         {
            tops[out] = tops[i];
            bottoms[out] = bottoms[i];
            out++;
         }
      }
      count = out;
   }

   double AtrPercentile(const int shift, const double current) const
   {
      int count = 0;
      int hits = 0;
      int size = ArraySize(m_atr);
      for(int k = shift; k < shift + TA_PERCENTILE_WINDOW && k < size; k++)
      {
         double value = m_atr[k];
         if(TA_Finite(value) && value > 0.0)
         {
            count++;
            if(value <= current)
               hits++;
         }
      }
      if(count < TA_PERCENTILE_WINDOW)
         return -1.0;
      return (double)hits / (double)count;
   }

   double BbWidthPercentile(const int shift, const double current) const
   {
      int count = 0;
      int hits = 0;
      int size = MathMin(ArraySize(m_bb_mid), MathMin(ArraySize(m_bb_upper),
                                                       ArraySize(m_bb_lower)));
      for(int k = shift; k < shift + TA_PERCENTILE_WINDOW && k < size; k++)
      {
         double mid = m_bb_mid[k];
         double upper = m_bb_upper[k];
         double lower = m_bb_lower[k];
         if(!TA_Finite(mid) || !TA_Finite(upper) || !TA_Finite(lower) || mid <= 0.0)
            continue;
         double width = (upper - lower) / mid;
         if(width <= 0.0)
            continue;
         count++;
         if(width <= current)
            hits++;
      }
      if(count < TA_PERCENTILE_WINDOW)
         return -1.0;
      return (double)hits / (double)count;
   }

   double VolumeZ(const int shift) const
   {
      int size = ArraySize(m_rates);
      if(shift + TA_VOLUME_WINDOW > size)
         return 0.0;
      double sum = 0.0;
      for(int k = shift; k < shift + TA_VOLUME_WINDOW; k++)
         sum += (double)m_rates[k].tick_volume;
      double mean = sum / (double)TA_VOLUME_WINDOW;
      double variance = 0.0;
      for(int k = shift; k < shift + TA_VOLUME_WINDOW; k++)
      {
         double diff = (double)m_rates[k].tick_volume - mean;
         variance += diff * diff;
      }
      variance /= (double)TA_VOLUME_WINDOW;
      if(variance <= 0.0)
         return 0.0;
      return ((double)m_rates[shift].tick_volume - mean) / MathSqrt(variance);
   }

   double ZoneTop(const double &tops[], const int count) const
   {
      if(count <= 0)
         return 0.0;
      return tops[count - 1];
   }

   double ZoneBottom(const double &bottoms[], const int count) const
   {
      if(count <= 0)
         return 0.0;
      return bottoms[count - 1];
   }

   void ProcessBar(const int shift, TASnapshot &out)
   {
      if(shift < 1 || shift + 1 >= m_len)
         return;
      MqlRates bar = m_rates[shift];
      double atr = m_atr[shift];
      if(!TA_Finite(atr) || atr <= 0.0)
         atr = 0.0;

      long utc_seconds = (long)bar.time + (long)m_utc_offset;
      long day_id = utc_seconds / 86400;
      if(utc_seconds < 0 && (utc_seconds % 86400) != 0)
         day_id--;
      int seconds_in_day = (int)(utc_seconds - day_id * 86400);
      int hour = seconds_in_day / 3600;
      int session = SessionFromHour(hour);

      // ---------------------------------------------------------- day rollover
      if(!m_day.valid || day_id != m_day.day_id)
      {
         if(m_day.valid)
         {
            m_day.prev_high = m_day.high;
            m_day.prev_low = m_day.low;
            m_day.prev_close = m_day.close;
            m_day.has_prev = true;
         }
         m_day.valid = true;
         m_day.day_id = day_id;
         m_day.open = bar.open;
         m_day.high = bar.high;
         m_day.low = bar.low;
         m_day.close = bar.close;
         m_day.asia_high = 0.0;
         m_day.asia_low = 0.0;
         m_day.asia_final = false;
         m_day.london_high = 0.0;
         m_day.london_low = 0.0;
         m_day.london_final = false;
         m_day.last_session = TA_SESSION_OFF;
      }
      else
      {
         if(bar.high > m_day.high)
            m_day.high = bar.high;
         if(bar.low < m_day.low)
            m_day.low = bar.low;
         m_day.close = bar.close;
      }

      if(session != m_day.last_session)
      {
         if(m_day.last_session == TA_SESSION_ASIA)
            m_day.asia_final = true;
         if(m_day.last_session == TA_SESSION_LONDON)
            m_day.london_final = true;
      }
      if(session == TA_SESSION_ASIA && !m_day.asia_final)
      {
         if(m_day.asia_high == 0.0 || bar.high > m_day.asia_high)
            m_day.asia_high = bar.high;
         if(m_day.asia_low == 0.0 || bar.low < m_day.asia_low)
            m_day.asia_low = bar.low;
      }
      if(session == TA_SESSION_LONDON && !m_day.london_final)
      {
         if(m_day.london_high == 0.0 || bar.high > m_day.london_high)
            m_day.london_high = bar.high;
         if(m_day.london_low == 0.0 || bar.low < m_day.london_low)
            m_day.london_low = bar.low;
      }
      m_day.last_session = session;

      // ---------------------------------------------------------- swings (confirmed after 2 bars)
      if(shift + 4 < m_len)
      {
         bool is_high = true;
         bool is_low = true;
         for(int k = shift; k <= shift + 4; k++)
         {
            if(m_rates[shift + 2].high < m_rates[k].high)
               is_high = false;
            if(m_rates[shift + 2].low > m_rates[k].low)
               is_low = false;
         }
         if(is_high)
         {
            m_last_swing_high = m_rates[shift + 2].high;
            m_has_swing_high = true;
         }
         if(is_low)
         {
            m_last_swing_low = m_rates[shift + 2].low;
            m_has_swing_low = true;
         }
      }

      double prev_close = m_rates[shift + 1].close;
      bool bos_up = false;
      bool bos_down = false;
      bool choch_up = false;
      bool choch_down = false;
      if(m_has_swing_high && bar.close > m_last_swing_high &&
         prev_close <= m_last_swing_high)
      {
         bos_up = true;
         if(m_structure_direction < 0)
            choch_up = true;
         m_structure_direction = 1;
      }
      else if(m_has_swing_low && bar.close < m_last_swing_low &&
              prev_close >= m_last_swing_low)
      {
         bos_down = true;
         if(m_structure_direction > 0)
            choch_down = true;
         m_structure_direction = -1;
      }

      bool sweep_high = (m_has_swing_high && bar.high > m_last_swing_high &&
                         bar.close < m_last_swing_high);
      bool sweep_low = (m_has_swing_low && bar.low < m_last_swing_low &&
                        bar.close > m_last_swing_low);
      double body = MathAbs(bar.close - bar.open);
      bool displacement = (atr > 0.0 && body > atr);
      bool displacement_up = displacement && bar.close > bar.open;
      bool displacement_down = displacement && bar.close < bar.open;

      // ---------------------------------------------------------- FVG / OB zones
      CompactBull(m_fvg_bull_top, m_fvg_bull_bottom, m_fvg_bull_count,
                  bar.close, true, bar.low);
      CompactBear(m_fvg_bear_top, m_fvg_bear_bottom, m_fvg_bear_count,
                  bar.close, true, bar.high);
      if(shift + 2 < m_len)
      {
         if(bar.low > m_rates[shift + 2].high)
            AddZone(m_fvg_bull_top, m_fvg_bull_bottom, m_fvg_bull_count,
                    bar.low, m_rates[shift + 2].high);
         if(bar.high < m_rates[shift + 2].low)
            AddZone(m_fvg_bear_top, m_fvg_bear_bottom, m_fvg_bear_count,
                    m_rates[shift + 2].low, bar.high);
      }

      CompactBull(m_ob_bull_top, m_ob_bull_bottom, m_ob_bull_count,
                  bar.close, false, bar.low);
      CompactBear(m_ob_bear_top, m_ob_bear_bottom, m_ob_bear_count,
                  bar.close, false, bar.high);
      if(atr > 0.0)
      {
         MqlRates prior = m_rates[shift + 1];
         if(body > atr && bar.close > prior.high && prior.close < prior.open)
            AddZone(m_ob_bull_top, m_ob_bull_bottom, m_ob_bull_count,
                    prior.high, prior.low);
         if(body > atr && bar.close < prior.low && prior.close > prior.open)
            AddZone(m_ob_bear_top, m_ob_bear_bottom, m_ob_bear_count,
                    prior.high, prior.low);
      }

      // ---------------------------------------------------------- feature values
      double ema20 = m_ema20[shift];
      double ema50 = m_ema50[shift];
      double ema200 = m_ema200[shift];
      double rsi = m_rsi[shift];
      double adx = m_adx[shift];
      double bb_mid = m_bb_mid[shift];
      double bb_upper = m_bb_upper[shift];
      double bb_lower = m_bb_lower[shift];
      double macd_main = m_macd_main[shift];
      double macd_signal = m_macd_signal[shift];
      double macd_hist = macd_main - macd_signal;
      double macd_hist_prev = m_macd_main[shift + 1] - m_macd_signal[shift + 1];
      double bb_width = 0.0;
      double bb_position = 0.5;
      if(TA_Finite(bb_mid) && bb_mid > 0.0)
      {
         bb_width = (bb_upper - bb_lower) / bb_mid;
         double span = bb_upper - bb_lower;
         if(span > 0.0)
            bb_position = TA_Clamp((bar.close - bb_lower) / span, 0.0, 1.0);
      }
      double atr_pct = (bar.close > 0.0) ? atr / bar.close : 0.0;
      double atr_percentile = AtrPercentile(shift, atr);
      double bb_width_pct = BbWidthPercentile(shift, bb_width);

      double roc3 = 0.0;
      double roc5 = 0.0;
      double roc10 = 0.0;
      if(shift + 3 < m_len && m_rates[shift + 3].close != 0.0)
         roc3 = bar.close / m_rates[shift + 3].close - 1.0;
      if(shift + 5 < m_len && m_rates[shift + 5].close != 0.0)
         roc5 = bar.close / m_rates[shift + 5].close - 1.0;
      if(shift + 10 < m_len && m_rates[shift + 10].close != 0.0)
         roc10 = bar.close / m_rates[shift + 10].close - 1.0;

      double ema20_slope = (shift + 10 < m_len) ? ema20 - m_ema20[shift + 10] : 0.0;
      double ema50_slope = (shift + 10 < m_len) ? ema50 - m_ema50[shift + 10] : 0.0;
      double ema20_slope_atr = (atr > 0.0) ? ema20_slope / atr : 0.0;
      double range_atr = (atr > 0.0) ? (bar.high - bar.low) / atr : 0.0;
      double body_atr = (atr > 0.0) ? body / atr : 0.0;
      double vol_z = VolumeZ(shift);

      double range_high = m_has_swing_high ? m_last_swing_high : 0.0;
      double range_low = m_has_swing_low ? m_last_swing_low : 0.0;
      double range_mid = (range_high > 0.0 && range_low > 0.0) ?
                         (range_high + range_low) / 2.0 : 0.0;
      double range_position = 0.5;
      if(range_high > range_low)
         range_position = TA_Clamp((bar.close - range_low) / (range_high - range_low),
                                   0.0, 1.0);

      double pd_range = 0.0;
      double pd_mid = 0.0;
      double pd_position = 0.5;
      if(m_day.has_prev)
      {
         pd_range = m_day.prev_high - m_day.prev_low;
         pd_mid = (m_day.prev_high + m_day.prev_low) / 2.0;
         if(pd_range > 0.0)
            pd_position = TA_Clamp((bar.close - m_day.prev_low) / pd_range, 0.0, 1.0);
      }

      int spread_points = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);
      if(spread_points <= 0)
         spread_points = (int)bar.spread;

      // ---------------------------------------------------------- snapshot
      out.valid = true;
      out.time = bar.time;
      out.open = bar.open;
      out.high = bar.high;
      out.low = bar.low;
      out.close = bar.close;
      out.atr = atr;
      out.atr_pct = atr_pct;
      out.atr_percentile = atr_percentile;
      out.ema12 = m_ema12[shift];
      out.ema20 = ema20;
      out.ema34 = m_ema34[shift];
      out.ema50 = ema50;
      out.ema200 = ema200;
      out.ema20_slope = ema20_slope;
      out.ema20_slope_atr = ema20_slope_atr;
      out.ema50_slope = ema50_slope;
      out.rsi = rsi;
      out.rsi_slope = rsi - m_rsi[shift + 1];
      out.adx = adx;
      out.di_plus = m_di_plus[shift];
      out.di_minus = m_di_minus[shift];
      out.macd = macd_main;
      out.macd_signal = macd_signal;
      out.macd_hist = macd_hist;
      out.macd_hist_slope = macd_hist - macd_hist_prev;
      out.roc3 = roc3;
      out.roc5 = roc5;
      out.roc10 = roc10;
      out.bb_mid = bb_mid;
      out.bb_upper = bb_upper;
      out.bb_lower = bb_lower;
      out.bb_width = bb_width;
      out.bb_width_pct = bb_width_pct;
      out.bb_position = bb_position;
      out.range_atr = range_atr;
      out.body_atr = body_atr;
      out.vol_z = vol_z;
      out.last_swing_high = range_high;
      out.last_swing_low = range_low;
      out.range_high = range_high;
      out.range_low = range_low;
      out.range_mid = range_mid;
      out.range_position = range_position;
      out.bos_up = bos_up;
      out.bos_down = bos_down;
      out.choch_up = choch_up;
      out.choch_down = choch_down;
      out.sweep_high = sweep_high;
      out.sweep_low = sweep_low;
      out.displacement_up = displacement_up;
      out.displacement_down = displacement_down;
      out.sweep_high_level = sweep_high ? m_last_swing_high : 0.0;
      out.sweep_low_level = sweep_low ? m_last_swing_low : 0.0;
      out.ob_bull_top = ZoneTop(m_ob_bull_top, m_ob_bull_count);
      out.ob_bull_bottom = ZoneBottom(m_ob_bull_bottom, m_ob_bull_count);
      out.ob_bear_top = ZoneTop(m_ob_bear_top, m_ob_bear_count);
      out.ob_bear_bottom = ZoneBottom(m_ob_bear_bottom, m_ob_bear_count);
      out.fvg_bull_top = ZoneTop(m_fvg_bull_top, m_fvg_bull_count);
      out.fvg_bull_bottom = ZoneBottom(m_fvg_bull_bottom, m_fvg_bull_count);
      out.fvg_bear_top = ZoneTop(m_fvg_bear_top, m_fvg_bear_count);
      out.fvg_bear_bottom = ZoneBottom(m_fvg_bear_bottom, m_fvg_bear_count);
      out.pd_high = m_day.has_prev ? m_day.prev_high : 0.0;
      out.pd_low = m_day.has_prev ? m_day.prev_low : 0.0;
      out.pd_close = m_day.has_prev ? m_day.prev_close : 0.0;
      out.pd_mid = pd_mid;
      out.pd_range = pd_range;
      out.pd_position = pd_position;
      out.asia_high = m_day.asia_high;
      out.asia_low = m_day.asia_low;
      out.london_high = m_day.london_high;
      out.london_low = m_day.london_low;
      out.session = session;
      out.structure_direction = m_structure_direction;
      out.spread_points = (double)spread_points;

      if(m_history_count >= 64)
      {
         for(int i = 1; i < m_history_count; i++)
            m_history[i - 1] = m_history[i];
         m_history_count = 63;
      }
      ArrayResize(m_history, m_history_count + 1);
      m_history[m_history_count] = out;
      m_history_count++;
   }

public:
   TAFeatures()
   {
      m_symbol = "";
      m_tf = PERIOD_M15;
      m_requested = 900;
      m_len = 0;
      m_utc_offset = 0;
      m_last_processed = 0;
      m_h_ema12 = INVALID_HANDLE;
      m_h_ema20 = INVALID_HANDLE;
      m_h_ema34 = INVALID_HANDLE;
      m_h_ema50 = INVALID_HANDLE;
      m_h_ema200 = INVALID_HANDLE;
      m_h_rsi = INVALID_HANDLE;
      m_h_atr = INVALID_HANDLE;
      m_h_adx = INVALID_HANDLE;
      m_h_macd = INVALID_HANDLE;
      m_h_bands = INVALID_HANDLE;
      m_ready = false;
      ResetState();
   }

   ~TAFeatures()
   {
      Deinit();
   }

   void Deinit()
   {
      if(m_h_ema12 != INVALID_HANDLE) IndicatorRelease(m_h_ema12);
      if(m_h_ema20 != INVALID_HANDLE) IndicatorRelease(m_h_ema20);
      if(m_h_ema34 != INVALID_HANDLE) IndicatorRelease(m_h_ema34);
      if(m_h_ema50 != INVALID_HANDLE) IndicatorRelease(m_h_ema50);
      if(m_h_ema200 != INVALID_HANDLE) IndicatorRelease(m_h_ema200);
      if(m_h_rsi != INVALID_HANDLE) IndicatorRelease(m_h_rsi);
      if(m_h_atr != INVALID_HANDLE) IndicatorRelease(m_h_atr);
      if(m_h_adx != INVALID_HANDLE) IndicatorRelease(m_h_adx);
      if(m_h_macd != INVALID_HANDLE) IndicatorRelease(m_h_macd);
      if(m_h_bands != INVALID_HANDLE) IndicatorRelease(m_h_bands);
      m_h_ema12 = INVALID_HANDLE;
      m_h_ema20 = INVALID_HANDLE;
      m_h_ema34 = INVALID_HANDLE;
      m_h_ema50 = INVALID_HANDLE;
      m_h_ema200 = INVALID_HANDLE;
      m_h_rsi = INVALID_HANDLE;
      m_h_atr = INVALID_HANDLE;
      m_h_adx = INVALID_HANDLE;
      m_h_macd = INVALID_HANDLE;
      m_h_bands = INVALID_HANDLE;
   }

   bool Init(const string symbol, const ENUM_TIMEFRAMES tf, const int history_bars,
             const int utc_offset_override, string &error)
   {
      error = "";
      m_symbol = symbol;
      m_tf = tf;
      m_requested = MathMax(400, history_bars);
      if(utc_offset_override != -99)
      {
         m_utc_offset = utc_offset_override * 3600;
      }
      else
      {
         int offset = (int)((long)TimeCurrent() - (long)TimeGMT());
         offset = (int)(MathRound((double)offset / 900.0) * 900.0);
         if(offset < -12 * 3600 || offset > 14 * 3600)
            offset = 0;
         m_utc_offset = offset;
      }

      m_h_ema12 = iMA(m_symbol, m_tf, 12, 0, MODE_EMA, PRICE_CLOSE);
      m_h_ema20 = iMA(m_symbol, m_tf, 20, 0, MODE_EMA, PRICE_CLOSE);
      m_h_ema34 = iMA(m_symbol, m_tf, 34, 0, MODE_EMA, PRICE_CLOSE);
      m_h_ema50 = iMA(m_symbol, m_tf, 50, 0, MODE_EMA, PRICE_CLOSE);
      m_h_ema200 = iMA(m_symbol, m_tf, 200, 0, MODE_EMA, PRICE_CLOSE);
      m_h_rsi = iRSI(m_symbol, m_tf, 14, PRICE_CLOSE);
      m_h_atr = iATR(m_symbol, m_tf, 14);
      m_h_adx = iADX(m_symbol, m_tf, 14);
      m_h_macd = iMACD(m_symbol, m_tf, 12, 26, 9, PRICE_CLOSE);
      m_h_bands = iBands(m_symbol, m_tf, 20, 0, 2.0, PRICE_CLOSE);

      if(m_h_ema12 == INVALID_HANDLE || m_h_ema20 == INVALID_HANDLE ||
         m_h_ema34 == INVALID_HANDLE || m_h_ema50 == INVALID_HANDLE ||
         m_h_ema200 == INVALID_HANDLE || m_h_rsi == INVALID_HANDLE ||
         m_h_atr == INVALID_HANDLE || m_h_adx == INVALID_HANDLE ||
         m_h_macd == INVALID_HANDLE || m_h_bands == INVALID_HANDLE)
      {
         error = StringFormat("indicator handle creation failed: %d", GetLastError());
         Deinit();
         return false;
      }
      ResetState();
      return true;
   }

   bool Refresh()
   {
      int copied = CopyRates(m_symbol, m_tf, 0, m_requested, m_rates);
      if(copied < 250)
      {
         PrintFormat("TAFeatures: only %d bars available (need 250)", copied);
         return false;
      }
      ArraySetAsSeries(m_rates, true);

      int n = copied;
      if(CopyBuffer(m_h_ema12, 0, 0, n, m_ema12) < n) return false;
      if(CopyBuffer(m_h_ema20, 0, 0, n, m_ema20) < n) return false;
      if(CopyBuffer(m_h_ema34, 0, 0, n, m_ema34) < n) return false;
      if(CopyBuffer(m_h_ema50, 0, 0, n, m_ema50) < n) return false;
      if(CopyBuffer(m_h_ema200, 0, 0, n, m_ema200) < n) return false;
      if(CopyBuffer(m_h_rsi, 0, 0, n, m_rsi) < n) return false;
      if(CopyBuffer(m_h_atr, 0, 0, n, m_atr) < n) return false;
      if(CopyBuffer(m_h_adx, 0, 0, n, m_adx) < n) return false;
      if(CopyBuffer(m_h_adx, 1, 0, n, m_di_plus) < n) return false;
      if(CopyBuffer(m_h_adx, 2, 0, n, m_di_minus) < n) return false;
      if(CopyBuffer(m_h_macd, 0, 0, n, m_macd_main) < n) return false;
      if(CopyBuffer(m_h_macd, 1, 0, n, m_macd_signal) < n) return false;
      if(CopyBuffer(m_h_bands, 0, 0, n, m_bb_mid) < n) return false;
      if(CopyBuffer(m_h_bands, 1, 0, n, m_bb_upper) < n) return false;
      if(CopyBuffer(m_h_bands, 2, 0, n, m_bb_lower) < n) return false;

      ArraySetAsSeries(m_ema12, true);
      ArraySetAsSeries(m_ema20, true);
      ArraySetAsSeries(m_ema34, true);
      ArraySetAsSeries(m_ema50, true);
      ArraySetAsSeries(m_ema200, true);
      ArraySetAsSeries(m_rsi, true);
      ArraySetAsSeries(m_atr, true);
      ArraySetAsSeries(m_adx, true);
      ArraySetAsSeries(m_di_plus, true);
      ArraySetAsSeries(m_di_minus, true);
      ArraySetAsSeries(m_macd_main, true);
      ArraySetAsSeries(m_macd_signal, true);
      ArraySetAsSeries(m_bb_mid, true);
      ArraySetAsSeries(m_bb_upper, true);
      ArraySetAsSeries(m_bb_lower, true);

      m_len = n;
      m_len = MathMin(m_len, ArraySize(m_ema12));
      m_len = MathMin(m_len, ArraySize(m_ema20));
      m_len = MathMin(m_len, ArraySize(m_ema34));
      m_len = MathMin(m_len, ArraySize(m_ema50));
      m_len = MathMin(m_len, ArraySize(m_ema200));
      m_len = MathMin(m_len, ArraySize(m_rsi));
      m_len = MathMin(m_len, ArraySize(m_atr));
      m_len = MathMin(m_len, ArraySize(m_adx));
      m_len = MathMin(m_len, ArraySize(m_di_plus));
      m_len = MathMin(m_len, ArraySize(m_di_minus));
      m_len = MathMin(m_len, ArraySize(m_macd_main));
      m_len = MathMin(m_len, ArraySize(m_macd_signal));
      m_len = MathMin(m_len, ArraySize(m_bb_mid));
      m_len = MathMin(m_len, ArraySize(m_bb_upper));
      m_len = MathMin(m_len, ArraySize(m_bb_lower));
      return (m_len >= 250);
   }

   bool Warmup(TASnapshot &snapshot)
   {
      if(!Refresh())
         return false;
      ResetState();
      int start = m_len - 5;
      for(int shift = start; shift >= 1; shift--)
      {
         ProcessBar(shift, m_snapshot);
      }
      m_last_processed = m_rates[1].time;
      m_ready = true;
      snapshot = m_snapshot;
      PrintFormat("TAFeatures warmup: bars=%d utc_offset=%d swings=(%.2f/%.2f) session=%s",
                  m_len, m_utc_offset, m_last_swing_high, m_last_swing_low,
                  TA_SessionName(m_snapshot.session));
      return m_snapshot.valid;
   }

   bool OnNewBar(TASnapshot &snapshot)
   {
      if(!Refresh())
         return false;
      int shift = 1;
      if(m_last_processed > 0)
      {
         bool found = false;
         for(int k = 1; k < m_len; k++)
         {
            if(m_rates[k].time == m_last_processed)
            {
               shift = k - 1;
               found = true;
               break;
            }
         }
         if(!found)
            shift = 1;
      }
      if(shift < 1)
         return false;
      // Process every missed closed bar in chronological order.
      for(int k = shift; k >= 1; k--)
         ProcessBar(k, m_snapshot);
      m_last_processed = m_rates[1].time;
      m_ready = true;
      snapshot = m_snapshot;
      return m_snapshot.valid;
   }

   void Current(TASnapshot &snapshot) const
   {
      snapshot = m_snapshot;
   }

   bool HistoryAt(const int bars_back, TASnapshot &snapshot) const
   {
      int index = m_history_count - 1 - bars_back;
      if(index < 0 || index >= m_history_count)
         return false;
      snapshot = m_history[index];
      return true;
   }

   bool RecentLow(const int bars, double &value) const
   {
      value = 0.0;
      bool found = false;
      for(int i = 0; i < bars; i++)
      {
         TASnapshot item;
         if(!HistoryAt(i, item))
            continue;
         if(!found || item.low < value)
         {
            value = item.low;
            found = true;
         }
      }
      return found;
   }

   bool RecentHigh(const int bars, double &value) const
   {
      value = 0.0;
      bool found = false;
      for(int i = 0; i < bars; i++)
      {
         TASnapshot item;
         if(!HistoryAt(i, item))
            continue;
         if(!found || item.high > value)
         {
            value = item.high;
            found = true;
         }
      }
      return found;
   }

   bool SweepLowInRecent(const int bars, double &level) const
   {
      level = 0.0;
      for(int i = 0; i < bars; i++)
      {
         TASnapshot item;
         if(!HistoryAt(i, item))
            continue;
         if(item.sweep_low && item.sweep_low_level > 0.0)
         {
            level = item.sweep_low_level;
            return true;
         }
      }
      return false;
   }

   bool SweepHighInRecent(const int bars, double &level) const
   {
      level = 0.0;
      for(int i = 0; i < bars; i++)
      {
         TASnapshot item;
         if(!HistoryAt(i, item))
            continue;
         if(item.sweep_high && item.sweep_high_level > 0.0)
         {
            level = item.sweep_high_level;
            return true;
         }
      }
      return false;
   }

   bool DisplacementInRecent(const int bars, const int direction) const
   {
      for(int i = 0; i < bars; i++)
      {
         TASnapshot item;
         if(!HistoryAt(i, item))
            continue;
         if(direction > 0)
         {
            if(item.displacement_up || item.bos_up || item.choch_up)
               return true;
         }
         else
         {
            if(item.displacement_down || item.bos_down || item.choch_down)
               return true;
         }
      }
      return false;
   }

   bool Ready() const
   {
      return m_ready;
   }

   datetime LastProcessed() const
   {
      return m_last_processed;
   }

   int UtcOffsetSeconds() const
   {
      return m_utc_offset;
   }
};

#endif // TA_FEATURES_MQH
