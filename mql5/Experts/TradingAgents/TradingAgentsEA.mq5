//+------------------------------------------------------------------+
//| TradingAgentsEA.mq5                                              |
//|                                                                  |
//| Native XAUUSD EA for the Trading Agents project.                 |
//|                                                                  |
//| Two operating paths:                                             |
//|   * TESTER  - deterministic native signals, no sockets, report.  |
//|   * LIVE/PAPER - optional Python intents over loopback, plus     |
//|     independent risk checks and tick-by-tick position management.|
//|                                                                  |
//| The EA never trusts an external intent blindly.  It re-validates |
//| symbol, price, stops, spread, session, volume, margin, exposure  |
//| and duplicate keys before sending anything to the broker.        |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "Trading Agents native EA (XAUUSD, five strategies)"
#property description "Default mode is PAPER (dry-run). Tester mode uses native signals only."

#include <TradingAgents/Types.mqh>
#include <TradingAgents/Json.mqh>
#include <TradingAgents/Protocol.mqh>
#include <TradingAgents/Socket.mqh>
#include <TradingAgents/Features.mqh>
#include <TradingAgents/Signals.mqh>
#include <TradingAgents/State.mqh>
#include <TradingAgents/Risk.mqh>
#include <TradingAgents/TradeManager.mqh>
#include <TradingAgents/TesterReport.mqh>

//--- general
input string          InpSymbol               = "";          // Symbol (empty = chart symbol)
input ENUM_TIMEFRAMES InpTimeframe            = PERIOD_M15;  // Signal timeframe
input ENUM_TA_MODE    InpMode                 = TA_MODE_PAPER;// Mode (PAPER = dry-run)
input long            InpMagic                = 20261102;    // Magic number
input bool            InpAllowNewTrades       = true;        // Allow new entries
input bool            InpUseNativeSignals     = true;        // Use native strategy engine
input bool            InpNativeInLive         = false;       // Allow native entries in LIVE mode

//--- risk
input double          InpRiskPercent          = 0.50;        // Risk per trade (% equity)
input double          InpMaxDailyLossPercent  = 2.00;        // Daily loss lockout (%)
input double          InpMaxDailyR            = 3.00;        // Daily realized-R lockout (R)
input int             InpMaxConsecutiveLosses = 3;           // Consecutive-loss lockout
input int             InpMaxPositions         = 1;           // Max simultaneous positions
input double          InpMaxTotalLots         = 0.30;        // Max total volume (lots)
input double          InpMaxSpreadPoints      = 80.0;        // Max spread (points)
input int             InpSlippagePoints       = 30;          // Max slippage (points)
input int             InpStopBufferPoints     = 5;           // Extra stop buffer (points)
input bool            InpFlattenOnLockout     = true;        // Flatten on daily lockout
input bool            InpCloseOnOpposite      = true;        // Close opposite position on new signal

//--- management
input double          InpPartialAtR           = 1.00;        // Partial profit trigger (R)
input double          InpPartialFraction      = 0.50;        // Partial close fraction
input double          InpBreakevenAtR         = 1.00;        // Breakeven trigger (R)
input double          InpTrailAtR             = 1.50;        // Trailing trigger (R)
input double          InpTrailAtr             = 1.20;        // Trailing distance (ATR)
input int             InpTimeStopBars         = 0;           // Extra time stop (bars, 0=engine)
input double          InpMinConfidence        = 0.45;        // Minimum signal confidence
input double          InpMinRR                = 1.50;        // Minimum reward:risk
input int             InpMaxSignalsPerBar     = 1;           // Max entries per closed bar
input bool            InpAllowOpposite        = false;       // Allow long and short together

//--- features
input int             InpWarmupBars           = 300;         // Warmup bars
input int             InpHistoryBars          = 900;         // History bars requested
input int             InpUtcOffsetOverride    = -99;         // UTC offset hours (-99 = auto)

//--- protocol
input string          InpHost                 = "127.0.0.1"; // Python bridge host
input int             InpPort                 = 8765;        // Python bridge port
input string          InpTokenFile            = "TradingAgents\\token.txt"; // Common-files token
input int             InpHeartbeatMs          = 1000;        // EA heartbeat (ms)
input int             InpDisconnectGraceSec   = 30;          // Entry grace after Python loss (s)

//--- reporting
input string          InpReportDir            = "TradingAgents"; // Common-files report folder
input bool            InpWriteTesterReport   = true;        // Write tester report

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
TAConfig          g_cfg;
TAFeatures        g_features;
TASignals         g_signals;
TARisk            g_risk;
TAState           g_state;
TATradeManager    g_trades;
TATesterReport    g_report;
TASocket          g_socket;

bool              g_features_ready = false;
bool              g_warmup_failed_logged = false;
bool              g_token_ok = false;
string            g_token = "";
datetime          g_last_bar = 0;
datetime          g_last_signal_bar = 0;
long              g_seq = 0;
long              g_server_seq = 0;
uint              g_last_heartbeat = 0;
uint              g_last_connect_attempt = 0;
uint              g_last_connect_log = 0;
uint              g_last_server_rx = 0;
uint              g_last_position_send = 0;
bool              g_positions_dirty = false;
bool              g_need_sync = false;
bool              g_connected_ever = false;
bool              g_entry_block_logged = false;
bool              g_report_written = false;
bool              g_lock_flattened = false;
int               g_last_record_count = 0;
ulong             g_closed_queue[];
int               g_closed_count = 0;
datetime          g_started_at = 0;

