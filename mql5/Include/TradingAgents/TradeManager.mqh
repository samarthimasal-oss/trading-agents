//+------------------------------------------------------------------+
//| TradeManager.mqh - order placement and tick-by-tick management   |
//|                                                                  |
//| Broker-side SL/TP are always attached.  The manager adds partials,|
//| breakeven, ATR trailing, time stops and strategy exits.  It never |
//| widens a stop and never averages into a loser.                   |
//+------------------------------------------------------------------+
#ifndef TA_TRADE_MANAGER_MQH
#define TA_TRADE_MANAGER_MQH

#include <Trade\Trade.mqh>
#include <TradingAgents/Types.mqh>
#include <TradingAgents/State.mqh>
#include <TradingAgents/Risk.mqh>
#include <TradingAgents/Features.mqh>
#include <TradingAgents/Protocol.mqh>

class TATradeManager
{
private:
   CTrade m_trade;

   int SessionForTime(const datetime value, const int utc_offset) const
   {
      long seconds = (long)value + (long)utc_offset;
      long day_id = seconds / 86400;
      if(seconds < 0 && (seconds % 86400) != 0)
         day_id--;
      int seconds_in_day = (int)(seconds - day_id * 86400);
      int hour = seconds_in_day / 3600;
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

   double StopDistance(const string symbol, const int extra_points) const
   {
      int stops = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      int points = stops + extra_points;
      if(points < 1)
         points = 1;
      return (double)points * point;
   }

   int StrategyFromComment(const string comment) const
   {
      if(StringFind(comment, "TA:") == 0)
         return TA_StrategyId(StringSubstr(comment, 3));
      return TA_STRAT_NONE;
   }

   ulong FindNewestPosition(const TAConfig &cfg) const
   {
      ulong best = 0;
      long best_time = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;
         long opened = PositionGetInteger(POSITION_TIME);
         if(opened >= best_time)
         {
            best_time = opened;
            best = ticket;
         }
      }
      return best;
   }

   bool ModifyStop(const TAConfig &cfg, const ulong ticket,
                   const double stop, const double target)
   {
      if(stop <= 0.0)
         return false;
      int digits = (int)SymbolInfoInteger(cfg.symbol, SYMBOL_DIGITS);
      double sl = NormalizeDouble(stop, digits);
      double tp = NormalizeDouble(target, digits);
      if(!m_trade.PositionModify(ticket, sl, tp))
         return false;
      int code = (int)m_trade.ResultRetcode();
      return (code == TRADE_RETCODE_DONE || code == TRADE_RETCODE_PLACED ||
              code == TRADE_RETCODE_DONE_PARTIAL);
   }

public:
   TATradeManager()
   {
   }

   void Init(const TAConfig &cfg)
   {
      m_trade.SetExpertMagicNumber((ulong)cfg.magic);
      m_trade.SetDeviationInPoints((ulong)MathMax(1, cfg.slippage_points));
      m_trade.SetTypeFillingBySymbol(cfg.symbol);
      m_trade.SetAsyncMode(false);
   }

