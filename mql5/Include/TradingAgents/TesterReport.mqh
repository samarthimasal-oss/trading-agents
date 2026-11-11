//+------------------------------------------------------------------+
//| TesterReport.mqh - machine-readable Strategy Tester report       |
//|                                                                  |
//| Writes a per-trade CSV plus a summary CSV/JSON in the MQL5       |
//| common folder.  All statistics are computed from completed       |
//| records only; no value is estimated from a future bar.           |
//+------------------------------------------------------------------+
#ifndef TA_TESTER_REPORT_MQH
#define TA_TESTER_REPORT_MQH

#include <TradingAgents/Types.mqh>
#include <TradingAgents/State.mqh>

struct TAReportBucket
{
   int      key;
   int      trades;
   int      wins;
   int      losses;
   double   gross_profit_r;
   double   gross_loss_r;
   double   net_r;
   double   net_money;
   double   best_r;
   double   worst_r;
};

class TATesterReport
{
private:
   TAReportBucket m_strategy[];
   TAReportBucket m_session[];
   TAReportBucket m_month[];
   double         m_initial_balance;

   void ResetBuckets()
   {
      ArrayResize(m_strategy, 0);
      ArrayResize(m_session, 0);
      ArrayResize(m_month, 0);
   }

   int FindBucket(TAReportBucket &buckets[], const int key)
   {
      for(int i = 0; i < ArraySize(buckets); i++)
      {
         if(buckets[i].key == key)
            return i;
      }
      int index = ArraySize(buckets);
      ArrayResize(buckets, index + 1);
      buckets[index].key = key;
      buckets[index].trades = 0;
      buckets[index].wins = 0;
      buckets[index].losses = 0;
      buckets[index].gross_profit_r = 0.0;
      buckets[index].gross_loss_r = 0.0;
      buckets[index].net_r = 0.0;
      buckets[index].net_money = 0.0;
      buckets[index].best_r = 0.0;
      buckets[index].worst_r = 0.0;
      return index;
   }

   void AddToBuckets(TAReportBucket &buckets[], const int key,
                     const TATradeRecord &record)
   {
      int index = FindBucket(buckets, key);
      buckets[index].trades++;
      buckets[index].net_r += record.realized_r;
      buckets[index].net_money += record.profit + record.swap + record.commission;
      if(record.realized_r > 0.0)
      {
         buckets[index].wins++;
         buckets[index].gross_profit_r += record.realized_r;
      }
      else if(record.realized_r < 0.0)
      {
         buckets[index].losses++;
         buckets[index].gross_loss_r += -record.realized_r;
      }
      if(record.realized_r > buckets[index].best_r)
         buckets[index].best_r = record.realized_r;
      if(record.realized_r < buckets[index].worst_r)
         buckets[index].worst_r = record.realized_r;
   }

   string BucketName(const int kind, const int key) const
   {
      if(kind == 0)
         return TA_StrategyName(key);
      if(kind == 1)
         return TA_SessionName(key);
      return StringFormat("%d", key);
   }

   string BucketKindName(const int kind) const
   {
      if(kind == 0)
         return "strategy";
      if(kind == 1)
         return "session";
      return "month";
   }

   double SafeDiv(const double numerator, const double denominator) const
   {
      if(denominator == 0.0 || !TA_Finite(denominator))
         return 0.0;
      double value = numerator / denominator;
      if(!TA_Finite(value))
         return 0.0;
      return value;
   }

   string Num(const double value, const int digits = 4) const
   {
      if(!TA_Finite(value))
         return "0";
      return DoubleToString(value, digits);
   }

   void WriteBucketCSV(const int handle, const int kind,
                       TAReportBucket &buckets[])
   {
      for(int i = 0; i < ArraySize(buckets); i++)
      {
         double pf = (buckets[i].gross_loss_r > 0.0) ?
                     buckets[i].gross_profit_r / buckets[i].gross_loss_r :
                     ((buckets[i].gross_profit_r > 0.0) ? 999.0 : 0.0);
         double win_rate = SafeDiv((double)buckets[i].wins,
                                  (double)buckets[i].trades) * 100.0;
         double avg_r = SafeDiv(buckets[i].net_r, (double)buckets[i].trades);
         FileWriteString(handle, StringFormat(
            "%s,%s,%d,%d,%d,%.2f,%.4f,%.4f,%.4f,%.4f,%.4f,%.2f\r\n",
            BucketKindName(kind), BucketName(kind, buckets[i].key),
            buckets[i].trades, buckets[i].wins, buckets[i].losses, win_rate,
            buckets[i].net_r, buckets[i].gross_profit_r,
            buckets[i].gross_loss_r, pf, avg_r, buckets[i].net_money));
      }
   }