#define TA_ACK_FILLED    0
#define TA_ACK_DUPLICATE 1
#define TA_ACK_REJECTED  2
#define TA_ACK_DRY_RUN   3

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
long CurrentDayId()
{
   long offset = (long)g_features.UtcOffsetSeconds();
   long seconds = (long)TimeCurrent() + offset;
   long day = seconds / 86400;
   if(seconds < 0 && (seconds % 86400) != 0)
      day--;
   return day;
}

int CurrentSession()
{
   long offset = (long)g_features.UtcOffsetSeconds();
   long seconds = (long)TimeCurrent() + offset;
   long day = seconds / 86400;
   if(seconds < 0 && (seconds % 86400) != 0)
      day--;
   int in_day = (int)(seconds - day * 86400);
   int hour = in_day / 3600;
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

double CurrentSpreadPoints()
{
   double point = SymbolInfoDouble(g_cfg.symbol, SYMBOL_POINT);
   if(point <= 0.0)
      return 0.0;
   double ask = SymbolInfoDouble(g_cfg.symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(g_cfg.symbol, SYMBOL_BID);
   if(ask > 0.0 && bid > 0.0)
      return (ask - bid) / point;
   return (double)SymbolInfoInteger(g_cfg.symbol, SYMBOL_SPREAD);
}

int OpenPositionCount()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_cfg.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_cfg.magic)
         continue;
      count++;
   }
   return count;
}

bool PythonFresh()
{
   if(g_cfg.mode != TA_MODE_LIVE)
      return true;
   if(!g_socket.IsOpen() || !g_socket.IsConnected())
      return false;
   if(g_last_server_rx == 0)
      return false;
   uint age = GetTickCount() - g_last_server_rx;
   return (age <= (uint)MathMax(1, g_cfg.disconnect_grace_sec) * 1000);
}

bool EntriesFresh()
{
   if(g_cfg.mode == TA_MODE_TESTER)
      return true;
   if(g_cfg.mode == TA_MODE_PAPER)
      return true;
   return PythonFresh();
}

void RememberClosedTicket(const ulong ticket)
{
   if(ticket == 0)
      return;
   for(int i = 0; i < g_closed_count; i++)
   {
      if(g_closed_queue[i] == ticket)
         return;
   }
   if(g_closed_count >= 32)
   {
      for(int i = 1; i < g_closed_count; i++)
         g_closed_queue[i - 1] = g_closed_queue[i];
      g_closed_count = 31;
   }
   ArrayResize(g_closed_queue, g_closed_count + 1);
   g_closed_queue[g_closed_count] = ticket;
   g_closed_count++;
   g_positions_dirty = true;
}

void RememberNewRecords(const int before)
{
   int after = g_state.RecordCount();
   for(int i = before; i < after; i++)
   {
      TATradeRecord record;
      if(g_state.RecordAt(i, record))
         RememberClosedTicket(record.ticket);
   }
}

bool BuildSnapshot(string &payload)
{
   g_trades.BuildPositionUpdate(g_cfg, g_state, payload);
   if(g_closed_count <= 0)
      return true;
   int length = StringLen(payload);
   if(length < 2 || StringSubstr(payload, length - 2, 2) != "]}")
      return true;
   payload = StringSubstr(payload, 0, length - 2);
   for(int i = 0; i < g_closed_count; i++)
   {
      payload += StringFormat(
         ",{\"ticket\":%I64d,\"symbol\":\"%s\",\"status\":\"closed\"}",
         (long)g_closed_queue[i], TA_JsonEscape(g_cfg.symbol));
   }
   payload += "]}";
   return true;
}

bool SendMessage(const string msg_type, const string payload)
{
   if(!g_socket.IsOpen() || !g_token_ok)
      return false;
   string msg_id = "";
   long next_seq = g_seq + 1;
   string line = TA_BuildEnvelope(msg_type, payload, g_token, next_seq, msg_id);
   if(StringLen(line) > TA_MAX_FRAME_BYTES)
   {
      Print("TA: outbound frame exceeds 1 MiB; dropping connection");
      g_socket.Close();
      return false;
   }
   if(!g_socket.SendLine(line))
   {
      g_socket.Close();
      return false;
   }
   g_seq = next_seq;
   return true;
}

void SendHeartbeat()
{
   if(!g_socket.IsOpen())
      return;
   string payload = StringFormat(
      "{\"equity\":%.2f,\"balance\":%.2f,\"margin_free\":%.2f,"
      "\"positions\":%d,\"locked\":%s,\"lock_reason\":\"%s\","
      "\"day_r\":%.4f,\"last_bar_time\":%I64d,\"uptime_seconds\":%I64d,"
      "\"pending_intents\":0,\"mode\":\"%s\",\"native_ready\":%s,"
      "\"consecutive_losses\":%d}",
      AccountInfoDouble(ACCOUNT_EQUITY), AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE), OpenPositionCount(),
      g_state.Locked() ? "true" : "false", TA_JsonEscape(g_state.LockReason()),
      g_state.DayRealizedR(), (long)g_features.LastProcessed(),
      (long)(TimeLocal() - g_started_at), TA_ModeName(g_cfg.mode),
      g_features_ready ? "true" : "false", g_state.ConsecutiveLosses());
   if(SendMessage("heartbeat", payload))
      g_last_heartbeat = GetTickCount();
}