   bool Execute(const TAConfig &cfg, const TASignal &signal, const string key,
                TAState &state, TARisk &risk, TAFill &fill)
   {
      fill.ok = false;
      fill.ticket = 0;
      fill.price = 0.0;
      fill.volume = 0.0;
      fill.slippage_points = 0.0;
      fill.retcode = 0;
      fill.retcode_name = "NONE";
      fill.message = "";

      ulong seen_ticket = 0;
      int seen_status = 0;
      if(state.FindKey(key, seen_ticket, seen_status))
      {
         fill.message = "duplicate idempotency key";
         fill.retcode = 10025;
         fill.retcode_name = TA_RetcodeName(10025);
         return false;
      }
      if(!signal.valid || (signal.direction != 1 && signal.direction != -1))
      {
         fill.message = "invalid signal direction";
         return false;
      }

      double ask = SymbolInfoDouble(cfg.symbol, SYMBOL_ASK);
      double bid = SymbolInfoDouble(cfg.symbol, SYMBOL_BID);
      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      if(ask <= 0.0 || bid <= 0.0 || point <= 0.0)
      {
         fill.message = "no valid quote";
         return false;
      }
      double market = (signal.direction > 0) ? ask : bid;
      double spread_points = (ask - bid) / point;
      if(cfg.max_spread_points > 0.0 && spread_points > cfg.max_spread_points)
      {
         fill.message = StringFormat("spread %.0f exceeds %.0f points",
                                     spread_points, cfg.max_spread_points);
         return false;
      }
      if(signal.entry > 0.0)
      {
         double distance = MathAbs(signal.entry - market) / point;
         if(distance > (double)(cfg.slippage_points + cfg.stop_buffer_points + 2))
         {
            fill.message = StringFormat("entry %.5f is %.0f points from market %.5f",
                                        signal.entry, distance, market);
            return false;
         }
      }

      string stop_reason = "";
      if(!risk.StopsValid(cfg, signal.direction, signal.stop, signal.target,
                          bid, ask, stop_reason))
      {
         fill.message = stop_reason;
         fill.retcode = 10016;
         fill.retcode_name = TA_RetcodeName(10016);
         return false;
      }

      double stop_distance = MathAbs(market - signal.stop);
      double volume = 0.0;
      if(signal.volume > 0.0)
         volume = risk.NormalizeVolume(cfg.symbol, signal.volume);
      else
         volume = risk.VolumeForRisk(cfg, stop_distance, signal.risk_percent);
      if(volume <= 0.0)
      {
         fill.message = "sizing produced zero volume";
         return false;
      }
      double minimum = SymbolInfoDouble(cfg.symbol, SYMBOL_VOLUME_MIN);
      double allowed_risk = AccountInfoDouble(ACCOUNT_EQUITY) *
                            (cfg.risk_percent / 100.0) * 1.50;
      if(minimum > 0.0 && volume <= minimum && allowed_risk > 0.0 &&
         risk.RiskMoneyFor(cfg, volume, stop_distance) > allowed_risk)
      {
         fill.message = "minimum lot risks more than the configured risk budget";
         return false;
      }
      if(cfg.max_total_lots > 0.0)
      {
         double used = risk.TotalVolume(cfg);
         double remaining = cfg.max_total_lots - used;
         if(remaining <= 0.0)
         {
            fill.message = "max total volume already reached";
            return false;
         }
         if(used + volume > cfg.max_total_lots)
         {
            // Reduce the order to the remaining exposure instead of
            // rejecting an otherwise valid risk-sized trade.
            double step = SymbolInfoDouble(cfg.symbol, SYMBOL_VOLUME_STEP);
            if(step <= 0.0)
               step = 0.01;
            int digits = 2;
            if(step < 0.01)
               digits = 3;
            if(step < 0.001)
               digits = 4;
            double capped = MathFloor((remaining + 1e-9) / step) * step;
            capped = NormalizeDouble(capped, digits);
            if(capped < minimum)
            {
               fill.message = "max total volume leaves no room for a minimum lot";
               return false;
            }
            volume = capped;
         }
      }
      if(risk.CountPositions(cfg) >= cfg.max_positions)
      {
         fill.message = "max open positions reached";
         return false;
      }
      string margin_reason = "";
      if(!risk.MarginAllowed(cfg, signal.direction, volume, market, margin_reason))
      {
         fill.message = margin_reason;
         return false;
      }

      fill.volume = volume;
      fill.price = market;
      if(cfg.mode == TA_MODE_PAPER)
      {
         fill.ok = true;
         fill.message = "paper mode: validated, no order sent";
         return true;
      }

      string comment = "TA:" + TA_StrategyName(signal.strategy);
      bool sent = false;
      if(signal.direction > 0)
         sent = m_trade.Buy(volume, cfg.symbol, 0.0, signal.stop, signal.target, comment);
      else
         sent = m_trade.Sell(volume, cfg.symbol, 0.0, signal.stop, signal.target, comment);

      int code = (int)m_trade.ResultRetcode();
      fill.retcode = code;
      fill.retcode_name = TA_RetcodeName(code);
      double result_price = m_trade.ResultPrice();
      double result_volume = m_trade.ResultVolume();
      if(result_price > 0.0)
         fill.price = result_price;
      if(result_volume > 0.0)
         fill.volume = result_volume;
      fill.slippage_points = MathAbs(fill.price - market) / point;
      if(!sent || (code != TRADE_RETCODE_DONE &&
                   code != TRADE_RETCODE_PLACED &&
                   code != TRADE_RETCODE_DONE_PARTIAL))
      {
         fill.ok = false;
         fill.message = StringFormat("order rejected: %s (%d)",
                                     TA_RetcodeName(code), code);
         return false;
      }

      ulong ticket = 0;
      ulong deal = m_trade.ResultDeal();
      ulong order = m_trade.ResultOrder();
      if(deal > 0 && HistoryDealSelect(deal))
      {
         long position_id = HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         if(position_id > 0)
            ticket = (ulong)position_id;
      }
      if(ticket == 0 && order > 0 && PositionSelectByTicket(order))
         ticket = order;
      if(ticket == 0)
         ticket = FindNewestPosition(cfg);

      fill.ok = true;
      fill.ticket = ticket;
      if(ticket == 0)
      {
         fill.message = "filled; position ticket pending reconciliation";
         state.RememberKey(key, 0, 0);
         return true;
      }

      TAPositionState ps;
      ps.ticket = ticket;
      ps.strategy = signal.strategy;
      ps.direction = signal.direction;
      ps.entry = fill.price;
      ps.initial_stop = signal.stop;
      ps.initial_risk = MathAbs(fill.price - signal.stop);
      if(ps.initial_risk <= 0.0)
         ps.initial_risk = stop_distance;
      ps.target = signal.target;
      ps.risk_money = risk.RiskMoneyFor(cfg, fill.volume, ps.initial_risk);
      ps.opened_at = TimeCurrent();
      ps.mfe_r = 0.0;
      ps.mae_r = 0.0;
      ps.partial_done = false;
      ps.breakeven_done = false;
      ps.adopted = false;
      ps.bars_held = 0;
      ps.pending_exit_reason = TA_EXIT_UNKNOWN;
      ps.close_requested = false;
      ps.entry_spread_points = spread_points;
      ps.initial_volume = fill.volume;
      ps.last_modify_ms = GetTickCount();
      state.AddOrUpdatePosition(ps);
      state.RememberKey(key, ticket, 0);
      PrintFormat("TA entry: %s %s %.2f @ %.5f sl=%.5f tp=%.5f ticket=%I64u",
                  TA_StrategyName(signal.strategy),
                  (signal.direction > 0 ? "BUY" : "SELL"), fill.volume,
                  fill.price, signal.stop, signal.target, ticket);
      return true;
   }

