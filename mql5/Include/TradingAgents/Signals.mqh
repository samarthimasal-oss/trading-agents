//+------------------------------------------------------------------+
//| Signals.mqh - deterministic native versions of the five engines  |
//|                                                                  |
//| These rules mirror strategies/*.py closely enough for tester     |
//| parity, using MQL5 indicator buffers.  They only run on a closed  |
//| bar and only return proposals; sizing and risk checks happen in  |
//| Risk.mqh / TradeManager.mqh.                                     |
//+------------------------------------------------------------------+
#ifndef TA_SIGNALS_MQH
#define TA_SIGNALS_MQH

#include <TradingAgents/Types.mqh>
#include <TradingAgents/Features.mqh>

class TASignals
{
private:
   TAFeatures *m_features;

   double DigitsValue(const TAConfig &cfg) const
   {
      return (double)SymbolInfoInteger(cfg.symbol, SYMBOL_DIGITS);
   }

   double PointValue(const TAConfig &cfg) const
   {
      return SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
   }

   bool BaseReady(const TASnapshot &snap, const TAConfig &cfg,
                  const int strategy) const
   {
      if(!snap.valid || !TA_Finite(snap.atr) || snap.atr <= 0.0)
         return false;
      if(!TA_IsTradeSession(snap.session))
         return false;
      double point = PointValue(cfg);
      if(point <= 0.0)
         return false;
      double spread_price = snap.spread_points * point;
      double max_atr = TA_StrategyMaxSpreadAtr(strategy);
      if(max_atr > 0.0 && spread_price > max_atr * snap.atr)
         return false;
      return true;
   }

   bool MakeSignal(const TASnapshot &snap, const TAConfig &cfg,
                   const int strategy, const int direction,
                   const double entry, const double stop, const double target,
                   const double confidence, const int expires_bars,
                   const double strategy_min_rr, TASignal &out) const
   {
      if(direction != 1 && direction != -1)
         return false;
      if(!TA_Finite(entry) || !TA_Finite(stop) || !TA_Finite(target))
         return false;
      if(entry <= 0.0 || stop <= 0.0 || target <= 0.0)
         return false;
      if(direction > 0 && !(stop < entry && entry < target))
         return false;
      if(direction < 0 && !(target < entry && entry < stop))
         return false;

      double risk = MathAbs(entry - stop);
      double reward = MathAbs(target - entry);
      if(risk <= 0.0 || reward <= 0.0)
         return false;
      double rr = reward / risk;
      double required = MathMax(strategy_min_rr, cfg.min_rr);
      if(rr + 1e-9 < required)
         return false;

      int digits = (int)SymbolInfoInteger(cfg.symbol, SYMBOL_DIGITS);
      double e = NormalizeDouble(entry, digits);
      double s = NormalizeDouble(stop, digits);
      double t = NormalizeDouble(target, digits);
      if(direction > 0 && !(s < e && e < t))
         return false;
      if(direction < 0 && !(t < e && e < s))
         return false;

      out.valid = true;
      out.strategy = strategy;
      out.direction = direction;
      out.entry = e;
      out.stop = s;
      out.target = t;
      out.volume = 0.0;
      out.risk_percent = 0.0;
      out.confidence = TA_Clamp(confidence, 0.0, 0.99);
      out.rr = rr;
      out.bar_time = snap.time;
      out.expires_bars = MathMax(1, expires_bars);
      out.spread_points = snap.spread_points;
      return true;
   }

   void AddCandidate(TASignal &list[], int &count, const TASignal &signal) const
   {
      if(!signal.valid)
         return;
      if(count >= 16)
         return;
      ArrayResize(list, count + 1);
      list[count] = signal;
      count++;
   }

   double Score(const TASignal &signal) const
   {
      double rr_bonus = 1.0 + 0.05 * MathMin(4.0, MathMax(0.0, signal.rr));
      return signal.confidence * TA_StrategyWeight(signal.strategy) * rr_bonus;
   }