void SendPositions(const bool force = false)
{
   if(!g_socket.IsOpen())
      return;
   uint now = GetTickCount();
   if(!force && !g_positions_dirty && (now - g_last_position_send) < 15000)
      return;
   string payload = "";
   BuildSnapshot(payload);
   if(SendMessage("position_update", payload))
   {
      g_positions_dirty = false;
      g_last_position_send = now;
      g_closed_count = 0;
      ArrayResize(g_closed_queue, 0);
   }
}

void SendHello()
{
   string payload = StringFormat(
      "{\"role\":\"ea\",\"version\":\"1.0.0\",\"account\":%I64d,"
      "\"server\":\"%s\",\"symbol\":\"%s\",\"period\":\"%s\","
      "\"mode\":\"%s\",\"dry_run\":%s,\"magic\":%I64d,"
      "\"terminal_build\":%d,\"utc_offset_seconds\":%d}",
      (long)AccountInfoInteger(ACCOUNT_LOGIN),
      TA_JsonEscape(AccountInfoString(ACCOUNT_SERVER)),
      TA_JsonEscape(g_cfg.symbol), EnumToString(g_cfg.timeframe),
      TA_ModeName(g_cfg.mode),
      (g_cfg.mode == TA_MODE_LIVE ? "false" : "true"),
      g_cfg.magic, (int)TerminalInfoInteger(TERMINAL_BUILD),
      g_features.UtcOffsetSeconds());
   SendMessage("hello", payload);
}

void SendAck(const string key, const int status, const ulong ticket,
             const double price, const double volume, const int retcode,
             const double slippage, const string message)
{
   string payload = TA_AckPayload(key, status, ticket, price, volume,
                                  retcode, slippage, message);
   SendMessage("ack", payload);
}

void SendReject(const string msg_id, const string reason)
{
   string payload = "{\"id\":\"" + TA_JsonEscape(msg_id) +
                    "\",\"reason\":\"" + TA_JsonEscape(reason) + "\"}";
   SendMessage("reject", payload);
}

void SendReconcile(const string request)
{
   string wrapper = "";
   g_trades.BuildPositionUpdate(g_cfg, g_state, wrapper);
   string positions = TA_JsonGetArray(wrapper, "positions", "[]");

   string orders = "[";
   bool first_order = true;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
         continue;
      if(OrderGetString(ORDER_SYMBOL) != g_cfg.symbol)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) != g_cfg.magic)
         continue;
      if(!first_order)
         orders += ",";
      first_order = false;
      orders += StringFormat(
         "{\"ticket\":%I64d,\"type\":%d,\"volume\":%.2f,\"price\":%.5f,"
         "\"sl\":%.5f,\"tp\":%.5f}",
         (long)ticket, (int)OrderGetInteger(ORDER_TYPE),
         OrderGetDouble(ORDER_VOLUME_CURRENT),
         OrderGetDouble(ORDER_PRICE_OPEN),
         OrderGetDouble(ORDER_SL), OrderGetDouble(ORDER_TP));
   }
   orders += "]";

   string account = StringFormat(
      "{\"equity\":%.2f,\"balance\":%.2f,\"margin_free\":%.2f,"
      "\"currency\":\"%s\",\"leverage\":%d}",
      AccountInfoDouble(ACCOUNT_EQUITY), AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE),
      TA_JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)),
      (int)AccountInfoInteger(ACCOUNT_LEVERAGE));

   ulong tickets[];
   int ticket_count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_cfg.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_cfg.magic)
         continue;
      ArrayResize(tickets, ticket_count + 1);
      tickets[ticket_count] = ticket;
      ticket_count++;
   }

   string mismatches = "[";
   bool first_mismatch = true;
   for(int i = 0; i < ticket_count; i++)
   {
      string needle = "\"ticket\":" + StringFormat("%I64d", (long)tickets[i]);
      if(StringFind(positions, needle) >= 0)
         continue;
      if(!first_mismatch)
         mismatches += ",";
      first_mismatch = false;
      mismatches += StringFormat(
         "{\"kind\":\"missing_in_python\",\"ticket\":%I64d}",
         (long)tickets[i]);
   }
   int search = 0;
   while(true)
   {
      int found = StringFind(request, "\"ticket\":", search);
      if(found < 0)
         break;
      int begin = found + 9;
      int end = begin;
      int n = StringLen(request);
      while(end < n && StringGetCharacter(request, end) >= '0' &&
            StringGetCharacter(request, end) <= '9')
         end++;
      string digits = StringSubstr(request, begin, end - begin);
      search = end;
      if(StringLen(digits) == 0)
         continue;
      long requested = StringToInteger(digits);
      bool present = false;
      for(int i = 0; i < ticket_count; i++)
      {
         if((long)tickets[i] == requested)
         {
            present = true;
            break;
         }
      }
      if(present)
         continue;
      if(!first_mismatch)
         mismatches += ",";
      first_mismatch = false;
      mismatches += StringFormat(
         "{\"kind\":\"missing_at_broker\",\"ticket\":%I64d}", requested);
   }
   mismatches += "]";

   string payload = "{\"broker_positions\":" + positions +
                    ",\"positions\":" + positions +
                    ",\"broker_orders\":" + orders +
                    ",\"account\":" + account +
                    ",\"mismatches\":" + mismatches +
                    ",\"unknown_keys\":[]" +
                    ",\"server_time\":" + StringFormat("%.3f", (double)TimeGMT()) +
                    "}";
   SendMessage("reconcile_result", payload);
   SendPositions(true);
}

