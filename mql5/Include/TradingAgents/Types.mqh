//+------------------------------------------------------------------+
//| Types.mqh - shared enums, config and numeric structs             |
//|                                                                  |
//| All structs used in arrays are numeric-only so MQL5 can copy     |
//| them safely.  Strings are derived from ids at the edges.         |
//+------------------------------------------------------------------+
#ifndef TA_TYPES_MQH
#define TA_TYPES_MQH

#define TA_PROTOCOL_VERSION 1
#define TA_MAX_ZONES        20
#define TA_MAX_SEEN         4096
#define TA_MAX_POSITIONS    64
#define TA_MAX_RECORDS      5000
#define TA_INVALID_HANDLE   -1

enum ENUM_TA_MODE
{
   TA_MODE_LIVE = 0,
   TA_MODE_TESTER = 1,
   TA_MODE_PAPER = 2
};

enum ENUM_TA_SESSION
{
   TA_SESSION_OFF = 0,
   TA_SESSION_ASIA = 1,
   TA_SESSION_LONDON = 2,
   TA_SESSION_OVERLAP = 3,
   TA_SESSION_NEWYORK = 4
};

enum ENUM_TA_STRATEGY
{
   TA_STRAT_NONE = -1,
   TA_STRAT_TREND = 0,
   TA_STRAT_BREAKOUT = 1,
   TA_STRAT_SMC = 2,
   TA_STRAT_MOMENTUM = 3,
   TA_STRAT_MEAN_REVERSION = 4
};

enum ENUM_TA_EXIT
{
   TA_EXIT_UNKNOWN = 0,
   TA_EXIT_STOP = 1,
   TA_EXIT_TARGET = 2,
   TA_EXIT_TIME = 3,
   TA_EXIT_STRUCTURE = 4,
   TA_EXIT_MOMENTUM = 5,
   TA_EXIT_MEAN_REVERSION = 6,
   TA_EXIT_BREAKEVEN = 7,
   TA_EXIT_TRAIL = 8,
   TA_EXIT_RISK_FLATTEN = 9,
   TA_EXIT_MANUAL = 10,
   TA_EXIT_PARTIAL = 11,
   TA_EXIT_OPPOSITE = 12
};

//+------------------------------------------------------------------+
//| Configuration assembled from EA inputs                           |
//+------------------------------------------------------------------+
struct TAConfig
{
   string            symbol;
   ENUM_TIMEFRAMES   timeframe;
   ENUM_TA_MODE      mode;
   long              magic;
   double            risk_percent;
   double            max_daily_loss_percent;
   double            max_daily_r;
   int               max_consecutive_losses;
   int               max_positions;
   double            max_total_lots;
   double            max_spread_points;
   int               slippage_points;
   int               warmup_bars;
   int               history_bars;
   bool              allow_new_trades;
   bool              use_native_signals;
   bool              flatten_on_lockout;
   int               heartbeat_ms;
   int               disconnect_grace_sec;
   string            host;
   int               port;
   string            token_file;
   string            report_dir;
   double            partial_at_r;
   double            partial_fraction;
   double            breakeven_at_r;
   double            trail_at_r;
   double            trail_atr;
   int               time_stop_bars;
   double            min_confidence;
   double            min_rr;
   int               max_signals_per_bar;
   bool              allow_opposite;
   int               utc_offset_override;
   int               stop_buffer_points;
   bool              close_on_opposite;
   bool              write_tester_report;
};