   void WriteBucketJSON(const int handle, const int kind,
                        TAReportBucket &buckets[], bool &first)
   {
      for(int i = 0; i < ArraySize(buckets); i++)
      {
         double pf = (buckets[i].gross_loss_r > 0.0) ?
                     buckets[i].gross_profit_r / buckets[i].gross_loss_r :
                     ((buckets[i].gross_profit_r > 0.0) ? 999.0 : 0.0);
         double win_rate = SafeDiv((double)buckets[i].wins,
                                   (double)buckets[i].trades) * 100.0;
         double avg_r = SafeDiv(buckets[i].net_r, (double)buckets[i].trades);
         if(!first)
            FileWriteString(handle, ",");
         first = false;
         FileWriteString(handle, StringFormat(
            "{\"kind\":\"%s\",\"key\":\"%s\",\"trades\":%d,\"wins\":%d,"
            "\"losses\":%d,\"win_rate\":%.2f,\"net_r\":%.4f,"
            "\"gross_profit_r\":%.4f,\"gross_loss_r\":%.4f,"
            "\"profit_factor\":%.4f,\"avg_r\":%.4f,\"net_money\":%.2f}",
            BucketKindName(kind), BucketName(kind, buckets[i].key),
            buckets[i].trades, buckets[i].wins, buckets[i].losses, win_rate,
            buckets[i].net_r, buckets[i].gross_profit_r,
            buckets[i].gross_loss_r, pf, avg_r, buckets[i].net_money));
      }
   }

public:
   TATesterReport()
   {
      m_initial_balance = 0.0;
      ResetBuckets();
   }

   void SetInitialBalance(const double value)
   {
      if(value > 0.0)
         m_initial_balance = value;
   }

   double Score(TAState &state) const
   {
      double net_r = 0.0;
      for(int i = 0; i < state.RecordCount(); i++)
      {
         TATradeRecord record;
         if(state.RecordAt(i, record))
            net_r += record.realized_r;
      }
      return net_r;
   }

