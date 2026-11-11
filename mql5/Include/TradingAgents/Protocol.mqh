//+------------------------------------------------------------------+
//| Protocol.mqh - v1 envelope, token and JSON message helpers       |
//+------------------------------------------------------------------+
#ifndef TA_PROTOCOL_MQH
#define TA_PROTOCOL_MQH

#include <TradingAgents/Types.mqh>
#include <TradingAgents/Json.mqh>

#define TA_MAX_FRAME_BYTES 1048576
#define TA_MAX_CLOCK_SKEW 10.0

struct TAEnvelope
{
   bool     ok;
   string   error;
   string   type;
   string   id;
   string   payload;
   long     seq;
   double   ts;
};

string TA_RetcodeName(const int code);

//+------------------------------------------------------------------+
//| Token file and constant-time comparison                          |
//+------------------------------------------------------------------+
string TA_LoadToken(const string relative_path, string &error)
{
   error = "";
   int handle = FileOpen(relative_path,
                         FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
   {
      error = StringFormat("token file not found in common files: %s (error %d)",
                           relative_path, GetLastError());
      return "";
   }
   string value = "";
   while(!FileIsEnding(handle))
   {
      string part = FileReadString(handle);
      value += part;
   }
   FileClose(handle);
   StringTrimLeft(value);
   StringTrimRight(value);
   if(StringLen(value) < 32)
   {
      error = "token file is missing or shorter than 32 characters";
      return "";
   }
   return value;
}

bool TA_TokenEqual(const string supplied, const string expected)
{
   int a = StringLen(supplied);
   int b = StringLen(expected);
   if(a == 0 || b == 0)
      return false;
   int diff = a ^ b;
   int n = (a > b) ? a : b;
   for(int i = 0; i < n; i++)
   {
      ushort ca = (i < a) ? StringGetCharacter(supplied, i) : 0;
      ushort cb = (i < b) ? StringGetCharacter(expected, i) : 0;
      diff |= (ca ^ cb);
   }
   return (diff == 0);
}

//+------------------------------------------------------------------+
//| SHA-256 helper for native idempotency keys                       |
//+------------------------------------------------------------------+
string TA_Sha256Hex(const string text)
{
   uchar data[];
   uchar key[];
   uchar hash[];
   int length = StringToCharArray(text, data, 0, WHOLE_ARRAY, CP_UTF8) - 1;
   if(length < 0)
      length = 0;
   ArrayResize(data, length);
   int encoded = CryptEncode(CRYPT_HASH_SHA256, data, key, hash);
   if(encoded <= 0)
      return "";
   string out = "";
   for(int i = 0; i < ArraySize(hash); i++)
      out += StringFormat("%02x", (int)hash[i]);
   return out;
}

string TA_NativeKey(const string strategy, const string symbol,
                    const datetime bar_time, const int direction)
{
   string raw = StringFormat("%s|%s|%I64d|%d", strategy, symbol,
                             (long)bar_time, direction);
   string hash = TA_Sha256Hex(raw);
   if(StringLen(hash) == 64)
      return hash;
   // A readable fallback is still unique; never silently reuse a key.
   return raw;
}

//+------------------------------------------------------------------+
//| Envelope build / parse                                           |
//+------------------------------------------------------------------+
string TA_BuildEnvelope(const string msg_type, const string payload,
                        const string token, const long seq,
                        string &msg_id)
{
   msg_id = StringFormat("ea-%I64d-%I64d", (long)GetMicrosecondCount(), seq);
   double ts = (double)TimeGMT();
   string body = "{\"v\":" + IntegerToString(TA_PROTOCOL_VERSION) +
                 ",\"type\":\"" + TA_JsonEscape(msg_type) +
                 "\",\"id\":\"" + TA_JsonEscape(msg_id) +
                 "\",\"seq\":" + IntegerToString(seq) +
                 ",\"ts\":" + StringFormat("%.3f", ts) +
                 ",\"token\":\"" + TA_JsonEscape(token) +
                 "\",\"payload\":" + payload + "}";
   return body;
}

bool TA_ParseEnvelope(const string line, const string expected_token,
                      TAEnvelope &envelope, bool &auth_failed)
{
   envelope.ok = false;
   envelope.error = "";
   envelope.type = "";
   envelope.id = "";
   envelope.payload = "{}";
   envelope.seq = 0;
   envelope.ts = 0.0;
   auth_failed = false;

   if(StringLen(line) == 0)
   {
      envelope.error = "empty frame";
      return false;
   }
   if(StringLen(line) > TA_MAX_FRAME_BYTES)
   {
      envelope.error = "frame exceeds the 1 MiB limit";
      return false;
   }
   if(StringGetCharacter(line, 0) != '{')
   {
      envelope.error = "frame must be a JSON object";
      return false;
   }

   long version = TA_JsonGetLong(line, "v", 0);
   if(version != TA_PROTOCOL_VERSION)
   {
      envelope.error = StringFormat("unsupported protocol version: %I64d", version);
      return false;
   }
   string msg_type = TA_JsonGetString(line, "type", "");
   string msg_id = TA_JsonGetString(line, "id", "");
   long seq = TA_JsonGetLong(line, "seq", 0);
   double ts = TA_JsonGetDouble(line, "ts", 0.0);
   string payload = TA_JsonGetObject(line, "payload", "{}");
   string supplied = TA_JsonGetString(line, "token", "");

   if(StringLen(msg_type) == 0)
   {
      envelope.error = "missing message type";
      return false;
   }
   if(StringLen(msg_id) == 0)
   {
      envelope.error = "missing message id";
      return false;
   }
   if(seq < 1)
   {
      envelope.error = "seq must be a positive integer";
      return false;
   }
   if(ts <= 0.0)
   {
      envelope.error = "ts must be epoch seconds";
      return false;
   }
   if(!TA_TokenEqual(supplied, expected_token))
   {
      envelope.error = "bad or missing token";
      auth_failed = true;
      return false;
   }

   double now = (double)TimeGMT();
   if(now > 0.0 && MathAbs(now - ts) > TA_MAX_CLOCK_SKEW)
   {
      envelope.error = StringFormat("clock skew %.1fs exceeds %.1fs",
                                    MathAbs(now - ts), TA_MAX_CLOCK_SKEW);
      return false;
   }

   envelope.ok = true;
   envelope.type = msg_type;
   envelope.id = msg_id;
   envelope.payload = payload;
   envelope.seq = seq;
   envelope.ts = ts;
   return true;
}

//+------------------------------------------------------------------+
//| Payload builders                                                 |
//+------------------------------------------------------------------+
string TA_StatusName(const int status)
{
   if(status == 0)
      return "filled";
   if(status == 1)
      return "duplicate";
   if(status == 2)
      return "rejected";
   if(status == 3)
      return "dry_run";
   return "unknown";
}

string TA_AckPayload(const string key, const int status, const ulong ticket,
                     const double price, const double volume,
                     const int retcode, const double slippage_points,
                     const string message)
{
   string payload = "{";
   payload += "\"idempotency_key\":\"" + TA_JsonEscape(key) + "\"";
   payload += ",\"status\":\"" + TA_StatusName(status) + "\"";
   payload += ",\"ticket\":" + StringFormat("%I64d", (long)ticket);
   payload += ",\"price\":" + StringFormat("%.5f", price);
   payload += ",\"volume\":" + StringFormat("%.2f", volume);
   payload += ",\"retcode\":" + IntegerToString(retcode);
   payload += ",\"retcode_name\":\"" + TA_JsonEscape(TA_RetcodeName(retcode)) + "\"";
   payload += ",\"slippage_points\":" + StringFormat("%.1f", slippage_points);
   payload += ",\"message\":\"" + TA_JsonEscape(message) + "\"";
   payload += ",\"timestamp\":" + StringFormat("%.3f", (double)TimeGMT());
   payload += "}";
   return payload;
}

string TA_RejectPayload(const string msg_id, const string reason,
                        const long expected_seq, const long received_seq)
{
   return "{\"id\":\"" + TA_JsonEscape(msg_id) +
          "\",\"reason\":\"" + TA_JsonEscape(reason) +
          "\",\"expected_seq\":" + IntegerToString(expected_seq) +
          ",\"received_seq\":" + IntegerToString(received_seq) + "}";
}

string TA_EventPayload(const string kind, const string reason,
                       const double value)
{
   return "{\"kind\":\"" + TA_JsonEscape(kind) +
          "\",\"reason\":\"" + TA_JsonEscape(reason) +
          "\",\"value\":" + StringFormat("%.4f", value) +
          ",\"time\":" + StringFormat("%.3f", (double)TimeGMT()) + "}";
}

string TA_PositionObject(const ulong ticket, const string symbol,
                         const int direction, const double volume,
                         const double entry, const double sl,
                         const double tp, const double price,
                         const double profit, const double swap,
                         const datetime opened_at, const int strategy,
                         const double mfe_r, const double mae_r,
                         const bool partial_done, const bool breakeven_done)
{
   string obj = "{";
   obj += "\"ticket\":" + StringFormat("%I64d", (long)ticket);
   obj += ",\"symbol\":\"" + TA_JsonEscape(symbol) + "\"";
   obj += ",\"direction\":" + IntegerToString(direction);
   obj += ",\"volume\":" + StringFormat("%.2f", volume);
   obj += ",\"entry\":" + StringFormat("%.5f", entry);
   obj += ",\"sl\":" + StringFormat("%.5f", sl);
   obj += ",\"tp\":" + StringFormat("%.5f", tp);
   obj += ",\"price\":" + StringFormat("%.5f", price);
   obj += ",\"profit\":" + StringFormat("%.2f", profit);
   obj += ",\"swap\":" + StringFormat("%.2f", swap);
   obj += ",\"opened_at\":" + StringFormat("%I64d", (long)opened_at);
   obj += ",\"strategy\":\"" + TA_StrategyName(strategy) + "\"";
   obj += ",\"comment\":\"TA:" + TA_StrategyName(strategy) + "\"";
   obj += ",\"mfe_r\":" + StringFormat("%.3f", mfe_r);
   obj += ",\"mae_r\":" + StringFormat("%.3f", mae_r);
   obj += ",\"partial_done\":" + (partial_done ? "true" : "false");
   obj += ",\"breakeven_done\":" + (breakeven_done ? "true" : "false");
   obj += "}";
   return obj;
}

//+------------------------------------------------------------------+
//| Broker retcode names (the common subset is enough for reports)   |
//+------------------------------------------------------------------+
string TA_RetcodeName(const int code)
{
   if(code == 0)
      return "NONE";
   if(code == 10004)
      return "TRADE_RETCODE_REQUOTE";
   if(code == 10006)
      return "TRADE_RETCODE_REJECT";
   if(code == 10007)
      return "TRADE_RETCODE_CANCEL";
   if(code == 10008)
      return "TRADE_RETCODE_PLACED";
   if(code == 10009)
      return "TRADE_RETCODE_DONE";
   if(code == 10010)
      return "TRADE_RETCODE_DONE_PARTIAL";
   if(code == 10011)
      return "TRADE_RETCODE_ERROR";
   if(code == 10012)
      return "TRADE_RETCODE_TIMEOUT";
   if(code == 10013)
      return "TRADE_RETCODE_INVALID";
   if(code == 10014)
      return "TRADE_RETCODE_INVALID_VOLUME";
   if(code == 10015)
      return "TRADE_RETCODE_INVALID_PRICE";
   if(code == 10016)
      return "TRADE_RETCODE_INVALID_STOPS";
   if(code == 10017)
      return "TRADE_RETCODE_TRADE_DISABLED";
   if(code == 10018)
      return "TRADE_RETCODE_MARKET_CLOSED";
   if(code == 10019)
      return "TRADE_RETCODE_NO_MONEY";
   if(code == 10020)
      return "TRADE_RETCODE_PRICE_CHANGED";
   if(code == 10021)
      return "TRADE_RETCODE_PRICE_OFF";
   if(code == 10022)
      return "TRADE_RETCODE_INVALID_EXPIRATION";
   if(code == 10023)
      return "TRADE_RETCODE_ORDER_CHANGED";
   if(code == 10024)
      return "TRADE_RETCODE_TOO_MANY_REQUESTS";
   if(code == 10025)
      return "TRADE_RETCODE_NO_CHANGES";
   if(code == 10026)
      return "TRADE_RETCODE_SERVER_DISABLES_AT";
   if(code == 10027)
      return "TRADE_RETCODE_CLIENT_DISABLES_AT";
   if(code == 10028)
      return "TRADE_RETCODE_LOCKED";
   if(code == 10029)
      return "TRADE_RETCODE_FROZEN";
   if(code == 10030)
      return "TRADE_RETCODE_INVALID_FILL";
   if(code == 10031)
      return "TRADE_RETCODE_CONNECTION";
   if(code == 10032)
      return "TRADE_RETCODE_ONLY_REAL";
   if(code == 10033)
      return "TRADE_RETCODE_LIMIT_ORDERS";
   if(code == 10034)
      return "TRADE_RETCODE_LIMIT_VOLUME";
   if(code == 10035)
      return "TRADE_RETCODE_INVALID_ORDER";
   if(code == 10036)
      return "TRADE_RETCODE_POSITION_CLOSED";
   if(code == 10038)
      return "TRADE_RETCODE_INVALID_CLOSE_VOLUME";
   if(code == 10039)
      return "TRADE_RETCODE_CLOSE_ORDER_EXIST";
   if(code == 10040)
      return "TRADE_RETCODE_LIMIT_POSITIONS";
   if(code == 10041)
      return "TRADE_RETCODE_REJECT_CANCEL";
   if(code == 10042)
      return "TRADE_RETCODE_LONG_ONLY";
   if(code == 10043)
      return "TRADE_RETCODE_SHORT_ONLY";
   if(code == 10044)
      return "TRADE_RETCODE_CLOSE_ONLY";
   if(code == 10045)
      return "TRADE_RETCODE_FIFO_CLOSE";
   if(code == 10046)
      return "TRADE_RETCODE_HEDGE_PROHIBITED";
   return StringFormat("RETCODE_%d", code);
}

#endif // TA_PROTOCOL_MQH