   //---------------------------------------------------------------- trend
   void EvalTrend(const TASnapshot &snap, const TAConfig &cfg,
                  TASignal &list[], int &count) const
   {
      if(!BaseReady(snap, cfg, TA_STRAT_TREND))
         return;
      if(!TA_Finite(snap.ema20) || !TA_Finite(snap.ema50) ||
         !TA_Finite(snap.ema200) || !TA_Finite(snap.adx) ||
         !TA_Finite(snap.roc10) || !TA_Finite(snap.ema20_slope))
         return;

      double atr = snap.atr;
      bool long_trend = (snap.ema20 > snap.ema50 && snap.ema50 > snap.ema200 &&
                         snap.ema20_slope > 0.0 && snap.roc10 > 0.0);
      bool short_trend = (snap.ema20 < snap.ema50 && snap.ema50 < snap.ema200 &&
                          snap.ema20_slope < 0.0 && snap.roc10 < 0.0);
      if(!long_trend && !short_trend)
         return;
      if(snap.adx < 20.0)
         return;

      double recent_low = 0.0;
      double recent_high = 0.0;
      if(!m_features.RecentLow(6, recent_low) || !m_features.RecentHigh(6, recent_high))
         return;

      double pullback = 0.5 * atr;
      double max_ext = 2.5;
      double atr_stop = 1.5;
      double rr_target = 2.0;
      double conf = 0.0;
      TASignal signal;

      if(long_trend)
      {
         bool touched = (recent_low <= snap.ema20 + pullback) ||
                        (recent_low <= snap.ema50 + pullback);
         double extension = (snap.close - snap.ema20) / atr;
         if(!touched || snap.close < snap.ema20 || extension > max_ext)
            return;
         double stop = MathMin(recent_low,
                               MathMin(snap.ema50 - 0.25 * atr,
                                       snap.close - atr_stop * atr));
         if(stop >= snap.close)
            return;
         double risk = snap.close - stop;
         double target = snap.close + rr_target * risk;
         conf = 0.45;
         conf += 0.20 * MathMin(1.0, MathMax(0.0, (snap.adx - 20.0) / 25.0));
         conf += 0.15 * MathMin(1.0, MathAbs(snap.roc10) / 0.01);
         conf += 0.10 * MathMin(1.0, MathAbs(snap.ema20_slope) / atr);
         conf += 0.10 * MathMax(0.0, 1.0 - extension / max_ext);
         if(MakeSignal(snap, cfg, TA_STRAT_TREND, 1, snap.close, stop, target,
                       conf, 3, 1.5, signal))
            AddCandidate(list, count, signal);
         return;
      }

      bool touched = (recent_high >= snap.ema20 - pullback) ||
                     (recent_high >= snap.ema50 - pullback);
      double extension = (snap.ema20 - snap.close) / atr;
      if(!touched || snap.close > snap.ema20 || extension > max_ext)
         return;
      double stop = MathMax(recent_high,
                            MathMax(snap.ema50 + 0.25 * atr,
                                    snap.close + atr_stop * atr));
      if(stop <= snap.close)
         return;
      double risk = stop - snap.close;
      double target = snap.close - rr_target * risk;
      conf = 0.45;
      conf += 0.20 * MathMin(1.0, MathMax(0.0, (snap.adx - 20.0) / 25.0));
      conf += 0.15 * MathMin(1.0, MathAbs(snap.roc10) / 0.01);
      conf += 0.10 * MathMin(1.0, MathAbs(snap.ema20_slope) / atr);
      conf += 0.10 * MathMax(0.0, 1.0 - extension / max_ext);
      if(MakeSignal(snap, cfg, TA_STRAT_TREND, -1, snap.close, stop, target,
                    conf, 3, 1.5, signal))
         AddCandidate(list, count, signal);
   }