   void AdoptPosition(const TAConfig &cfg, const ulong ticket,
                      const TASnapshot &snap, TAState &state, TARisk &risk)
   {
      if(!PositionSelectByTicket(ticket))
         return;
      int direction = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double risk_distance = MathAbs(entry - sl);
      if(risk_distance <= 0.0)
      {
         risk_distance = (snap.atr > 0.0) ? snap.atr * 1.5 : entry * 0.005;
         if(risk_distance <= 0.0)
            return;
         double candidate = entry - direction * risk_distance;
         if(ModifyStop(cfg, ticket, candidate, tp))
         {
            sl = candidate;
            PrintFormat("TA adopted ticket %I64u without a stop; attached %.5f",
                        ticket, sl);
         }
      }
      TAPositionState ps;
      ps.ticket = ticket;
      ps.strategy = StrategyFromComment(PositionGetString(POSITION_COMMENT));
      ps.direction = direction;
      ps.entry = entry;
      ps.initial_stop = sl;
      ps.initial_risk = risk_distance;
      ps.target = tp;
      ps.risk_money = risk.RiskMoneyFor(cfg, volume, risk_distance);
      ps.opened_at = (datetime)PositionGetInteger(POSITION_TIME);
      ps.mfe_r = 0.0;
      ps.mae_r = 0.0;
      ps.partial_done = false;
      ps.breakeven_done = false;
      ps.adopted = true;
      ps.bars_held = 0;
      ps.pending_exit_reason = TA_EXIT_UNKNOWN;
      ps.close_requested = false;
      ps.entry_spread_points = snap.spread_points;
      ps.initial_volume = volume;
      ps.last_modify_ms = GetTickCount();
      state.AddOrUpdatePosition(ps);
      PrintFormat("TA adopted ticket %I64u strategy=%s dir=%d entry=%.5f risk=%.5f",
                  ticket, TA_StrategyName(ps.strategy), direction, entry, risk_distance);
   }