//+------------------------------------------------------------------+
//| Intent and command handling                                      |
//+------------------------------------------------------------------+
void ProcessIntent(const string payload)
{
   string key = TA_JsonGetString(payload, "idempotency_key", "");
   if(StringLen(key) < 16)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "missing or short idempotency_key");
      return;
   }
   ulong seen_ticket = 0;
   int seen_status = 0;
   if(g_state.FindKey(key, seen_ticket, seen_status))
   {
      SendAck(key, TA_ACK_DUPLICATE, seen_ticket, 0.0, 0.0, 10025, 0.0,
              "duplicate idempotency key");
      return;
   }

   string symbol = TA_JsonGetString(payload, "symbol", "");
   if(symbol != g_cfg.symbol)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "symbol mismatch: " + symbol);
      return;
   }
   int direction = (int)TA_JsonGetLong(payload, "direction", 0);
   if(direction != 1 && direction != -1)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "direction must be 1 or -1");
      return;
   }
   string strategy_name = TA_JsonGetString(payload, "strategy", "");
   int strategy = TA_StrategyId(strategy_name);
   if(strategy == TA_STRAT_NONE)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "unknown strategy");
      return;
   }
   double stop = TA_JsonGetDouble(payload, "sl", 0.0);
   double target = TA_JsonGetDouble(payload, "tp", 0.0);
   if(stop <= 0.0 || target <= 0.0)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10016, 0.0,
              "sl and tp are required");
      return;
   }
   long expires = TA_JsonGetLong(payload, "expires_at", 0);
   if(expires > 0 && (long)TimeGMT() > expires)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "intent expired");
      return;
   }

   TASnapshot snap;
   g_features.Current(snap);
   if(!snap.valid)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "no closed-bar snapshot");
      return;
   }

   TASnapshot gate = snap;
   gate.session = CurrentSession();
   gate.spread_points = CurrentSpreadPoints();
   string reason = "";
   if(!g_risk.NewEntriesAllowed(g_cfg, gate, g_state, true, reason))
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0, reason);
      return;
   }

   TASignal signal;
   signal.valid = true;
   signal.strategy = strategy;
   signal.direction = direction;
   signal.entry = TA_JsonGetDouble(payload, "entry", 0.0);
   signal.stop = stop;
   signal.target = target;
   signal.volume = TA_JsonGetDouble(payload, "volume", 0.0);
   signal.risk_percent = TA_JsonGetDouble(payload, "risk_percent", 0.0);
   signal.confidence = 1.0;
   double risk_distance = MathAbs(signal.entry - stop);
   if(signal.entry <= 0.0)
   {
      double ask = SymbolInfoDouble(g_cfg.symbol, SYMBOL_ASK);
      double bid = SymbolInfoDouble(g_cfg.symbol, SYMBOL_BID);
      risk_distance = MathAbs((direction > 0 ? ask : bid) - stop);
   }
   signal.rr = (risk_distance > 0.0) ?
               MathAbs(target - (signal.entry > 0.0 ? signal.entry :
                      (direction > 0 ? SymbolInfoDouble(g_cfg.symbol, SYMBOL_ASK) :
                       SymbolInfoDouble(g_cfg.symbol, SYMBOL_BID)))) / risk_distance : 0.0;
   signal.bar_time = TimeCurrent();
   signal.expires_bars = 1;
   signal.spread_points = gate.spread_points;

   int slippage = (int)TA_JsonGetLong(payload, "max_slippage_points", 0);
   if(slippage > 0 && slippage < g_cfg.slippage_points)
      g_cfg.slippage_points = slippage;

   TAFill fill;
   bool ok = g_trades.Execute(g_cfg, signal, key, g_state, g_risk, fill);
   if(ok)
   {
      int status = (g_cfg.mode == TA_MODE_PAPER) ? TA_ACK_DRY_RUN : TA_ACK_FILLED;
      if(g_cfg.mode == TA_MODE_PAPER)
         g_state.RememberKey(key, 0, TA_ACK_DRY_RUN);
      SendAck(key, status, fill.ticket, fill.price, fill.volume,
              fill.retcode, fill.slippage_points,
              (g_cfg.mode == TA_MODE_PAPER) ?
              "dry-run: validated, no order sent" : fill.message);
      g_positions_dirty = true;
      SendPositions(true);
      return;
   }

   if(StringFind(fill.message, "duplicate") >= 0)
   {
      SendAck(key, TA_ACK_DUPLICATE, fill.ticket, 0.0, 0.0, 10025, 0.0,
              fill.message);
      return;
   }
   SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0,
           fill.retcode > 0 ? fill.retcode : 10013,
           fill.slippage_points, fill.message);
}