//+------------------------------------------------------------------+
//| One closed-bar feature snapshot                                  |
//+------------------------------------------------------------------+
struct TASnapshot
{
   bool     valid;
   datetime time;
   double   open;
   double   high;
   double   low;
   double   close;
   double   atr;
   double   atr_pct;
   double   atr_percentile;
   double   ema12;
   double   ema20;
   double   ema34;
   double   ema50;
   double   ema200;
   double   ema20_slope;
   double   ema20_slope_atr;
   double   ema50_slope;
   double   rsi;
   double   rsi_slope;
   double   adx;
   double   di_plus;
   double   di_minus;
   double   macd;
   double   macd_signal;
   double   macd_hist;
   double   macd_hist_slope;
   double   roc3;
   double   roc5;
   double   roc10;
   double   bb_mid;
   double   bb_upper;
   double   bb_lower;
   double   bb_width;
   double   bb_width_pct;
   double   bb_position;
   double   range_atr;
   double   body_atr;
   double   vol_z;
   double   last_swing_high;
   double   last_swing_low;
   double   range_high;
   double   range_low;
   double   range_mid;
   double   range_position;
   bool     bos_up;
   bool     bos_down;
   bool     choch_up;
   bool     choch_down;
   bool     sweep_high;
   bool     sweep_low;
   bool     displacement_up;
   bool     displacement_down;
   double   sweep_high_level;
   double   sweep_low_level;
   double   ob_bull_top;
   double   ob_bull_bottom;
   double   ob_bear_top;
   double   ob_bear_bottom;
   double   fvg_bull_top;
   double   fvg_bull_bottom;
   double   fvg_bear_top;
   double   fvg_bear_bottom;
   double   pd_high;
   double   pd_low;
   double   pd_close;
   double   pd_mid;
   double   pd_range;
   double   pd_position;
   double   asia_high;
   double   asia_low;
   double   london_high;
   double   london_low;
   int      session;
   int      structure_direction;
   double   spread_points;
};

//+------------------------------------------------------------------+
//| A strategy proposal (never an order)                             |
//+------------------------------------------------------------------+
struct TASignal
{
   bool     valid;
   int      strategy;
   int      direction;
   double   entry;
   double   stop;
   double   target;
   double   volume;
   double   risk_percent;
   double   confidence;
   double   rr;
   datetime bar_time;
   int      expires_bars;
   double   spread_points;
};

//+------------------------------------------------------------------+
//| Per-ticket management state (numeric only)                       |
//+------------------------------------------------------------------+
struct TAPositionState
{
   ulong    ticket;
   int      strategy;
   int      direction;
   double   entry;
   double   initial_stop;
   double   initial_risk;
   double   target;
   double   risk_money;
   datetime opened_at;
   double   mfe_r;
   double   mae_r;
   bool     partial_done;
   bool     breakeven_done;
   bool     adopted;
   int      bars_held;
   int      pending_exit_reason;
   bool     close_requested;
   double   entry_spread_points;
   double   initial_volume;
   uint     last_modify_ms;
};

//+------------------------------------------------------------------+
//| A completed trade for the machine-readable report                |
//+------------------------------------------------------------------+
struct TATradeRecord
{
   ulong    ticket;
   int      strategy;
   int      direction;
   datetime opened_at;
   datetime closed_at;
   double   entry;
   double   exit;
   double   volume;
   double   initial_stop;
   double   target;
   double   realized_r;
   double   mfe_r;
   double   mae_r;
   double   spread_points;
   double   commission;
   double   swap;
   double   profit;
   int      bars_held;
   int      exit_reason;
   int      session;
   int      month;
};

//+------------------------------------------------------------------+
//| Result of an order attempt                                       |
//+------------------------------------------------------------------+
struct TAFill
{
   bool     ok;
   ulong    ticket;
   double   price;
   double   volume;
   double   slippage_points;
   int      retcode;
   string   retcode_name;
   string   message;
};

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
double TA_Clamp(const double value, const double low, const double high)
{
   if(value < low)
      return low;
   if(value > high)
      return high;
   return value;
}

bool TA_Finite(const double value)
{
   if(!MathIsValidNumber(value))
      return false;
   if(value == EMPTY_VALUE || value == DBL_MAX || value == -DBL_MAX)
      return false;
   return true;
}