   bool FinalizeClosed(const TAConfig &cfg, const TAPositionState &ps,
                       TAState &state, const int utc_offset,
                       const bool force = false)
   {
      long from = (long)ps.opened_at - 120;
      if(from < 0)
         from = 0;
      HistorySelect((datetime)from, (datetime)((long)TimeCurrent() + 120));
      double gross = 0.0;
      double swap = 0.0;
      double commission = 0.0;
      double exit_price = 0.0;
      datetime closed_at = TimeCurrent();
      int deals = 0;
      int total = HistoryDealsTotal();
      for(int i = 0; i < total; i++)
      {
         ulong deal = HistoryDealGetTicket(i);
         if(deal == 0)
            continue;
         long position_id = HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         if(position_id != (long)ps.ticket)
            continue;
         deals++;
         gross += HistoryDealGetDouble(deal, DEAL_PROFIT);
         swap += HistoryDealGetDouble(deal, DEAL_SWAP);
         commission += HistoryDealGetDouble(deal, DEAL_COMMISSION);
         double price = HistoryDealGetDouble(deal, DEAL_PRICE);
         if(price > 0.0)
            exit_price = price;
         long when = HistoryDealGetInteger(deal, DEAL_TIME);
         if(when > 0)
            closed_at = (datetime)when;
      }
      if(deals == 0 && !force)
         return false;

      double net = gross + swap + commission;
      double risk_money = ps.risk_money;
      if(risk_money <= 0.0 && ps.initial_risk > 0.0)
      {
         double tick_value = SymbolInfoDouble(cfg.symbol, SYMBOL_TRADE_TICK_VALUE);
         double tick_size = SymbolInfoDouble(cfg.symbol, SYMBOL_TRADE_TICK_SIZE);
         if(tick_size <= 0.0)
            tick_size = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
         double volume = (ps.initial_volume > 0.0) ? ps.initial_volume : 0.01;
         if(tick_value > 0.0 && tick_size > 0.0)
            risk_money = (ps.initial_risk / tick_size) * tick_value * volume;
      }
      double realized_r = (risk_money > 0.0) ? net / risk_money : 0.0;

      int reason = ps.pending_exit_reason;
      if(reason == TA_EXIT_UNKNOWN)
      {
         double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
         double tolerance = MathMax(5.0 * point, 0.05 * ps.initial_risk);
         if(ps.initial_stop > 0.0 && exit_price > 0.0 &&
            MathAbs(exit_price - ps.initial_stop) <= tolerance)
            reason = TA_EXIT_STOP;
         else if(ps.target > 0.0 && exit_price > 0.0 &&
                 MathAbs(exit_price - ps.target) <= tolerance)
            reason = TA_EXIT_TARGET;
         else
            reason = TA_EXIT_MANUAL;
      }

      TATradeRecord record;
      record.ticket = ps.ticket;
      record.strategy = ps.strategy;
      record.direction = ps.direction;
      record.opened_at = ps.opened_at;
      record.closed_at = closed_at;
      record.entry = ps.entry;
      record.exit = exit_price;
      record.volume = (ps.initial_volume > 0.0) ? ps.initial_volume : 0.0;
      record.initial_stop = ps.initial_stop;
      record.target = ps.target;
      record.realized_r = realized_r;
      record.mfe_r = ps.mfe_r;
      record.mae_r = ps.mae_r;
      record.spread_points = ps.entry_spread_points;
      record.commission = commission;
      record.swap = swap;
      record.profit = gross;
      record.bars_held = ps.bars_held;
      record.exit_reason = reason;
      record.session = SessionForTime(ps.opened_at, utc_offset);
      MqlDateTime dt;
      TimeToStruct((datetime)((long)closed_at + (long)utc_offset), dt);
      record.month = dt.year * 100 + dt.mon;

      state.AddRecord(record);
      state.RegisterResult(realized_r);
      state.RemovePosition(ps.ticket);
      PrintFormat("TA close: ticket=%I64u %s net=%.2f R=%.2f mfe=%.2f mae=%.2f reason=%s",
                  ps.ticket, TA_StrategyName(ps.strategy), net, realized_r,
                  ps.mfe_r, ps.mae_r, TA_ExitName(reason));
      return true;
   }