   //------------------------------------------------------------- breakout
   void EvalBreakout(const TASnapshot &snap, const TAConfig &cfg,
                     TASignal &list[], int &count) const
   {
      if(!BaseReady(snap, cfg, TA_STRAT_BREAKOUT))
         return;

      double high = 0.0;
      double low = 0.0;
      bool have = false;
      if(snap.session == TA_SESSION_LONDON)
      {
         if(snap.asia_high > snap.asia_low && snap.asia_high > 0.0)
         {
            high = snap.asia_high;
            low = snap.asia_low;
            have = true;
         }
      }
      else if(snap.session == TA_SESSION_OVERLAP || snap.session == TA_SESSION_NEWYORK)
      {
         if(snap.london_high > snap.london_low && snap.london_high > 0.0)
         {
            high = snap.london_high;
            low = snap.london_low;
            have = true;
         }
         else if(snap.asia_high > snap.asia_low && snap.asia_high > 0.0)
         {
            high = snap.asia_high;
            low = snap.asia_low;
            have = true;
         }
      }
      if(!have && snap.pd_high > snap.pd_low && snap.pd_high > 0.0)
      {
         high = snap.pd_high;
         low = snap.pd_low;
         have = true;
      }
      if(!have || high <= low)
         return;

      double atr = snap.atr;
      double range_atr = (high - low) / atr;
      if(range_atr < 0.30 || range_atr > 2.50)
         return;
      if(snap.atr_percentile >= 0.0 && snap.atr_percentile > 0.70)
         return;
      if(snap.vol_z < -0.5)
         return;

      TASnapshot previous;
      if(!m_features.HistoryAt(1, previous))
         return;

      double buffer = 0.10 * atr;
      double max_break = 3.0 * atr;
      double atr_stop = 1.0;
      double rr_target = 2.0;
      double conf = 0.0;
      TASignal signal;

      if(snap.close > high + buffer && previous.close <= high + buffer)
      {
         double extension = snap.close - (high + buffer);
         if(extension > max_break)
            return;
         double stop = MathMin(high - 0.2 * atr, snap.close - atr_stop * atr);
         if(stop >= snap.close)
            return;
         double risk = snap.close - stop;
         double target = snap.close + rr_target * risk;
         conf = 0.40;
         conf += 0.20 * MathMin(1.0, MathMax(0.0, snap.vol_z) / 2.0);
         conf += 0.20 * (1.0 - MathMin(1.0, range_atr / 2.50));
         if(snap.atr_percentile >= 0.0)
            conf += 0.10 * (1.0 - MathMin(1.0, snap.atr_percentile / 0.70));
         conf += 0.10 * MathMin(1.0, MathMax(0.0, extension / max_break));
         if(MakeSignal(snap, cfg, TA_STRAT_BREAKOUT, 1, snap.close, stop, target,
                       conf, 3, 1.5, signal))
            AddCandidate(list, count, signal);
         return;
      }

      if(snap.close < low - buffer && previous.close >= low - buffer)
      {
         double extension = (low - buffer) - snap.close;
         if(extension > max_break)
            return;
         double stop = MathMax(low + 0.2 * atr, snap.close + atr_stop * atr);
         if(stop <= snap.close)
            return;
         double risk = stop - snap.close;
         double target = snap.close - rr_target * risk;
         conf = 0.40;
         conf += 0.20 * MathMin(1.0, MathMax(0.0, snap.vol_z) / 2.0);
         conf += 0.20 * (1.0 - MathMin(1.0, range_atr / 2.50));
         if(snap.atr_percentile >= 0.0)
            conf += 0.10 * (1.0 - MathMin(1.0, snap.atr_percentile / 0.70));
         conf += 0.10 * MathMin(1.0, MathMax(0.0, extension / max_break));
         if(MakeSignal(snap, cfg, TA_STRAT_BREAKOUT, -1, snap.close, stop, target,
                       conf, 3, 1.5, signal))
            AddCandidate(list, count, signal);
      }
   }

