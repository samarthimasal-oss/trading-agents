//+------------------------------------------------------------------+
//| Json.mqh - small, dependency-free JSON helpers for the EA protocol|
//+------------------------------------------------------------------+
#ifndef TA_JSON_MQH
#define TA_JSON_MQH

string TA_JsonEscape(const string value)
{
   string out = "";
   int n = StringLen(value);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c == '"')
         out += "\\\"";
      else if(c == '\\')
         out += "\\\\";
      else if(c == '\n')
         out += "\\n";
      else if(c == '\r')
         out += "\\r";
      else if(c == '\t')
         out += "\\t";
      else if(c < 32)
         out += StringFormat("\\u%04x", (int)c);
      else
         out += ShortToString(c);
   }
   return out;
}

string TA_JsonUnescape(const string value)
{
   string out = "";
   int n = StringLen(value);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c != '\\' || i + 1 >= n)
      {
         out += ShortToString(c);
         continue;
      }
      i++;
      ushort e = StringGetCharacter(value, i);
      if(e == '"')
         out += "\"";
      else if(e == '\\')
         out += "\\";
      else if(e == '/')
         out += "/";
      else if(e == 'b')
         out += ShortToString((ushort)8);
      else if(e == 'f')
         out += ShortToString((ushort)12);
      else if(e == 'n')
         out += "\n";
      else if(e == 'r')
         out += "\r";
      else if(e == 't')
         out += "\t";
      else if(e == 'u' && i + 4 < n)
      {
         string hex = StringSubstr(value, i + 1, 4);
         int code = (int)StringToInteger("0x" + hex);
         if(code > 0 && code <= 0xFFFF)
            out += ShortToString((ushort)code);
         i += 4;
      }
      else
         out += ShortToString(e);
   }
   return out;
}

bool TA_IsSpace(ushort c)
{
   return (c == ' ' || c == '\t' || c == '\r' || c == '\n');
}

// Returns the position of the first value character after "key":.
int TA_JsonValueStart(const string json, const string key, int start = 0)
{
   string needle = "\"" + key + "\"";
   int pos = start;
   int n = StringLen(json);
   while(pos >= 0 && pos < n)
   {
      pos = StringFind(json, needle, pos);
      if(pos < 0)
         return -1;
      int before = pos - 1;
      while(before >= 0 && TA_IsSpace(StringGetCharacter(json, before)))
         before--;
      if(before >= 0 && StringGetCharacter(json, before) != '{' &&
         StringGetCharacter(json, before) != ',')
      {
         pos += StringLen(needle);
         continue;
      }
      int after = pos + StringLen(needle);
      while(after < n && TA_IsSpace(StringGetCharacter(json, after)))
         after++;
      if(after < n && StringGetCharacter(json, after) == ':')
         return after + 1;
      pos += StringLen(needle);
   }
   return -1;
}

string TA_JsonRawValue(const string json, const string key, const string fallback = "")
{
   int start = TA_JsonValueStart(json, key);
   if(start < 0)
      return fallback;
   int n = StringLen(json);
   while(start < n && TA_IsSpace(StringGetCharacter(json, start)))
      start++;
   if(start >= n)
      return fallback;

   ushort first = StringGetCharacter(json, start);
   if(first == '"')
   {
      int i = start + 1;
      bool escaped = false;
      while(i < n)
      {
         ushort c = StringGetCharacter(json, i);
         if(escaped)
         {
            escaped = false;
         }
         else if(c == '\\')
         {
            escaped = true;
         }
         else if(c == '"')
         {
            return StringSubstr(json, start, i - start + 1);
         }
         i++;
      }
      return fallback;
   }

   if(first == '{' || first == '[')
   {
      ushort open = first;
      ushort close = (first == '{') ? '}' : ']';
      int depth = 0;
      bool in_string = false;
      bool escaped = false;
      for(int i = start; i < n; i++)
      {
         ushort c = StringGetCharacter(json, i);
         if(in_string)
         {
            if(escaped)
               escaped = false;
            else if(c == '\\')
               escaped = true;
            else if(c == '"')
               in_string = false;
            continue;
         }
         if(c == '"')
         {
            in_string = true;
            continue;
         }
         if(c == open)
            depth++;
         else if(c == close)
         {
            depth--;
            if(depth == 0)
               return StringSubstr(json, start, i - start + 1);
         }
      }
      return fallback;
   }

   int i = start;
   while(i < n)
   {
      ushort c = StringGetCharacter(json, i);
      if(c == ',' || c == '}' || c == ']' || TA_IsSpace(c))
         break;
      i++;
   }
   return StringSubstr(json, start, i - start);
}

bool TA_JsonHasKey(const string json, const string key)
{
   return TA_JsonValueStart(json, key) >= 0;
}

string TA_JsonGetString(const string json, const string key, const string fallback = "")
{
   string raw = TA_JsonRawValue(json, key, "");
   if(raw == "" || raw == "null")
      return fallback;
   int n = StringLen(raw);
   if(n >= 2 && StringGetCharacter(raw, 0) == '"' &&
      StringGetCharacter(raw, n - 1) == '"')
      return TA_JsonUnescape(StringSubstr(raw, 1, n - 2));
   return raw;
}

double TA_JsonGetDouble(const string json, const string key, const double fallback = 0.0)
{
   string raw = TA_JsonRawValue(json, key, "");
   if(raw == "" || raw == "null")
      return fallback;
   if(StringGetCharacter(raw, 0) == '"')
      raw = TA_JsonUnescape(StringSubstr(raw, 1, StringLen(raw) - 2));
   if(!MathIsValidNumber(StringToDouble(raw)))
      return fallback;
   return StringToDouble(raw);
}

long TA_JsonGetLong(const string json, const string key, const long fallback = 0)
{
   string raw = TA_JsonRawValue(json, key, "");
   if(raw == "" || raw == "null")
      return fallback;
   if(StringGetCharacter(raw, 0) == '"')
      raw = TA_JsonUnescape(StringSubstr(raw, 1, StringLen(raw) - 2));
   return (long)StringToInteger(raw);
}

bool TA_JsonGetBool(const string json, const string key, const bool fallback = false)
{
   string raw = TA_JsonRawValue(json, key, "");
   if(raw == "")
      return fallback;
   StringToLower(raw);
   if(raw == "true" || raw == "1" || raw == "\"true\"")
      return true;
   if(raw == "false" || raw == "0" || raw == "\"false\"")
      return false;
   return fallback;
}

string TA_JsonGetObject(const string json, const string key, const string fallback = "{}")
{
   string raw = TA_JsonRawValue(json, key, "");
   if(StringLen(raw) >= 2 && StringGetCharacter(raw, 0) == '{')
      return raw;
   return fallback;
}

string TA_JsonGetArray(const string json, const string key, const string fallback = "[]")
{
   string raw = TA_JsonRawValue(json, key, "");
   if(StringLen(raw) >= 2 && StringGetCharacter(raw, 0) == '[')
      return raw;
   return fallback;
}

// Builds {"key":value,...} from alternating key/value string arguments.
string TA_JsonObject(const string &parts[])
{
   string out = "{";
   int n = ArraySize(parts);
   for(int i = 0; i + 1 < n; i += 2)
   {
      if(i > 0)
         out += ",";
      out += "\"" + TA_JsonEscape(parts[i]) + "\":" + parts[i + 1];
   }
   out += "}";
   return out;
}

#endif // TA_JSON_MQH