   bool CloseWithReason(const TAConfig &cfg, const ulong ticket,
                        const int reason, TAState &state)
   {
      int index = -1;
      state.FindPosition(ticket, index);
      if(index >= 0)
      {
         TAPositionState ps;
         state.PositionAt(index, ps);
         ps.close_requested = true;
         ps.pending_exit_reason = reason;
         state.AddOrUpdatePosition(ps);
      }
      if(!PositionSelectByTicket(ticket))
         return false;
      bool closed = m_trade.PositionClose(ticket);
      int code = (int)m_trade.ResultRetcode();
      if(!closed || (code != TRADE_RETCODE_DONE &&
                     code != TRADE_RETCODE_PLACED &&
                     code != TRADE_RETCODE_DONE_PARTIAL))
      {
         PrintFormat("TA close failed ticket=%I64u reason=%s retcode=%s",
                     ticket, TA_ExitName(reason), TA_RetcodeName(code));
         return false;
      }
      PrintFormat("TA close request ticket=%I64u reason=%s", ticket,
                  TA_ExitName(reason));
      return true;
   }

   void CloseAll(const TAConfig &cfg, TAState &state, const int reason)
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;
         CloseWithReason(cfg, ticket, reason, state);
      }
   }

   void ManagePositions(const TAConfig &cfg, const TASnapshot &snap,
                        TAState &state, TARisk &risk, const int utc_offset)
   {
      // Finalize positions that disappeared since the last tick.
      for(int i = state.PositionCount() - 1; i >= 0; i--)
      {
         TAPositionState ps;
         if(!state.PositionAt(i, ps))
            continue;
         if(!PositionSelectByTicket(ps.ticket))
            FinalizeClosed(cfg, ps, state, utc_offset);
      }

      int period = PeriodSeconds(cfg.timeframe);
      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      double stops = StopDistance(cfg.symbol, cfg.stop_buffer_points);

      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;

         int index = -1;
         state.FindPosition(ticket, index);
         if(index < 0)
         {
            AdoptPosition(cfg, ticket, snap, state, risk);
            state.FindPosition(ticket, index);
            if(index < 0)
               continue;
         }
         TAPositionState ps;
         if(!state.PositionAt(index, ps))
            continue;

         double volume = PositionGetDouble(POSITION_VOLUME);
         if(volume <= 0.0)
            continue;
         int direction = ps.direction;
         double bid = SymbolInfoDouble(cfg.symbol, SYMBOL_BID);
         double ask = SymbolInfoDouble(cfg.symbol, SYMBOL_ASK);
         double price = (direction > 0) ? bid : ask;
         if(price <= 0.0 || ps.initial_risk <= 0.0)
            continue;

         double r_multiple = (price - ps.entry) * direction / ps.initial_risk;
         if(r_multiple > ps.mfe_r)
            ps.mfe_r = r_multiple;
         if(r_multiple < ps.mae_r)
            ps.mae_r = r_multiple;
         int bars = 0;
         if(period > 0 && (long)ps.opened_at > 0)
            bars = (int)(((long)TimeCurrent() - (long)ps.opened_at) / period);
         if(bars < 0)
            bars = 0;
         ps.bars_held = bars;

         // Partial profit, then move to breakeven.
         if(cfg.partial_at_r > 0.0 && !ps.partial_done &&
            r_multiple >= cfg.partial_at_r)
         {
            double min_volume = SymbolInfoDouble(cfg.symbol, SYMBOL_VOLUME_MIN);
            if(volume >= min_volume * 2.0)
            {
               double close_volume = risk.NormalizeVolume(
                  cfg.symbol, volume * cfg.partial_fraction);
               if(close_volume >= min_volume &&
                  volume - close_volume >= min_volume)
               {
                  if(m_trade.PositionClosePartial(ticket, close_volume))
                  {
                     ps.partial_done = true;
                     ps.last_modify_ms = GetTickCount();
                     state.AddOrUpdatePosition(ps);
                     PrintFormat("TA partial ticket=%I64u closed %.2f of %.2f at %.2fR",
                                 ticket, close_volume, volume, r_multiple);
                     if(!PositionSelectByTicket(ticket))
                        continue;
                     volume = PositionGetDouble(POSITION_VOLUME);
                  }
               }
            }
         }

         if(!ps.breakeven_done && cfg.breakeven_at_r > 0.0 &&
            r_multiple >= cfg.breakeven_at_r)
         {
            double current_sl = PositionGetDouble(POSITION_SL);
            double tp = PositionGetDouble(POSITION_TP);
            double buffer = MathMax((ask - bid), 0.05 * snap.atr);
            double candidate = ps.entry + direction * buffer;
            bool better = (direction > 0) ?
                          (candidate > current_sl + point) :
                          (current_sl <= 0.0 || candidate < current_sl - point);
            bool safe = (direction > 0) ?
                        (candidate < price - stops) :
                        (candidate > price + stops);
            if(better && safe)
            {
               if(ModifyStop(cfg, ticket, candidate, tp))
               {
                  ps.breakeven_done = true;
                  ps.last_modify_ms = GetTickCount();
                  state.AddOrUpdatePosition(ps);
                  PrintFormat("TA breakeven ticket=%I64u sl=%.5f", ticket, candidate);
               }
            }
         }

         if(ps.breakeven_done && cfg.trail_at_r > 0.0 &&
            r_multiple >= cfg.trail_at_r && snap.atr > 0.0)
         {
            uint now = GetTickCount();
            if((uint)(now - ps.last_modify_ms) >= 1000)
            {
               double current_sl = PositionGetDouble(POSITION_SL);
               double tp = PositionGetDouble(POSITION_TP);
               double candidate = price - direction * cfg.trail_atr * snap.atr;
               double step = MathMax(stops, 0.10 * snap.atr);
               bool better = (direction > 0) ?
                              (candidate > current_sl + step) :
                              (current_sl <= 0.0 || candidate < current_sl - step);
               bool safe = (direction > 0) ?
                           (candidate < price - stops) :
                           (candidate > price + stops);
               if(better && safe)
               {
                  ps.last_modify_ms = now;
                  if(ModifyStop(cfg, ticket, candidate, tp))
                     PrintFormat("TA trail ticket=%I64u sl=%.5f r=%.2f",
                                 ticket, candidate, r_multiple);
               }
            }
         }

         // Time stop.
         int max_bars = TA_StrategyMaxBars(ps.strategy);
         if(cfg.time_stop_bars > 0)
            max_bars = MathMin(max_bars, cfg.time_stop_bars);
         if(max_bars > 0 && bars >= max_bars)
         {
            CloseWithReason(cfg, ticket, TA_EXIT_TIME, state);
            continue;
         }

         // Strategy-specific structure exits.  Never exit on the entry bar:
         // the snapshot that produced the signal is the same closed bar.
         int exit_reason = TA_EXIT_UNKNOWN;
         if(bars < 1)
         {
            state.AddOrUpdatePosition(ps, false);
            continue;
         }
         if(ps.strategy == TA_STRAT_TREND)         {
            if(direction > 0)
            {
               if(snap.ema20 < snap.ema50 || snap.close < snap.ema50)
                  exit_reason = TA_EXIT_STRUCTURE;
            }
            else
            {
               if(snap.ema20 > snap.ema50 || snap.close > snap.ema50)
                  exit_reason = TA_EXIT_STRUCTURE;
            }
         }
         else if(ps.strategy == TA_STRAT_SMC)
         {
            if(direction > 0)
            {
               if(snap.choch_down || snap.bos_down ||
                  (snap.range_mid > 0.0 && snap.close < snap.range_mid && bars >= 10))
                  exit_reason = TA_EXIT_STRUCTURE;
            }
            else
            {
               if(snap.choch_up || snap.bos_up ||
                  (snap.range_mid > 0.0 && snap.close > snap.range_mid && bars >= 10))
                  exit_reason = TA_EXIT_STRUCTURE;
            }
         }
         else if(ps.strategy == TA_STRAT_MOMENTUM)
         {
            if(direction > 0)
            {
               if(snap.macd_hist < 0.0 || snap.close < snap.ema20)
                  exit_reason = TA_EXIT_MOMENTUM;
            }
            else
            {
               if(snap.macd_hist > 0.0 || snap.close > snap.ema20)
                  exit_reason = TA_EXIT_MOMENTUM;
            }
         }
         else if(ps.strategy == TA_STRAT_MEAN_REVERSION)
         {
            if(direction > 0)
            {
               if(snap.close >= snap.bb_mid || snap.rsi >= 55.0)
                  exit_reason = TA_EXIT_MEAN_REVERSION;
            }
            else
            {
               if(snap.close <= snap.bb_mid || snap.rsi <= 45.0)
                  exit_reason = TA_EXIT_MEAN_REVERSION;
            }
         }
         if(exit_reason != TA_EXIT_UNKNOWN)
         {
            CloseWithReason(cfg, ticket, exit_reason, state);
            continue;
         }

         // Persist the running MFE/MAE without writing the file every tick.
         state.AddOrUpdatePosition(ps, false);
      }
   }

   void BuildPositionUpdate(const TAConfig &cfg, TAState &state, string &payload)
   {
      string body = "{\"positions\":[";
      bool first = true;
      for(int i = 0; i < PositionsTotal(); i++)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) != cfg.symbol)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != cfg.magic)
            continue;
         int index = -1;
         state.FindPosition(ticket, index);
         TAPositionState ps;
         double mfe = 0.0;
         double mae = 0.0;
         bool partial = false;
         bool breakeven = false;
         int strategy = StrategyFromComment(PositionGetString(POSITION_COMMENT));
         if(index >= 0 && state.PositionAt(index, ps))
         {
            mfe = ps.mfe_r;
            mae = ps.mae_r;
            partial = ps.partial_done;
            breakeven = ps.breakeven_done;
            strategy = ps.strategy;
         }
         if(!first)
            body += ",";
         first = false;
         int direction = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
         body += TA_PositionObject(
            ticket, cfg.symbol, direction,
            PositionGetDouble(POSITION_VOLUME),
            PositionGetDouble(POSITION_PRICE_OPEN),
            PositionGetDouble(POSITION_SL),
            PositionGetDouble(POSITION_TP),
            PositionGetDouble(POSITION_PRICE_CURRENT),
            PositionGetDouble(POSITION_PROFIT),
            PositionGetDouble(POSITION_SWAP),
            (datetime)PositionGetInteger(POSITION_TIME),
            strategy, mfe, mae, partial, breakeven);
      }
      body += "]}";
      payload = body;
   }

   int OpenCount(const TAConfig &cfg) const
   {
      int count = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         if(PositionGetString(POSITION_SYMBOL) == cfg.symbol &&
            PositionGetInteger(POSITION_MAGIC) == cfg.magic)
            count++;
      }
      return count;
   }
};

#endif // TA_TRADE_MANAGER_MQH