   //------------------------------------------------------------------ smc
   void EvalSmc(const TASnapshot &snap, const TAConfig &cfg,
                TASignal &list[], int &count) const
   {
      if(!BaseReady(snap, cfg, TA_STRAT_SMC))
         return;
      if(snap.range_high <= 0.0 || snap.range_low <= 0.0 ||
         snap.range_high <= snap.range_low)
         return;

      double atr = snap.atr;
      double range_position = snap.range_position;
      if(!TA_Finite(range_position))
         range_position = 0.5;
      double buffer = 0.35 * atr;
      double rr_target = 2.0;
      double max_pos = 0.55;
      TASignal signal;

      double sweep_level = 0.0;
      bool swept_low = m_features.SweepLowInRecent(10, sweep_level);
      if(swept_low && sweep_level <= 0.0)
      {
         if(!m_features.RecentLow(10, sweep_level))
            swept_low = false;
      }
      if(swept_low)
      {
         double zone_bottom = 0.0;
         double zone_top = 0.0;
         bool has_zone = false;
         if(snap.ob_bull_top > snap.ob_bull_bottom && snap.ob_bull_bottom > 0.0)
         {
            zone_top = snap.ob_bull_top;
            zone_bottom = snap.ob_bull_bottom;
            has_zone = true;
         }
         else if(snap.fvg_bull_top > snap.fvg_bull_bottom && snap.fvg_bull_bottom > 0.0)
         {
            zone_top = snap.fvg_bull_top;
            zone_bottom = snap.fvg_bull_bottom;
            has_zone = true;
         }

         if(m_features.DisplacementInRecent(4, 1) && range_position <= max_pos)
         {
            double stop_base = has_zone ? MathMin(sweep_level, zone_bottom) : sweep_level;
            double stop = stop_base - buffer;
            if(stop < snap.close)
            {
               double risk = snap.close - stop;
               double target = snap.close + rr_target * risk;
               double conf = 0.44 + 0.14 * (1.0 - range_position);
               if(has_zone)
               {
                  double tolerance = 0.60 * atr;
                  if(snap.close >= zone_bottom - tolerance &&
                     snap.close <= zone_top + tolerance)
                     conf += 0.12;
               }
               conf += 0.10 * MathMin(1.0, MathAbs(snap.body_atr) / 1.5);
               if(MakeSignal(snap, cfg, TA_STRAT_SMC, 1, snap.close, stop, target,
                             conf, 3, 1.5, signal))
                  AddCandidate(list, count, signal);
               return;
            }
         }
      }

      double sweep_high = 0.0;
      bool swept_high = m_features.SweepHighInRecent(10, sweep_high);
      if(swept_high && sweep_high <= 0.0)
      {
         if(!m_features.RecentHigh(10, sweep_high))
            swept_high = false;
      }
      if(!swept_high)
         return;
      if(!m_features.DisplacementInRecent(4, -1) ||
         range_position < 1.0 - max_pos)
         return;

      double zone_top = 0.0;
      double zone_bottom = 0.0;
      bool has_zone = false;
      if(snap.ob_bear_top > snap.ob_bear_bottom && snap.ob_bear_top > 0.0)
      {
         zone_top = snap.ob_bear_top;
         zone_bottom = snap.ob_bear_bottom;
         has_zone = true;
      }
      else if(snap.fvg_bear_top > snap.fvg_bear_bottom && snap.fvg_bear_top > 0.0)
      {
         zone_top = snap.fvg_bear_top;
         zone_bottom = snap.fvg_bear_bottom;
         has_zone = true;
      }

      double stop_base = has_zone ? MathMax(sweep_high, zone_top) : sweep_high;
      double stop = stop_base + buffer;
      if(stop <= snap.close)
         return;
      double risk = stop - snap.close;
      double target = snap.close - rr_target * risk;
      double conf = 0.44 + 0.14 * range_position;
      if(has_zone)
      {
         double tolerance = 0.60 * atr;
         if(snap.close >= zone_bottom - tolerance &&
            snap.close <= zone_top + tolerance)
            conf += 0.12;
      }
      conf += 0.10 * MathMin(1.0, MathAbs(snap.body_atr) / 1.5);
      if(MakeSignal(snap, cfg, TA_STRAT_SMC, -1, snap.close, stop, target,
                    conf, 3, 1.5, signal))
         AddCandidate(list, count, signal);
   }

