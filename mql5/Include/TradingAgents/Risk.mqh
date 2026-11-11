//+------------------------------------------------------------------+
//| Risk.mqh - broker validation, sizing and daily guardrails        |
//+------------------------------------------------------------------+
#ifndef TA_RISK_MQH
#define TA_RISK_MQH

#include <TradingAgents/Types.mqh>
#include <TradingAgents/State.mqh>

class TARisk
{
public:
   double NormalizeVolume(const string symbol, const double volume) const
   {
      double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      double minimum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      double maximum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
      if(step <= 0.0)
         step = 0.01;
      double value = MathFloor(volume / step) * step;
      if(value < minimum)
         value = minimum;
      if(maximum > 0.0 && value > maximum)
         value = maximum;
      int digits = 2;
      if(step < 0.01)
         digits = 3;
      if(step < 0.001)
         digits = 4;
      return NormalizeDouble(value, digits);
   }

   double ValuePerPoint(const string symbol) const
   {
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
      double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
      double contract = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
      double value = 0.0;
      if(tick_size > 0.0 && tick_value > 0.0)
         value = (tick_value / tick_size) * point;
      // Some CFD feeds report a tick value that is inconsistent with the
      // contract size.  When the profit currency is the account currency,
      // contract_size * point is the authoritative value per point.
      string profit_currency = SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT);
      string account_currency = AccountInfoString(ACCOUNT_CURRENCY);
      if(profit_currency == account_currency && contract > 0.0 && point > 0.0)
      {
         double direct = contract * point;
         if(direct > value)
            value = direct;
      }
      return value;
   }

   double VolumeForRisk(const TAConfig &cfg, const double stop_distance,
                        const double risk_percent_override = 0.0) const
   {
      if(stop_distance <= 0.0)
         return 0.0;
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      double pct = (risk_percent_override > 0.0 &&
                    risk_percent_override < cfg.risk_percent) ?
                   risk_percent_override : cfg.risk_percent;
      double risk_money = equity * (pct / 100.0);
      if(risk_money <= 0.0)
         return 0.0;
      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      double per_point = ValuePerPoint(cfg.symbol);
      if(per_point <= 0.0 || point <= 0.0)
         return 0.0;
      double loss_per_lot = (stop_distance / point) * per_point;
      if(loss_per_lot <= 0.0)
         return 0.0;
      return NormalizeVolume(cfg.symbol, risk_money / loss_per_lot);
   }

   double RiskMoneyFor(const TAConfig &cfg, const double volume,
                       const double stop_distance) const
   {
      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      double per_point = ValuePerPoint(cfg.symbol);
      if(per_point <= 0.0 || point <= 0.0 || stop_distance <= 0.0)
         return 0.0;
      return (stop_distance / point) * per_point * volume;
   }

   bool StopsValid(const TAConfig &cfg, const int direction,
                   const double sl, const double tp,
                   const double bid, const double ask,
                   string &reason) const
   {
      reason = "";
      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      int stops_points = (int)SymbolInfoInteger(cfg.symbol, SYMBOL_TRADE_STOPS_LEVEL);
      int freeze_points = (int)SymbolInfoInteger(cfg.symbol, SYMBOL_TRADE_FREEZE_LEVEL);
      double stops = (double)MathMax(stops_points + cfg.stop_buffer_points, 1) * point;
      double freeze = (double)freeze_points * point;
      if(sl <= 0.0 || tp <= 0.0)
      {
         reason = "sl and tp must be positive";
         return false;
      }
      if(direction > 0)
      {
         if(sl >= ask - stops)
         {
            reason = StringFormat("buy stop too close: sl %.5f ask %.5f stops %.5f",
                                  sl, ask, stops);
            return false;
         }
         if(tp <= bid + stops)
         {
            reason = StringFormat("buy target too close: tp %.5f bid %.5f stops %.5f",
                                  tp, bid, stops);
            return false;
         }
         if(freeze > 0.0 && (ask - sl < freeze || tp - bid < freeze))
         {
            reason = "order is inside the freeze level";
            return false;
         }
      }
      else
      {
         if(sl <= bid + stops)
         {
            reason = StringFormat("sell stop too close: sl %.5f bid %.5f stops %.5f",
                                  sl, bid, stops);
            return false;
         }
         if(tp >= ask - stops)
         {
            reason = StringFormat("sell target too close: tp %.5f ask %.5f stops %.5f",
                                  tp, ask, stops);
            return false;
         }
         if(freeze > 0.0 && (sl - bid < freeze || ask - tp < freeze))
         {
            reason = "order is inside the freeze level";
            return false;
         }
      }
      return true;
   }

   bool SpreadAllowed(const TAConfig &cfg, const TASnapshot &snap,
                      string &reason) const
   {
      reason = "";
      if(cfg.max_spread_points <= 0.0)
         return true;
      if(snap.spread_points > cfg.max_spread_points)
      {
         reason = StringFormat("spread %.0f pts exceeds limit %.0f",
                               snap.spread_points, cfg.max_spread_points);
         return false;
      }
      return true;
   }

   int CountPositions(const TAConfig &cfg) const
   {
      int count = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         if(!PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;
         count++;
      }
      return count;
   }

   double TotalVolume(const TAConfig &cfg) const
   {
      double total = 0.0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         if(!PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;
         total += PositionGetDouble(POSITION_VOLUME);
      }
      return total;
   }

   bool MarginAllowed(const TAConfig &cfg, const int direction,
                      const double volume, const double price,
                      string &reason) const
   {
      reason = "";
      ENUM_ORDER_TYPE type = (direction > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double margin = 0.0;
      if(!OrderCalcMargin(type, cfg.symbol, volume, price, margin))
      {
         reason = StringFormat("OrderCalcMargin failed: %d", GetLastError());
         return false;
      }
      double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
      if(margin > free_margin * 0.80)
      {
         reason = StringFormat("margin %.2f exceeds 80%% of free margin %.2f",
                               margin, free_margin);
         return false;
      }
      return true;
   }

   bool CheckDailyLockout(const TAConfig &cfg, TAState &state,
                          string &reason) const
   {
      reason = "";
      if(state.Locked())
      {
         reason = "daily lockout: " + state.LockReason();
         return false;
      }
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      double start = state.DayStartEquity();
      if(start > 0.0 && cfg.max_daily_loss_percent > 0.0)
      {
         double loss_pct = (start - equity) / start * 100.0;
         if(loss_pct >= cfg.max_daily_loss_percent)
         {
            reason = StringFormat("daily loss %.2f%% >= %.2f%%",
                                  loss_pct, cfg.max_daily_loss_percent);
            state.Lock(reason);
            return false;
         }
      }
      if(cfg.max_daily_r > 0.0 && state.DayRealizedR() <= -cfg.max_daily_r)
      {
         reason = StringFormat("daily realized %.2fR <= -%.2fR",
                               state.DayRealizedR(), cfg.max_daily_r);
         state.Lock(reason);
         return false;
      }
      if(cfg.max_consecutive_losses > 0 &&
         state.ConsecutiveLosses() >= cfg.max_consecutive_losses)
      {
         reason = StringFormat("%d consecutive losses", state.ConsecutiveLosses());
         state.Lock(reason);
         return false;
      }
      return true;
   }

   bool NewEntriesAllowed(const TAConfig &cfg, const TASnapshot &snap,
                          TAState &state, const bool python_fresh,
                          string &reason) const
   {
      reason = "";
      if(!cfg.allow_new_trades)
      {
         reason = "new entries disabled by input";
         return false;
      }
      if(!snap.valid)
      {
         reason = "no closed-bar snapshot";
         return false;
      }
      if(state.Halted())
      {
         reason = "halted: " + state.HaltReason();
         return false;
      }
      if(!CheckDailyLockout(cfg, state, reason))
         return false;
      if(snap.session != TA_SESSION_LONDON && snap.session != TA_SESSION_OVERLAP &&
         snap.session != TA_SESSION_NEWYORK)
      {
         reason = "outside london/overlap/newyork";
         return false;
      }
      if(!SpreadAllowed(cfg, snap, reason))
         return false;
      if(CountPositions(cfg) >= cfg.max_positions)
      {
         reason = "max open positions reached";
         return false;
      }
      if(cfg.max_total_lots > 0.0 &&
         TotalVolume(cfg) >= cfg.max_total_lots)
      {
         reason = "max total volume reached";
         return false;
      }
      if(cfg.mode != TA_MODE_TESTER && !python_fresh)
      {
         reason = "python heartbeat is stale";
         return false;
      }
      return true;
   }
};

#endif // TA_RISK_MQH