string TA_StrategyName(const int strategy)
{
   if(strategy == TA_STRAT_TREND)
      return "trend";
   if(strategy == TA_STRAT_BREAKOUT)
      return "breakout";
   if(strategy == TA_STRAT_SMC)
      return "smc";
   if(strategy == TA_STRAT_MOMENTUM)
      return "momentum";
   if(strategy == TA_STRAT_MEAN_REVERSION)
      return "mean_reversion";
   return "none";
}

int TA_StrategyId(const string name)
{
   string lower = name;
   StringToLower(lower);
   if(lower == "trend")
      return TA_STRAT_TREND;
   if(lower == "breakout")
      return TA_STRAT_BREAKOUT;
   if(lower == "smc")
      return TA_STRAT_SMC;
   if(lower == "momentum")
      return TA_STRAT_MOMENTUM;
   if(lower == "mean_reversion")
      return TA_STRAT_MEAN_REVERSION;
   return TA_STRAT_NONE;
}

double TA_StrategyWeight(const int strategy)
{
   if(strategy == TA_STRAT_TREND)
      return 1.10;
   if(strategy == TA_STRAT_BREAKOUT)
      return 1.00;
   if(strategy == TA_STRAT_SMC)
      return 1.15;
   if(strategy == TA_STRAT_MOMENTUM)
      return 0.95;
   if(strategy == TA_STRAT_MEAN_REVERSION)
      return 0.80;
   return 1.0;
}

int TA_StrategyMaxBars(const int strategy)
{
   if(strategy == TA_STRAT_TREND)
      return 40;
   if(strategy == TA_STRAT_BREAKOUT)
      return 20;
   if(strategy == TA_STRAT_SMC)
      return 30;
   if(strategy == TA_STRAT_MOMENTUM)
      return 24;
   if(strategy == TA_STRAT_MEAN_REVERSION)
      return 16;
   return 24;
}

double TA_StrategyMaxSpreadAtr(const int strategy)
{
   if(strategy == TA_STRAT_SMC)
      return 0.18;
   if(strategy == TA_STRAT_MOMENTUM)
      return 0.15;
   if(strategy == TA_STRAT_MEAN_REVERSION)
      return 0.12;
   if(strategy == TA_STRAT_BREAKOUT)
      return 0.15;
   return 0.15;
}

string TA_SessionName(const int session)
{
   if(session == TA_SESSION_ASIA)
      return "asia";
   if(session == TA_SESSION_LONDON)
      return "london";
   if(session == TA_SESSION_OVERLAP)
      return "overlap";
   if(session == TA_SESSION_NEWYORK)
      return "newyork";
   return "off";
}

bool TA_IsTradeSession(const int session)
{
   return (session == TA_SESSION_LONDON || session == TA_SESSION_OVERLAP ||
           session == TA_SESSION_NEWYORK);
}

string TA_ExitName(const int reason)
{
   if(reason == TA_EXIT_STOP)
      return "stop";
   if(reason == TA_EXIT_TARGET)
      return "target";
   if(reason == TA_EXIT_TIME)
      return "time";
   if(reason == TA_EXIT_STRUCTURE)
      return "structure";
   if(reason == TA_EXIT_MOMENTUM)
      return "momentum";
   if(reason == TA_EXIT_MEAN_REVERSION)
      return "mean_reversion";
   if(reason == TA_EXIT_BREAKEVEN)
      return "breakeven";
   if(reason == TA_EXIT_TRAIL)
      return "trail";
   if(reason == TA_EXIT_RISK_FLATTEN)
      return "risk_flatten";
   if(reason == TA_EXIT_MANUAL)
      return "manual";
   if(reason == TA_EXIT_PARTIAL)
      return "partial";
   if(reason == TA_EXIT_OPPOSITE)
      return "opposite";
   return "unknown";
}

string TA_ModeName(const ENUM_TA_MODE mode)
{
   if(mode == TA_MODE_LIVE)
      return "LIVE";
   if(mode == TA_MODE_TESTER)
      return "TESTER";
   return "PAPER";
}

#endif // TA_TYPES_MQH