void ProcessCloseRequest(const string payload)
{
   string key = TA_JsonGetString(payload, "idempotency_key", "");
   long ticket_value = TA_JsonGetLong(payload, "ticket", 0);
   ulong ticket = (ulong)ticket_value;
   if(ticket == 0)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "missing ticket");
      return;
   }
   ulong seen_ticket = 0;
   int seen_status = 0;
   if(StringLen(key) >= 8 && g_state.FindKey(key, seen_ticket, seen_status))
   {
      SendAck(key, TA_ACK_DUPLICATE, seen_ticket, 0.0, 0.0, 10025, 0.0,
              "duplicate close request");
      return;
   }
   if(!PositionSelectByTicket(ticket))
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10036, 0.0,
              "position not found");
      return;
   }
   if(PositionGetString(POSITION_SYMBOL) != g_cfg.symbol ||
      PositionGetInteger(POSITION_MAGIC) != g_cfg.magic)
   {
      SendAck(key, TA_ACK_REJECTED, 0, 0.0, 0.0, 10013, 0.0,
              "ticket does not belong to this EA");
      return;
   }
   double price = PositionGetDouble(POSITION_PRICE_CURRENT);
   double volume = PositionGetDouble(POSITION_VOLUME);
   bool closed = g_trades.CloseWithReason(g_cfg, ticket, TA_EXIT_MANUAL, g_state);
   if(!closed)
   {
      SendAck(key, TA_ACK_REJECTED, ticket, price, volume, 10013, 0.0,
              "close request rejected by broker");
      return;
   }
   if(StringLen(key) >= 8)
      g_state.RememberKey(key, ticket, TA_ACK_FILLED);
   SendAck(key, TA_ACK_FILLED, ticket, price, volume, 10009, 0.0,
           "close request accepted");
   g_positions_dirty = true;
   SendPositions(true);
}

void ProcessHalt(const string payload)
{
   string reason = TA_JsonGetString(payload, "reason", "python halt");
   bool flatten = TA_JsonGetBool(payload, "flatten", false);
   long until = TA_JsonGetLong(payload, "until", 0);
   g_state.Halt(reason, until);
   PrintFormat("TA: halt received: %s (flatten=%s)", reason,
               flatten ? "true" : "false");
   if(flatten)
   {
      int before = g_state.RecordCount();
      g_trades.CloseAll(g_cfg, g_state, TA_EXIT_RISK_FLATTEN);
      RememberNewRecords(before);
   }
   SendHeartbeat();
   SendPositions(true);
}

void ProcessResume(const string payload)
{
   string reason = TA_JsonGetString(payload, "reason", "python resume");
   g_state.Resume();
   PrintFormat("TA: resume received: %s", reason);
   SendHeartbeat();
}

//+------------------------------------------------------------------+
//| Socket pump                                                      |
//+------------------------------------------------------------------+
void ConnectIfNeeded()
{
   if(g_cfg.mode == TA_MODE_TESTER)
      return;
   if(!g_token_ok)
      return;
   if(g_socket.IsOpen())
   {
      if(!g_socket.IsConnected())
      {
         Print("TA: socket disconnected");
         g_socket.Close();
         g_last_position_send = 0;
      }
      else
      {
         return;
      }
   }
   uint now = GetTickCount();
   if(g_last_connect_attempt != 0 && (now - g_last_connect_attempt) < 5000)
      return;
   g_last_connect_attempt = now;
   string error = "";
   if(!g_socket.Connect(g_cfg.host, g_cfg.port, 1500, error))
   {
      if(g_last_connect_log == 0 || (now - g_last_connect_log) > 60000)
      {
         PrintFormat("TA: Python bridge unavailable: %s", error);
         g_last_connect_log = now;
      }
      return;
   }
   g_seq = 0;
   g_server_seq = 0;
   g_last_server_rx = GetTickCount();
   g_last_heartbeat = 0;
   g_last_position_send = 0;
   g_connected_ever = true;
   g_need_sync = true;
   PrintFormat("TA: connected to Python bridge %s:%d", g_cfg.host, g_cfg.port);
   SendHello();
}