   bool Write(const TAConfig &cfg, TAState &state)
   {
      ResetBuckets();
      FolderCreate(cfg.report_dir, FILE_COMMON);

      int total = state.RecordCount();
      double gross_profit_r = 0.0;
      double gross_loss_r = 0.0;
      double gross_profit_money = 0.0;
      double gross_loss_money = 0.0;
      double net_r = 0.0;
      double net_money = 0.0;
      double commission = 0.0;
      double swap = 0.0;
      double spread_cost = 0.0;
      double mfe_total = 0.0;
      double mae_total = 0.0;
      double bars_total = 0.0;
      double spread_points_total = 0.0;
      int wins = 0;
      int losses = 0;
      int breakeven = 0;
      int longest_win_streak = 0;
      int longest_loss_streak = 0;
      int current_win_streak = 0;
      int current_loss_streak = 0;
      double best_r = 0.0;
      double worst_r = 0.0;
      double cumulative_r = 0.0;
      double peak_r = 0.0;
      double max_dd_r = 0.0;
      double cumulative_money = 0.0;
      double peak_money = 0.0;
      double max_dd_money = 0.0;
      double sq_sum = 0.0;
      datetime first_open = 0;
      datetime last_close = 0;

      double point = SymbolInfoDouble(cfg.symbol, SYMBOL_POINT);
      double tick_value = SymbolInfoDouble(cfg.symbol, SYMBOL_TRADE_TICK_VALUE);
      double tick_size = SymbolInfoDouble(cfg.symbol, SYMBOL_TRADE_TICK_SIZE);
      double contract = SymbolInfoDouble(cfg.symbol, SYMBOL_TRADE_CONTRACT_SIZE);
      if(tick_size <= 0.0)
         tick_size = point;
      double value_per_point = 0.0;
      if(tick_size > 0.0 && tick_value > 0.0)
         value_per_point = tick_value / tick_size * point;
      // Match the sizing logic: for a profit currency equal to the account
      // currency the contract size is authoritative on feeds that misreport
      // the tick value (observed on this broker's XAUUSD).
      if(SymbolInfoString(cfg.symbol, SYMBOL_CURRENCY_PROFIT) ==
         AccountInfoString(ACCOUNT_CURRENCY) && contract > 0.0 && point > 0.0)
      {
         double direct = contract * point;
         if(direct > value_per_point)
            value_per_point = direct;
      }

      int trades_handle = FileOpen(cfg.report_dir + "\\tester_trades.csv",
                                   FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(trades_handle != INVALID_HANDLE)
      {
         FileWriteString(trades_handle,
            "ticket,strategy,direction,opened_at,closed_at,entry,exit,volume,"
            "initial_stop,target,realized_r,mfe_r,mae_r,spread_points,"
            "commission,swap,profit,net,bars_held,exit_reason,session,month\r\n");
      }

      for(int i = 0; i < total; i++)
      {
         TATradeRecord record;
         if(!state.RecordAt(i, record))
            continue;

         double net = record.profit + record.swap + record.commission;
         net_r += record.realized_r;
         net_money += net;
         commission += record.commission;
         swap += record.swap;
         spread_points_total += record.spread_points;
         mfe_total += record.mfe_r;
         mae_total += record.mae_r;
         bars_total += (double)record.bars_held;
         if(value_per_point > 0.0 && record.volume > 0.0)
            spread_cost += record.spread_points * value_per_point * record.volume;
         if(record.realized_r > 0.0)
         {
            wins++;
            gross_profit_r += record.realized_r;
            gross_profit_money += net;
            current_win_streak++;
            current_loss_streak = 0;
            if(current_win_streak > longest_win_streak)
               longest_win_streak = current_win_streak;
         }
         else if(record.realized_r < 0.0)
         {
            losses++;
            gross_loss_r += -record.realized_r;
            gross_loss_money += -net;
            current_loss_streak++;
            current_win_streak = 0;
            if(current_loss_streak > longest_loss_streak)
               longest_loss_streak = current_loss_streak;
         }
         else
         {
            breakeven++;
         }
         if(record.realized_r > best_r)
            best_r = record.realized_r;
         if(record.realized_r < worst_r)
            worst_r = record.realized_r;
         sq_sum += record.realized_r * record.realized_r;

         cumulative_r += record.realized_r;
         if(cumulative_r > peak_r)
            peak_r = cumulative_r;
         double dd_r = peak_r - cumulative_r;
         if(dd_r > max_dd_r)
            max_dd_r = dd_r;

         cumulative_money += net;
         if(cumulative_money > peak_money)
            peak_money = cumulative_money;
         double dd_money = peak_money - cumulative_money;
         if(dd_money > max_dd_money)
            max_dd_money = dd_money;

         if(first_open == 0 || record.opened_at < first_open)
            first_open = record.opened_at;
         if(record.closed_at > last_close)
            last_close = record.closed_at;

         AddToBuckets(m_strategy, record.strategy, record);
         AddToBuckets(m_session, record.session, record);
         AddToBuckets(m_month, record.month, record);

         if(trades_handle != INVALID_HANDLE)
         {
            FileWriteString(trades_handle, StringFormat(
               "%I64u,%s,%d,%I64d,%I64d,%.5f,%.5f,%.2f,%.5f,%.5f,%.4f,"
               "%.4f,%.4f,%.1f,%.2f,%.2f,%.2f,%.2f,%d,%s,%s,%d\r\n",
               record.ticket, TA_StrategyName(record.strategy), record.direction,
               (long)record.opened_at, (long)record.closed_at, record.entry,
               record.exit, record.volume, record.initial_stop, record.target,
               record.realized_r, record.mfe_r, record.mae_r,
               record.spread_points, record.commission, record.swap,
               record.profit, net, record.bars_held,
               TA_ExitName(record.exit_reason), TA_SessionName(record.session),
               record.month));
         }
      }

      if(trades_handle != INVALID_HANDLE)
         FileClose(trades_handle);

      double win_rate = SafeDiv((double)wins, (double)total) * 100.0;
      double profit_factor = (gross_loss_r > 0.0) ?
                              gross_profit_r / gross_loss_r :
                              ((gross_profit_r > 0.0) ? 999.0 : 0.0);
      double profit_factor_money = (gross_loss_money > 0.0) ?
                                    gross_profit_money / gross_loss_money :
                                    ((gross_profit_money > 0.0) ? 999.0 : 0.0);
      double expectancy = SafeDiv(net_r, (double)total);
      double avg_win = SafeDiv(gross_profit_r, (double)wins);
      double avg_loss = SafeDiv(gross_loss_r, (double)losses);
      double payoff = (avg_loss > 0.0) ? avg_win / avg_loss : 0.0;
      double mean_net = net_r / (double)MathMax(1, total);
      double variance = 0.0;
      if(total > 1)
      {
         variance = (sq_sum - (double)total * mean_net * mean_net) /
                    (double)(total - 1);
         if(variance < 0.0)
            variance = 0.0;
      }
      double stdev = MathSqrt(variance);
      double sharpe = (stdev > 0.0) ? mean_net / stdev : 0.0;
      double tstat = (stdev > 0.0) ? sharpe * MathSqrt((double)total) : 0.0;

      double span_days = 0.0;
      if(last_close > first_open)
         span_days = (double)(last_close - first_open) / 86400.0;
      double trades_per_year = 0.0;
      double sharpe_annualized = 0.0;
      if(span_days > 0.0 && total > 0)
      {
         trades_per_year = (double)total / (span_days / 365.25);
         sharpe_annualized = sharpe * MathSqrt(trades_per_year);
      }
      double recovery_factor = (max_dd_r > 0.0) ? net_r / max_dd_r : 0.0;
      double max_dd_pct = 0.0;
      double final_equity = AccountInfoDouble(ACCOUNT_EQUITY);
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double initial_balance = (m_initial_balance > 0.0) ?
                               m_initial_balance : balance - net_money;
      if(initial_balance > 0.0)
         max_dd_pct = max_dd_money / initial_balance * 100.0;

      int summary = FileOpen(cfg.report_dir + "\\tester_summary.csv",
                             FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(summary != INVALID_HANDLE)
      {
         FileWriteString(summary,
            "section,key,trades,wins,losses,win_rate,net_r,gross_profit_r,"
            "gross_loss_r,profit_factor,avg_r,net_money\r\n");
         FileWriteString(summary, StringFormat(
            "summary,all,%d,%d,%d,%.2f,%.4f,%.4f,%.4f,%.4f,%.4f,%.2f\r\n",
            total, wins, losses, win_rate, net_r, gross_profit_r,
            gross_loss_r, profit_factor, expectancy, net_money));
         WriteBucketCSV(summary, 0, m_strategy);
         WriteBucketCSV(summary, 1, m_session);
         WriteBucketCSV(summary, 2, m_month);
         FileClose(summary);
      }

      int json = FileOpen(cfg.report_dir + "\\tester_summary.json",
                          FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(json != INVALID_HANDLE)
      {
         FileWriteString(json, "{");
         FileWriteString(json, StringFormat(
            "\"generated_at\":%I64d,\"symbol\":\"%s\",\"timeframe\":\"%s\","
            "\"mode\":\"%s\",\"initial_balance\":%.2f,\"final_equity\":%.2f,"
            "\"final_balance\":%.2f,\"trades\":%d,\"wins\":%d,\"losses\":%d,"
            "\"breakeven\":%d,\"win_rate\":%.2f,\"net_r\":%.4f,"
            "\"gross_profit_r\":%.4f,\"gross_loss_r\":%.4f,"
            "\"profit_factor\":%.4f,\"expectancy_r\":%.4f,"
            "\"avg_win_r\":%.4f,\"avg_loss_r\":%.4f,\"payoff\":%.4f,"
            "\"best_r\":%.4f,\"worst_r\":%.4f,\"sharpe_per_trade\":%.4f,"
            "\"sharpe_tstat\":%.4f,\"sharpe_annualized\":%.4f,"
            "\"trades_per_year\":%.2f,\"max_drawdown_r\":%.4f,"
            "\"max_drawdown_pct\":%.4f,\"recovery_factor\":%.4f,"
            "\"longest_win_streak\":%d,\"longest_loss_streak\":%d,"
            "\"net_money\":%.2f,\"gross_profit_money\":%.2f,"
            "\"gross_loss_money\":%.2f,\"profit_factor_money\":%.4f,"
            "\"commission\":%.2f,\"swap\":%.2f,\"spread_cost_estimate\":%.2f,"
            "\"avg_mfe_r\":%.4f,\"avg_mae_r\":%.4f,\"avg_bars\":%.2f,"
            "\"avg_spread_points\":%.2f,\"span_days\":%.2f",
            (long)TimeGMT(), cfg.symbol, EnumToString(cfg.timeframe),
            TA_ModeName(cfg.mode), initial_balance, final_equity,
            balance, total, wins, losses, breakeven, win_rate, net_r,
            gross_profit_r, gross_loss_r, profit_factor, expectancy, avg_win,
            avg_loss, payoff, best_r, worst_r, sharpe, tstat,
            sharpe_annualized, trades_per_year, max_dd_r, max_dd_pct,
            recovery_factor, longest_win_streak, longest_loss_streak, net_money,
            gross_profit_money, gross_loss_money, profit_factor_money,
            commission, swap, spread_cost,
            SafeDiv(mfe_total, (double)total), SafeDiv(mae_total, (double)total),
            SafeDiv(bars_total, (double)total),
            SafeDiv(spread_points_total, (double)total), span_days));
         FileWriteString(json, ",\"breakdown\":[");
         bool first = true;
         WriteBucketJSON(json, 0, m_strategy, first);
         WriteBucketJSON(json, 1, m_session, first);
         WriteBucketJSON(json, 2, m_month, first);
         FileWriteString(json, "],\"trades_file\":\"tester_trades.csv\"}");
         FileClose(json);
      }

      PrintFormat("TA report: %d trades netR=%.2f PF=%.2f DD=%.2fR win=%.1f%%",
                  total, net_r, profit_factor, max_dd_r, win_rate);
      return (trades_handle != INVALID_HANDLE);
   }
};

#endif // TA_TESTER_REPORT_MQH