   //------------------------------------------------------------- momentum
   void EvalMomentum(const TASnapshot &snap, const TAConfig &cfg,
                     TASignal &list[], int &count) const
   {
      if(!BaseReady(snap, cfg, TA_STRAT_MOMENTUM))
         return;
      if(!TA_Finite(snap.ema20) || !TA_Finite(snap.ema50) ||
         !TA_Finite(snap.rsi) || !TA_Finite(snap.adx) ||
         !TA_Finite(snap.roc5) || !TA_Finite(snap.macd_hist) ||
         !TA_Finite(snap.macd_hist_slope))
         return;

      double atr = snap.atr;
      double extension = (snap.close - snap.ema20) / atr;
      if(extension > 2.2)
         return;
      double conf = 0.0;
      TASignal signal;

      bool long_ok = (snap.close > snap.ema20 && snap.ema20 > snap.ema50 &&
                      snap.macd_hist > 0.0 && snap.macd_hist_slope > 0.0 &&
                      snap.rsi >= 52.0 && snap.rsi <= 72.0 &&
                      snap.roc5 >= 0.0005 && snap.adx >= 18.0 &&
                      snap.vol_z >= -0.2 && snap.body_atr >= 0.30 &&
                      snap.close > snap.open);
      if(long_ok)
      {
         double recent_low = 0.0;
         if(!m_features.RecentLow(5, recent_low))
            return;
         double stop = MathMin(recent_low,
                               MathMin(snap.ema20 - 0.35 * atr,
                                       snap.close - 1.20 * atr));
         if(stop >= snap.close)
            return;
         double risk = snap.close - stop;
         double target = snap.close + 2.0 * risk;
         conf = 0.42;
         conf += 0.16 * MathMin(1.0, MathMax(0.0, (snap.adx - 18.0) / 25.0));
         conf += 0.14 * MathMin(1.0, MathAbs(snap.roc5) / 0.0005);
         conf += 0.10 * MathMin(1.0, MathMax(0.0, snap.vol_z) / 2.0);
         conf += 0.12 * (1.0 - MathMin(1.0, extension / 2.2));
         conf += 0.06 * MathMax(0.0, MathMin(1.0, (snap.rsi - 52.0) / 10.0));
         if(MakeSignal(snap, cfg, TA_STRAT_MOMENTUM, 1, snap.close, stop, target,
                       conf, 2, 1.5, signal))
            AddCandidate(list, count, signal);
         return;
      }

      extension = (snap.ema20 - snap.close) / atr;
      if(extension > 2.2)
         return;
      bool short_ok = (snap.close < snap.ema20 && snap.ema20 < snap.ema50 &&
                       snap.macd_hist < 0.0 && snap.macd_hist_slope < 0.0 &&
                       snap.rsi >= 28.0 && snap.rsi <= 48.0 &&
                       snap.roc5 <= -0.0005 && snap.adx >= 18.0 &&
                       snap.vol_z >= -0.2 && snap.body_atr >= 0.30 &&
                       snap.close < snap.open);
      if(!short_ok)
         return;
      double recent_high = 0.0;
      if(!m_features.RecentHigh(5, recent_high))
         return;
      double stop = MathMax(recent_high,
                            MathMax(snap.ema20 + 0.35 * atr,
                                    snap.close + 1.20 * atr));
      if(stop <= snap.close)
         return;
      double risk = stop - snap.close;
      double target = snap.close - 2.0 * risk;
      conf = 0.42;
      conf += 0.16 * MathMin(1.0, MathMax(0.0, (snap.adx - 18.0) / 25.0));
      conf += 0.14 * MathMin(1.0, MathAbs(snap.roc5) / 0.0005);
      conf += 0.10 * MathMin(1.0, MathMax(0.0, snap.vol_z) / 2.0);
      conf += 0.12 * (1.0 - MathMin(1.0, extension / 2.2));
      conf += 0.06 * MathMax(0.0, MathMin(1.0, (72.0 - snap.rsi) / 10.0));
      if(MakeSignal(snap, cfg, TA_STRAT_MOMENTUM, -1, snap.close, stop, target,
                    conf, 2, 1.5, signal))
         AddCandidate(list, count, signal);
   }