void HandleMessage(const string line)
{
   TAEnvelope envelope;
   bool auth_failed = false;
   if(!TA_ParseEnvelope(line, g_token, envelope, auth_failed))
   {
      if(auth_failed)
      {
         PrintFormat("TA: protocol authentication failed: %s", envelope.error);
         g_socket.Close();
         return;
      }
      string msg_id = TA_JsonGetString(line, "id", "");
      SendReject(msg_id, envelope.error);
      return;
   }

   g_last_server_rx = GetTickCount();
   if(envelope.type == "hello")
   {
      g_server_seq = envelope.seq;
   }
   else
   {
      if(envelope.seq <= g_server_seq)
      {
         PrintFormat("TA: ignoring old/duplicate seq %I64d (last %I64d)",
                     envelope.seq, g_server_seq);
         return;
      }
      if(g_server_seq > 0 && envelope.seq > g_server_seq + 1)
      {
         string resync = StringFormat(
            "{\"expected_seq\":%I64d,\"received_seq\":%I64d}",
            g_server_seq + 1, envelope.seq);
         SendMessage("resync", resync);
         SendReject(envelope.id, StringFormat(
            "sequence gap: expected %I64d received %I64d",
            g_server_seq + 1, envelope.seq));
         return;
      }
      g_server_seq = envelope.seq;
   }

   if(envelope.type == "hello")
   {
      g_need_sync = true;
      SendPositions(true);
      SendReconcile("{}");
   }
   else if(envelope.type == "heartbeat")
   {
      // Freshness is tracked by g_last_server_rx.
   }
   else if(envelope.type == "intent")
   {
      ProcessIntent(envelope.payload);
   }
   else if(envelope.type == "close_request")
   {
      ProcessCloseRequest(envelope.payload);
   }
   else if(envelope.type == "halt")
   {
      ProcessHalt(envelope.payload);
   }
   else if(envelope.type == "resume")
   {
      ProcessResume(envelope.payload);
   }
   else if(envelope.type == "reconcile_request")
   {
      SendReconcile(envelope.payload);
   }
   else if(envelope.type == "resync")
   {
      SendHeartbeat();
      SendPositions(true);
      SendReconcile("{}");
   }
   else
   {
      SendReject(envelope.id, "unknown message type: " + envelope.type);
   }
}

void PumpSocket()
{
   if(g_cfg.mode == TA_MODE_TESTER)
      return;
   ConnectIfNeeded();
   if(g_socket.IsOpen())
   {
      int guard = 0;
      string line = "";
      while(guard++ < 100 && g_socket.Poll(line))
         HandleMessage(line);
      if(g_need_sync && g_socket.IsOpen())
      {
         g_need_sync = false;
         SendPositions(true);
         SendReconcile("{}");
      }
   }

   uint now = GetTickCount();
   if(g_socket.IsOpen())
   {
      if(g_last_heartbeat == 0 ||
         (now - g_last_heartbeat) >= (uint)MathMax(250, g_cfg.heartbeat_ms))
         SendHeartbeat();
      if(g_positions_dirty || (now - g_last_position_send) >= 15000)
         SendPositions(false);
   }
}

//+------------------------------------------------------------------+
//| Trading                                                          |
//+------------------------------------------------------------------+
bool EnsureFeatures()
{
   if(g_features_ready)
      return true;
   TASnapshot snapshot;
   if(!g_features.Warmup(snapshot))
   {
      if(!g_warmup_failed_logged)
      {
         Print("TA: waiting for enough closed-bar history");
         g_warmup_failed_logged = true;
      }
      return false;
   }
   g_features_ready = true;
   g_last_bar = iTime(g_cfg.symbol, g_cfg.timeframe, 0);
   PrintFormat("TA: features ready, last closed bar %s",
               TimeToString(snapshot.time, TIME_DATE | TIME_MINUTES));
   return true;
}

void CloseOpposite(const int direction)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_cfg.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_cfg.magic)
         continue;
      int position_direction =
         (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      if(position_direction == direction)
         continue;
      g_trades.CloseWithReason(g_cfg, ticket, TA_EXIT_OPPOSITE, g_state);
   }
}

void EvaluateNative(const TASnapshot &snapshot)
{
   if(!g_cfg.use_native_signals)
      return;
   if(g_cfg.mode == TA_MODE_LIVE && !InpNativeInLive)
      return;
   if(snapshot.time == g_last_signal_bar)
      return;
   g_last_signal_bar = snapshot.time;

   TASnapshot gate = snapshot;
   gate.session = CurrentSession();
   gate.spread_points = CurrentSpreadPoints();
   string reason = "";
   if(!g_risk.NewEntriesAllowed(g_cfg, gate, g_state, EntriesFresh(), reason))
   {
      if(!g_entry_block_logged)
      {
         PrintFormat("TA: new entries blocked: %s", reason);
         g_entry_block_logged = true;
      }
      return;
   }
   g_entry_block_logged = false;

   TASignal signal;
   if(!g_signals.Evaluate(snapshot, g_cfg, signal))
      return;
   if(g_cfg.max_signals_per_bar <= 0)
      return;

   string key = TA_NativeKey(TA_StrategyName(signal.strategy), g_cfg.symbol,
                             signal.bar_time, signal.direction);
   ulong seen_ticket = 0;
   int seen_status = 0;
   if(g_state.FindKey(key, seen_ticket, seen_status))
      return;

   if(g_cfg.close_on_opposite)
      CloseOpposite(signal.direction);

   TAFill fill;
   bool ok = g_trades.Execute(g_cfg, signal, key, g_state, g_risk, fill);
   if(ok)
   {
      g_positions_dirty = true;
      PrintFormat("TA native entry: %s conf=%.2f rr=%.2f %s @ %.5f",
                  TA_StrategyName(signal.strategy), signal.confidence,
                  signal.rr, (signal.direction > 0 ? "BUY" : "SELL"),
                  fill.price);
      if(g_cfg.mode != TA_MODE_TESTER)
         SendPositions(true);
   }
   else
   {
      PrintFormat("TA native entry rejected: %s %s",
                  TA_StrategyName(signal.strategy), fill.message);
   }
}

void ManageAndSync(const TASnapshot &snapshot)
{
   int before = g_state.RecordCount();
   g_trades.ManagePositions(g_cfg, snapshot, g_state, g_risk,
                            g_features.UtcOffsetSeconds());
   int after = g_state.RecordCount();
   if(after > before)
   {
      RememberNewRecords(before);
      g_last_record_count = after;
   }
   if(g_state.Locked() && g_cfg.flatten_on_lockout && !g_lock_flattened)
   {
      g_lock_flattened = true;
      PrintFormat("TA: lockout flatten: %s", g_state.LockReason());
      int records = g_state.RecordCount();
      g_trades.CloseAll(g_cfg, g_state, TA_EXIT_RISK_FLATTEN);
      RememberNewRecords(records);
   }
   if(!g_state.Locked())
      g_lock_flattened = false;
}

void OnNewBar()
{
   TASnapshot snapshot;
   if(!g_features.OnNewBar(snapshot))
      return;
   g_last_bar = iTime(g_cfg.symbol, g_cfg.timeframe, 0);

   // Manage first so a position closed by a time/structure exit frees a slot.
   ManageAndSync(snapshot);
   EvaluateNative(snapshot);

   if(g_cfg.mode != TA_MODE_TESTER)
      SendPositions(false);
}