   //-------------------------------------------------------- mean reversion
   void EvalMeanReversion(const TASnapshot &snap, const TAConfig &cfg,
                          TASignal &list[], int &count) const
   {
      if(!BaseReady(snap, cfg, TA_STRAT_MEAN_REVERSION))
         return;
      if(!TA_Finite(snap.adx) || !TA_Finite(snap.rsi) ||
         !TA_Finite(snap.rsi_slope) || !TA_Finite(snap.bb_mid) ||
         !TA_Finite(snap.bb_position) || !TA_Finite(snap.bb_width_pct))
         return;
      if(snap.adx > 28.0)
         return;
      if(snap.bb_width_pct < 0.0 || snap.bb_width_pct > 0.90)
         return;
      if(snap.bb_mid <= 0.0)
         return;
      if(TA_Finite(snap.ema200) && snap.ema200 > 0.0)
      {
         double trend_distance = MathAbs(snap.close - snap.ema200) / snap.atr;
         if(trend_distance > 5.0)
            return;
      }

      TASnapshot previous;
      if(!m_features.HistoryAt(1, previous))
         return;
      double atr = snap.atr;
      double rr_target = 1.60;
      double atr_stop = 1.10;
      double conf = 0.0;
      TASignal signal;

      if(snap.bb_position <= 0.20 && snap.rsi <= 40.0 &&
         (snap.close > snap.open ||
          (snap.rsi_slope > 0.0 && snap.close >= previous.close)))
      {
         double recent_low = 0.0;
         if(!m_features.RecentLow(4, recent_low))
            return;
         double stop = MathMin(recent_low, snap.close - atr_stop * atr);
         if(stop >= snap.close)
            return;
         double risk = snap.close - stop;
         double target = snap.bb_mid;
         double rr = (target - snap.close) / risk;
         if(rr < 0.80)
            return;
         conf = 0.40;
         conf += 0.15 * (1.0 - snap.bb_position);
         conf += 0.15 * MathMax(0.0, MathMin(1.0, (40.0 - snap.rsi) / 12.0 + 0.5));
         conf += 0.10 * MathMax(0.0, 1.0 - snap.adx / 28.0);
         conf += 0.10 * MathMax(0.0, 1.0 - snap.bb_width_pct / 0.90);
         if(MakeSignal(snap, cfg, TA_STRAT_MEAN_REVERSION, 1, snap.close, stop,
                       target, conf, 2, 0.80, signal))
            AddCandidate(list, count, signal);
         return;
      }

      if(snap.bb_position >= 0.80 && snap.rsi >= 60.0 &&
         (snap.close < snap.open ||
          (snap.rsi_slope < 0.0 && snap.close <= previous.close)))
      {
         double recent_high = 0.0;
         if(!m_features.RecentHigh(4, recent_high))
            return;
         double stop = MathMax(recent_high, snap.close + atr_stop * atr);
         if(stop <= snap.close)
            return;
         double risk = stop - snap.close;
         double target = snap.bb_mid;
         double rr = (snap.close - target) / risk;
         if(rr < 0.80)
            return;
         conf = 0.40;
         conf += 0.15 * snap.bb_position;
         conf += 0.15 * MathMax(0.0, MathMin(1.0, (snap.rsi - 60.0) / 12.0 + 0.5));
         conf += 0.10 * MathMax(0.0, 1.0 - snap.adx / 28.0);
         conf += 0.10 * MathMax(0.0, 1.0 - snap.bb_width_pct / 0.90);
         if(MakeSignal(snap, cfg, TA_STRAT_MEAN_REVERSION, -1, snap.close, stop,
                       target, conf, 2, 0.80, signal))
            AddCandidate(list, count, signal);
      }
   }

public:
   TASignals()
   {
      m_features = NULL;
   }

   void Init(TAFeatures &features)
   {
      m_features = &features;
   }

   bool Evaluate(const TASnapshot &snap, const TAConfig &cfg, TASignal &best)
   {
      best.valid = false;
      best.strategy = TA_STRAT_NONE;
      best.direction = 0;
      if(m_features == NULL || !snap.valid)
         return false;

      TASignal candidates[];
      int count = 0;
      EvalTrend(snap, cfg, candidates, count);
      EvalBreakout(snap, cfg, candidates, count);
      EvalSmc(snap, cfg, candidates, count);
      EvalMomentum(snap, cfg, candidates, count);
      EvalMeanReversion(snap, cfg, candidates, count);
      if(count <= 0)
         return false;

      // Rank by weighted confidence with an RR bonus, exactly like the arbiter.
      for(int i = 0; i < count - 1; i++)
      {
         for(int j = i + 1; j < count; j++)
         {
            double si = Score(candidates[i]);
            double sj = Score(candidates[j]);
            if(sj > si + 1e-12 ||
               (MathAbs(sj - si) <= 1e-12 && candidates[j].strategy < candidates[i].strategy))
            {
               TASignal tmp = candidates[i];
               candidates[i] = candidates[j];
               candidates[j] = tmp;
            }
         }
      }

      int chosen_direction = 0;
      for(int i = 0; i < count; i++)
      {
         if(!TA_Finite(candidates[i].confidence) ||
            candidates[i].confidence < cfg.min_confidence)
            continue;
         if(candidates[i].direction != 1 && candidates[i].direction != -1)
            continue;
         if(chosen_direction == 0)
         {
            chosen_direction = candidates[i].direction;
            best = candidates[i];
            best.valid = true;
            if(cfg.allow_opposite)
               return true;
         }
         else if(candidates[i].direction == chosen_direction)
         {
            // The first same-direction candidate is already the top-ranked one.
            return true;
         }
      }
      return best.valid;
   }
};

#endif // TA_SIGNALS_MQH