void RollDay()
{
   long day = CurrentDayId();
   if(day != g_state.DayId())
   {
      g_state.RollDay(day, AccountInfoDouble(ACCOUNT_EQUITY));
      g_lock_flattened = false;
   }
}

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
{
   g_started_at = TimeLocal();
   g_closed_count = 0;
   ArrayResize(g_closed_queue, 0);
   g_report_written = false;

   g_cfg.symbol = (StringLen(InpSymbol) > 0) ? InpSymbol : _Symbol;
   if(!SymbolSelect(g_cfg.symbol, true))
   {
      PrintFormat("TA: symbol %s is not available", g_cfg.symbol);
      return INIT_FAILED;
   }
   double point = SymbolInfoDouble(g_cfg.symbol, SYMBOL_POINT);
   double tick_size = SymbolInfoDouble(g_cfg.symbol, SYMBOL_TRADE_TICK_SIZE);
   if(point <= 0.0 || tick_size <= 0.0)
   {
      PrintFormat("TA: symbol %s has invalid point/tick size", g_cfg.symbol);
      return INIT_FAILED;
   }

   g_cfg.timeframe = InpTimeframe;
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION))
      g_cfg.mode = TA_MODE_TESTER;
   else if(InpMode == TA_MODE_TESTER)
      g_cfg.mode = TA_MODE_PAPER;
   else
      g_cfg.mode = InpMode;

   g_cfg.magic = (InpMagic != 0) ? InpMagic : 20261102;
   g_cfg.risk_percent = MathMax(0.0, MathMin(10.0, InpRiskPercent));
   g_cfg.max_daily_loss_percent = MathMax(0.0, InpMaxDailyLossPercent);
   g_cfg.max_daily_r = MathMax(0.0, InpMaxDailyR);
   g_cfg.max_consecutive_losses = MathMax(0, InpMaxConsecutiveLosses);
   g_cfg.max_positions = MathMax(1, InpMaxPositions);
   g_cfg.max_total_lots = MathMax(0.0, InpMaxTotalLots);
   g_cfg.max_spread_points = MathMax(0.0, InpMaxSpreadPoints);
   g_cfg.slippage_points = MathMax(1, InpSlippagePoints);
   g_cfg.warmup_bars = MathMax(100, InpWarmupBars);
   g_cfg.history_bars = MathMax(g_cfg.warmup_bars + 100, InpHistoryBars);
   g_cfg.allow_new_trades = InpAllowNewTrades;
   g_cfg.use_native_signals = InpUseNativeSignals;
   g_cfg.flatten_on_lockout = InpFlattenOnLockout;
   g_cfg.heartbeat_ms = MathMax(250, InpHeartbeatMs);
   g_cfg.disconnect_grace_sec = MathMax(5, InpDisconnectGraceSec);
   g_cfg.host = InpHost;
   g_cfg.port = MathMax(1, MathMin(65535, InpPort));
   g_cfg.token_file = InpTokenFile;
   g_cfg.report_dir = (StringLen(InpReportDir) > 0) ? InpReportDir : "TradingAgents";
   g_cfg.partial_at_r = MathMax(0.0, InpPartialAtR);
   g_cfg.partial_fraction = MathMax(0.0, MathMin(0.90, InpPartialFraction));
   g_cfg.breakeven_at_r = MathMax(0.0, InpBreakevenAtR);
   g_cfg.trail_at_r = MathMax(0.0, InpTrailAtR);
   g_cfg.trail_atr = MathMax(0.0, InpTrailAtr);
   g_cfg.time_stop_bars = MathMax(0, InpTimeStopBars);
   g_cfg.min_confidence = MathMax(0.0, MathMin(0.99, InpMinConfidence));
   g_cfg.min_rr = MathMax(0.1, InpMinRR);
   g_cfg.max_signals_per_bar = MathMax(0, InpMaxSignalsPerBar);
   g_cfg.allow_opposite = InpAllowOpposite;
   g_cfg.utc_offset_override = InpUtcOffsetOverride;
   g_cfg.stop_buffer_points = MathMax(0, InpStopBufferPoints);
   g_cfg.close_on_opposite = InpCloseOnOpposite;
   g_cfg.write_tester_report = InpWriteTesterReport;

   if(g_cfg.mode == TA_MODE_TESTER)
   {
      g_cfg.use_native_signals = true;
      g_cfg.allow_new_trades = InpAllowNewTrades;
   }

   FolderCreate(g_cfg.report_dir, FILE_COMMON);

   string error = "";
   if(!g_features.Init(g_cfg.symbol, g_cfg.timeframe, g_cfg.history_bars,
                       g_cfg.utc_offset_override, error))
   {
      PrintFormat("TA: feature init failed: %s", error);
      return INIT_FAILED;
   }
   g_signals.Init(g_features);
   g_trades.Init(g_cfg);
   g_state.Init(g_cfg.report_dir, g_cfg.mode != TA_MODE_TESTER);
   g_state.LoadState();
   g_state.LoadKeys();
   g_state.LoadPositions();
   g_report.SetInitialBalance(AccountInfoDouble(ACCOUNT_BALANCE));

   if(g_cfg.mode != TA_MODE_TESTER)
   {
      g_token = TA_LoadToken(g_cfg.token_file, error);
      g_token_ok = (StringLen(g_token) >= 32);
      if(!g_token_ok)
         PrintFormat("TA: protocol token unavailable (%s); Python intents disabled",
                     error);
   }

   // Adopt any broker positions that already carry our magic.
   TASnapshot zero;
   zero.valid = false;
   zero.atr = 0.0;
   zero.spread_points = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_cfg.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_cfg.magic)
         continue;
      int index = -1;
      if(g_state.FindPosition(ticket, index) < 0)
         g_trades.AdoptPosition(g_cfg, ticket, zero, g_state, g_risk);
   }

   EnsureFeatures();
   RollDay();

   if(g_cfg.mode != TA_MODE_TESTER && g_token_ok)
   {
      EventSetMillisecondTimer((int)MathMax(250, g_cfg.heartbeat_ms));
      ConnectIfNeeded();
   }

   PrintFormat("TA init: symbol=%s tf=%s mode=%s magic=%I64d risk=%.2f%% "
               "maxpos=%d maxspread=%.0f native=%s token=%s",
               g_cfg.symbol, EnumToString(g_cfg.timeframe),
               TA_ModeName(g_cfg.mode), g_cfg.magic, g_cfg.risk_percent,
               g_cfg.max_positions, g_cfg.max_spread_points,
               g_cfg.use_native_signals ? "on" : "off",
               g_token_ok ? "ok" : "missing");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   g_socket.Close();
   g_features.Deinit();
   if(g_cfg.write_tester_report && g_cfg.mode == TA_MODE_TESTER &&
      !g_report_written && !MQLInfoInteger(MQL_OPTIMIZATION))
   {
      g_report_written = true;
      g_report.Write(g_cfg, g_state);
   }
   PrintFormat("TA deinit: reason=%d trades=%d netR=%.2f", reason,
               g_state.RecordCount(), g_report.Score(g_state));
}

void OnTick()
{
   if(!EnsureFeatures())
      return;
   RollDay();

   TASnapshot snapshot;
   g_features.Current(snapshot);

   datetime bar_zero = iTime(g_cfg.symbol, g_cfg.timeframe, 0);
   if(bar_zero != g_last_bar && bar_zero > 0)
   {
      OnNewBar();
      g_features.Current(snapshot);
   }

   if(snapshot.valid)
      ManageAndSync(snapshot);

   if(g_cfg.mode != TA_MODE_TESTER)
      PumpSocket();
}

void OnTimer()
{
   if(g_cfg.mode == TA_MODE_TESTER)
      return;
   PumpSocket();
   if(g_features_ready)
   {
      TASnapshot snapshot;
      g_features.Current(snapshot);
      if(snapshot.valid)
         ManageAndSync(snapshot);
   }
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.symbol != g_cfg.symbol)
      return;
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD ||
      trans.type == TRADE_TRANSACTION_REQUEST)
   {
      g_positions_dirty = true;
      g_last_position_send = 0;
   }
}

double OnTester()
{
   double score = g_report.Score(g_state);
   if(g_cfg.write_tester_report && !MQLInfoInteger(MQL_OPTIMIZATION) &&
      !g_report_written)
   {
      g_report_written = true;
      g_report.Write(g_cfg, g_state);
   }
   PrintFormat("TA tester: trades=%d netR=%.2f", g_state.RecordCount(), score);
   return score;
}
